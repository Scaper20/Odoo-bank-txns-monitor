from odoo import _, api, fields, models


class BankTransactionAlert(models.Model):
    _name = "bank.transaction.alert"
    _description = "Transaction Monitoring Alert"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(required=True, readonly=True, copy=False, default=lambda self: _("New"))
    transaction_id = fields.Many2one(
        "bank.transaction", required=True, ondelete="cascade", index=True, readonly=True,
    )
    partner_id = fields.Many2one(
        related="transaction_id.partner_id", string="Customer", store=True, index=True,
    )
    amount = fields.Monetary(related="transaction_id.amount", store=True)
    currency_id = fields.Many2one(related="transaction_id.currency_id", store=True)
    transaction_date = fields.Datetime(related="transaction_id.transaction_date", store=True)
    alert_type = fields.Selection(
        [("high_value", "High Value"), ("high_frequency", "High Frequency")],
        required=True, readonly=True,
    )
    details = fields.Text(readonly=True)
    state = fields.Selection(
        [("new", "New"), ("reviewed", "Reviewed"), ("escalated", "Escalated"), ("dismissed", "Dismissed")],
        required=True, default="new", tracking=True, index=True,
    )
    reviewer_id = fields.Many2one("res.users", readonly=True, copy=False)
    reviewed_on = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        (
            "transaction_type_unique",
            "UNIQUE(transaction_id, alert_type)",
            "An alert of this type already exists for this transaction.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("bank.transaction.alert") or _("New")
        return super().create(vals_list)

    def _set_review_state(self, state):
        self.write({"state": state, "reviewer_id": self.env.uid, "reviewed_on": fields.Datetime.now()})

    def action_mark_reviewed(self):
        self._set_review_state("reviewed")

    def action_escalate(self):
        self._set_review_state("escalated")

    def action_dismiss(self):
        self._set_review_state("dismissed")
