import logging
import time
from collections import Counter, defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.sql import create_index

_logger = logging.getLogger(__name__)

PARAM_PREFIX = "bank_transaction_monitoring."

# Defaults used when a system parameter is missing or invalid. The type of each
# default is also the type the stored string value is converted to.
DEFAULT_PARAMS = {
    "high_value_threshold": 10_000_000.0,
    "velocity_max_count": 5,
    "velocity_window_minutes": 10,
    "batch_size": 1000,
    "max_attempts": 3,
    "time_budget_seconds": 90,
}

# Business data that must not change once the engine has picked a transaction up.
LOCKED_FIELDS = {"reference", "partner_id", "amount", "currency_id", "transaction_type", "transaction_date"}


class BankTransaction(models.Model):
    _name = "bank.transaction"
    _description = "Bank Transaction"
    _inherit = ["mail.thread"]
    _order = "transaction_date desc, id desc"
    _rec_name = "reference"

    reference = fields.Char(
        required=True, copy=False, tracking=True,
        help="Unique reference from the core banking system. Used to reject duplicate imports.",
    )
    partner_id = fields.Many2one("res.partner", string="Customer", required=True, ondelete="restrict", tracking=True)
    amount = fields.Monetary(required=True, currency_field="currency_id", tracking=True)
    currency_id = fields.Many2one("res.currency", required=True, default=lambda self: self._default_currency())
    transaction_type = fields.Selection(
        [("debit", "Debit"), ("credit", "Credit")], required=True, default="debit",
    )
    transaction_date = fields.Datetime(required=True, index=True, default=fields.Datetime.now)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processed", "Processed"),
            ("flagged", "Flagged for Review"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("failed", "Failed"),
        ],
        required=True, default="pending", copy=False, tracking=True,
    )
    is_high_value = fields.Boolean(readonly=True, copy=False)
    is_high_frequency = fields.Boolean(readonly=True, copy=False)
    alert_ids = fields.One2many("bank.transaction.alert", "transaction_id", string="Alerts")
    alert_count = fields.Integer(compute="_compute_alert_count")
    processed_on = fields.Datetime(readonly=True, copy=False)
    attempt_count = fields.Integer(readonly=True, copy=False, help="Number of failed processing attempts.")
    error_message = fields.Text(readonly=True, copy=False)
    reviewed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    reviewed_on = fields.Datetime(readonly=True, copy=False)
    review_note = fields.Text()

    _sql_constraints = [
        ("reference_unique", "UNIQUE(reference)", "A transaction with this reference already exists."),
        ("amount_positive", "CHECK(amount > 0)", "The transaction amount must be greater than zero."),
    ]

    def init(self):
        # Velocity check: count a customer's transactions in a time range.
        create_index(
            self.env.cr, "bank_transaction_partner_date_idx", self._table,
            ["partner_id", "transaction_date"],
        )
        # Cron pick-up: small partial index that only holds work still to do.
        create_index(
            self.env.cr, "bank_transaction_todo_idx", self._table,
            ["id"], where="state IN ('pending', 'failed')",
        )

    @api.model
    def _default_currency(self):
        return self.env.ref("base.NGN", raise_if_not_found=False) or self.env.company.currency_id

    @api.depends("alert_ids")
    def _compute_alert_count(self):
        counts = {
            transaction.id: count
            for transaction, count in self.env["bank.transaction.alert"]._read_group(
                [("transaction_id", "in", self.ids)], ["transaction_id"], ["__count"],
            )
        }
        for transaction in self:
            transaction.alert_count = counts.get(transaction.id, 0)

    # ------------------------------------------------------------------
    # Data integrity
    # ------------------------------------------------------------------

    def write(self, vals):
        if LOCKED_FIELDS & set(vals) and any(t.state != "pending" for t in self):
            raise UserError(_("Transaction details cannot be changed once the transaction has been processed."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_pending(self):
        if any(t.state != "pending" for t in self):
            raise UserError(_("Only pending transactions can be deleted."))

    @api.model
    def create_from_feed(self, vals_list):
        """Idempotent ingestion entry point (e.g. for an import job or API).

        References already stored are skipped instead of raising, so the same
        feed file can be safely re-sent. The UNIQUE constraint remains the
        final guarantee against concurrent imports.
        """
        refs = [vals["reference"] for vals in vals_list]
        existing = set(self.search([("reference", "in", refs)]).mapped("reference"))
        seen = set()
        new_vals = []
        for vals in vals_list:
            if vals["reference"] in existing or vals["reference"] in seen:
                continue
            seen.add(vals["reference"])
            new_vals.append(vals)
        if len(new_vals) < len(vals_list):
            _logger.info("Skipped %s duplicate transaction(s) from feed", len(vals_list) - len(new_vals))
        return self.with_context(tracking_disable=True).create(new_vals)

    # ------------------------------------------------------------------
    # Review actions
    # ------------------------------------------------------------------

    def _check_state(self, allowed):
        if any(t.state not in allowed for t in self):
            raise UserError(_("This action is not allowed for transactions in their current state."))

    def action_approve(self):
        self._check_state({"flagged"})
        self.write({"state": "approved", "reviewed_by_id": self.env.uid, "reviewed_on": fields.Datetime.now()})
        self.alert_ids.filtered(lambda a: a.state == "new").action_mark_reviewed()

    def action_reject(self):
        self._check_state({"flagged"})
        self.write({"state": "rejected", "reviewed_by_id": self.env.uid, "reviewed_on": fields.Datetime.now()})
        self.alert_ids.filtered(lambda a: a.state == "new").action_escalate()

    def action_retry(self):
        """Send failed transactions back to the queue after manual investigation."""
        self._check_state({"failed"})
        self.write({"state": "pending", "attempt_count": 0, "error_message": False})

    def action_view_alerts(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Alerts"),
            "res_model": "bank.transaction.alert",
            "view_mode": "tree,form",
            "domain": [("transaction_id", "=", self.id)],
        }

    def action_run_monitoring(self):
        """Ask the scheduler to run the monitoring job now, in the background."""
        self.env.ref("bank_transaction_monitoring.ir_cron_process_transactions")._trigger()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "message": _("Transaction monitoring has been queued and will run in the background."),
            },
        }

    # ------------------------------------------------------------------
    # Monitoring engine
    # ------------------------------------------------------------------

    @api.model
    def _get_monitoring_params(self):
        icp = self.env["ir.config_parameter"].sudo()
        params = {}
        for key, default in DEFAULT_PARAMS.items():
            raw = icp.get_param(PARAM_PREFIX + key)
            try:
                value = type(default)(raw) if raw else default
            except (TypeError, ValueError):
                value = None
            if value is None or value <= 0:
                _logger.warning("Invalid monitoring parameter %s=%r, using default %r", key, raw, default)
                value = default
            params[key] = value
        return params

    @api.model
    def _cron_process_transactions(self):
        """Scheduled action entry point.

        Claims batches of pending (or retryable failed) transactions and
        processes them until the queue is empty or the time budget is used up.
        Each batch is committed on its own so progress survives a crash or a
        worker timeout; if work remains when the budget runs out, the cron is
        re-triggered instead of being killed mid-batch.
        """
        params = self._get_monitoring_params()
        commit = self.env.context.get("monitoring_commit", True)
        deadline = time.monotonic() + params["time_budget_seconds"]
        started = time.monotonic()
        model = self.with_context(tracking_disable=True, mail_create_nolog=True, mail_notrack=True)
        totals = Counter()
        last_id = 0

        while True:
            batch = model._claim_batch(params, last_id)
            if not batch:
                break
            last_id = max(batch.ids)
            totals.update(batch._process_batch(params))
            if commit:
                self.env.cr.commit()  # pylint: disable=invalid-commit
            if time.monotonic() >= deadline:
                if model._claim_batch(params, last_id, limit=1):
                    _logger.info("Monitoring time budget reached with work remaining; re-triggering cron")
                    self.env.ref("bank_transaction_monitoring.ir_cron_process_transactions")._trigger()
                break

        _logger.info(
            "Transaction monitoring run finished in %.2fs: %s processed, %s flagged, %s failed",
            time.monotonic() - started, totals["processed"], totals["flagged"], totals["failed"],
        )
        return dict(totals)

    @api.model
    def _claim_batch(self, params, after_id, limit=None):
        """Lock and return the next batch of transactions to process.

        * ``id > after_id`` (keyset pagination) guarantees each row is visited
          at most once per run, so a record that keeps failing cannot loop.
        * ``FOR UPDATE SKIP LOCKED`` lets concurrent runs share the queue
          without ever processing the same row twice.
        """
        self.env.cr.execute(
            """
            SELECT id
              FROM bank_transaction
             WHERE id > %s
               AND (state = 'pending' OR (state = 'failed' AND attempt_count < %s))
             ORDER BY id
             LIMIT %s
               FOR UPDATE SKIP LOCKED
            """,
            (after_id, params["max_attempts"], limit or params["batch_size"]),
        )
        return self.browse(row[0] for row in self.env.cr.fetchall())

    def _process_batch(self, params):
        """Process a batch in one go; on error, fall back to one record at a time.

        The fast path does everything with a handful of set-based queries. If
        anything in it fails, the savepoint rolls the batch back and every
        record is retried in its own savepoint, so a single bad record is
        marked as failed without blocking the others.
        """
        try:
            with self.env.cr.savepoint():
                return self._evaluate_and_apply(params)
        except Exception:
            _logger.exception("Batch of %s transactions failed, retrying one by one", len(self))
            self.env.invalidate_all()

        stats = Counter()
        for transaction in self:
            try:
                with self.env.cr.savepoint():
                    stats.update(transaction._evaluate_and_apply(params))
            except Exception as error:
                self.env.invalidate_all()
                _logger.exception("Transaction %s failed to process", transaction.id)
                transaction._mark_failed(error)
                stats["failed"] += 1
        return stats

    def _mark_failed(self, error):
        self.ensure_one()
        self.write({
            "state": "failed",
            "attempt_count": self.attempt_count + 1,
            "error_message": f"{type(error).__name__}: {error}"[:2000],
        })

    def _evaluate_and_apply(self, params):
        """Run all rules on this recordset and persist the outcome in bulk."""
        threshold = params["high_value_threshold"]
        high_value_ids = {t.id for t in self if t.amount >= threshold}
        high_frequency = self._detect_high_frequency(params)

        self._create_alerts(high_value_ids, high_frequency, params)

        # Group records by outcome so each group is a single UPDATE.
        groups = defaultdict(list)
        for transaction in self:
            key = (transaction.id in high_value_ids, transaction.id in high_frequency)
            groups[key].append(transaction.id)

        now = fields.Datetime.now()
        stats = Counter()
        for (is_high_value, is_high_frequency), ids in groups.items():
            flagged = is_high_value or is_high_frequency
            self.browse(ids).write({
                "state": "flagged" if flagged else "processed",
                "is_high_value": is_high_value,
                "is_high_frequency": is_high_frequency,
                "processed_on": now,
                "error_message": False,
            })
            stats["flagged" if flagged else "processed"] += len(ids)
        return stats

    def _detect_high_frequency(self, params):
        """Return {transaction_id: window_count} for transactions in ``self``
        that are the (max_count + 1)th or later transaction of their customer
        within the rolling window ending at that transaction.

        History in any state counts, so bursts that span several batches or
        cron runs are still detected. Each count is an index range scan on
        (partner_id, transaction_date).
        """
        if not self:
            return {}
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT t.id, w.cnt
              FROM bank_transaction t
              CROSS JOIN LATERAL (
                    SELECT COUNT(*) AS cnt
                      FROM bank_transaction h
                     WHERE h.partner_id = t.partner_id
                       AND h.transaction_date BETWEEN t.transaction_date - make_interval(mins => %(window)s)
                                                  AND t.transaction_date
              ) w
             WHERE t.id = ANY(%(ids)s)
               AND w.cnt > %(max_count)s
            """,
            {"ids": self.ids, "window": params["velocity_window_minutes"], "max_count": params["velocity_max_count"]},
        )
        return dict(self.env.cr.fetchall())

    def _create_alerts(self, high_value_ids, high_frequency, params):
        Alert = self.env["bank.transaction.alert"]
        # Idempotency: never recreate an alert that already exists (e.g. after
        # a reviewer sent a transaction back to pending).
        existing = {
            (alert.transaction_id.id, alert.alert_type)
            for alert in Alert.search([("transaction_id", "in", self.ids)])
        }
        window = params["velocity_window_minutes"]
        vals_list = []
        for transaction in self:
            if transaction.id in high_value_ids and (transaction.id, "high_value") not in existing:
                vals_list.append({
                    "transaction_id": transaction.id,
                    "alert_type": "high_value",
                    "details": _(
                        "Amount %(amount)s %(currency)s is at or above the review threshold of %(threshold)s.",
                        amount=f"{transaction.amount:,.2f}", currency=transaction.currency_id.name,
                        threshold=f"{params['high_value_threshold']:,.2f}",
                    ),
                })
            if transaction.id in high_frequency and (transaction.id, "high_frequency") not in existing:
                vals_list.append({
                    "transaction_id": transaction.id,
                    "alert_type": "high_frequency",
                    "details": _(
                        "Customer made %(count)s transactions within %(window)s minutes (limit %(limit)s).",
                        count=high_frequency[transaction.id], window=window, limit=params["velocity_max_count"],
                    ),
                })
        return Alert.create(vals_list)
