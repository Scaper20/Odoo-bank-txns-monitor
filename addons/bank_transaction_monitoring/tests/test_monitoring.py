from datetime import datetime, timedelta
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

BASE_TIME = datetime(2026, 1, 5, 9, 0, 0)


@tagged("post_install", "-at_install")
class TestTransactionMonitoring(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Transaction = cls.env["bank.transaction"]
        cls.Alert = cls.env["bank.transaction.alert"]
        cls.customer = cls.env["res.partner"].create({"name": "Ada Customer"})
        cls.other_customer = cls.env["res.partner"].create({"name": "Bola Customer"})
        cls._seq = 0

    def _txn(self, amount=50_000, minutes=0, partner=None, **vals):
        type(self)._seq += 1
        return self.Transaction.create({
            "reference": f"TEST-{self._seq:06d}",
            "partner_id": (partner or self.customer).id,
            "amount": amount,
            "transaction_date": BASE_TIME + timedelta(minutes=minutes),
            **vals,
        })

    def _run(self):
        # The cron commits after each batch; tests must stay inside their own transaction.
        return self.Transaction.with_context(monitoring_commit=False)._cron_process_transactions()

    # ------------------------------------------------------------------
    # High value rule
    # ------------------------------------------------------------------

    def test_amount_at_threshold_is_flagged(self):
        txn = self._txn(amount=10_000_000)
        self._run()
        self.assertEqual(txn.state, "flagged")
        self.assertTrue(txn.is_high_value)
        self.assertEqual(txn.alert_ids.alert_type, "high_value")

    def test_amount_below_threshold_is_processed(self):
        txn = self._txn(amount=9_999_999.99)
        self._run()
        self.assertEqual(txn.state, "processed")
        self.assertFalse(txn.alert_ids)
        self.assertTrue(txn.processed_on)

    # ------------------------------------------------------------------
    # High frequency rule
    # ------------------------------------------------------------------

    def test_six_transactions_within_ten_minutes_flags_the_sixth(self):
        txns = [self._txn(minutes=m) for m in (0, 2, 4, 6, 8, 10)]
        self._run()
        self.assertEqual([t.state for t in txns[:5]], ["processed"] * 5)
        self.assertEqual(txns[5].state, "flagged")
        self.assertTrue(txns[5].is_high_frequency)
        self.assertEqual(txns[5].alert_ids.alert_type, "high_frequency")

    def test_five_transactions_within_ten_minutes_is_normal(self):
        txns = [self._txn(minutes=m) for m in range(5)]
        self._run()
        self.assertTrue(all(t.state == "processed" for t in txns))

    def test_six_transactions_spread_over_more_than_ten_minutes_is_normal(self):
        txns = [self._txn(minutes=m * 2.5) for m in range(6)]  # 0 .. 12.5 minutes
        self._run()
        self.assertTrue(all(t.state == "processed" for t in txns))

    def test_frequency_is_counted_per_customer(self):
        txns = [self._txn(minutes=m, partner=self.customer if m % 2 else self.other_customer) for m in range(8)]
        self._run()
        self.assertTrue(all(t.state == "processed" for t in txns))

    def test_burst_split_across_runs_is_detected(self):
        first = [self._txn(minutes=m) for m in range(3)]
        self._run()
        second = [self._txn(minutes=3 + m) for m in range(3)]
        self._run()
        self.assertTrue(all(t.state == "processed" for t in first + second[:2]))
        self.assertEqual(second[2].state, "flagged")

    def test_burst_split_across_batches_is_detected(self):
        self.env["ir.config_parameter"].sudo().set_param("bank_transaction_monitoring.batch_size", 2)
        txns = [self._txn(minutes=m) for m in range(6)]
        stats = self._run()
        self.assertEqual(txns[5].state, "flagged")
        self.assertEqual(stats, {"processed": 5, "flagged": 1})

    def test_both_rules_on_one_transaction(self):
        txns = [self._txn(minutes=m) for m in range(5)] + [self._txn(amount=20_000_000, minutes=5)]
        self._run()
        self.assertEqual(txns[5].state, "flagged")
        self.assertEqual(set(txns[5].alert_ids.mapped("alert_type")), {"high_value", "high_frequency"})

    # ------------------------------------------------------------------
    # Duplicate prevention / data integrity
    # ------------------------------------------------------------------

    def test_running_twice_does_not_reprocess(self):
        normal = self._txn()
        flagged = self._txn(amount=15_000_000)
        self._run()
        processed_on = normal.processed_on
        alert_count = self.Alert.search_count([])

        stats = self._run()
        self.assertEqual(stats, {})
        self.assertEqual(normal.processed_on, processed_on)
        self.assertEqual(flagged.state, "flagged")
        self.assertEqual(self.Alert.search_count([]), alert_count)

    def test_duplicate_reference_rejected(self):
        self._txn(reference="DUP-1")
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self._txn(reference="DUP-1")

    def test_feed_ingestion_skips_duplicates(self):
        self._txn(reference="FEED-1")
        vals = {"partner_id": self.customer.id, "amount": 100, "transaction_date": BASE_TIME}
        created = self.Transaction.create_from_feed([
            {**vals, "reference": "FEED-1"},
            {**vals, "reference": "FEED-2"},
            {**vals, "reference": "FEED-2"},
        ])
        self.assertEqual(created.mapped("reference"), ["FEED-2"])

    def test_non_positive_amount_rejected(self):
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self._txn(amount=0)

    def test_processed_transaction_is_locked(self):
        txn = self._txn()
        self._run()
        with self.assertRaises(UserError):
            txn.amount = 1
        with self.assertRaises(UserError):
            txn.unlink()

    def test_reset_flagged_does_not_duplicate_alerts(self):
        txn = self._txn(amount=12_000_000)
        self._run()
        # Simulate an operator pushing the record back into the queue.
        self.env.cr.execute("UPDATE bank_transaction SET state = 'pending' WHERE id = %s", [txn.id])
        txn.invalidate_recordset()
        self._run()
        self.assertEqual(txn.state, "flagged")
        self.assertEqual(len(txn.alert_ids), 1)

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def test_failing_record_is_isolated_and_retried(self):
        good = self._txn()
        bad = self._txn()
        TransactionModel = type(self.Transaction)
        original = TransactionModel._evaluate_and_apply

        def fake_evaluate(records, params):
            if bad in records:
                raise ValueError("core banking lookup failed")
            return original(records, params)

        with patch.object(TransactionModel, "_evaluate_and_apply", fake_evaluate), \
                mute_logger("odoo.addons.bank_transaction_monitoring.models.bank_transaction"):
            stats = self._run()

        self.assertEqual(stats, {"processed": 1, "failed": 1})
        self.assertEqual(good.state, "processed")
        self.assertEqual(bad.state, "failed")
        self.assertEqual(bad.attempt_count, 1)
        self.assertIn("core banking lookup failed", bad.error_message)

        # Next run succeeds once the cause is gone.
        self._run()
        self.assertEqual(bad.state, "processed")
        self.assertFalse(bad.error_message)

    def test_failed_record_stops_after_max_attempts(self):
        self.env["ir.config_parameter"].sudo().set_param("bank_transaction_monitoring.max_attempts", 2)
        bad = self._txn()
        TransactionModel = type(self.Transaction)

        def always_fail(records, params):
            raise ValueError("boom")

        with patch.object(TransactionModel, "_evaluate_and_apply", always_fail), \
                mute_logger("odoo.addons.bank_transaction_monitoring.models.bank_transaction"):
            self._run()
            self._run()
            stats = self._run()

        self.assertEqual(bad.attempt_count, 2)
        self.assertEqual(stats, {})
        bad.action_retry()
        self.assertEqual((bad.state, bad.attempt_count), ("pending", 0))

    def test_invalid_parameter_falls_back_to_default(self):
        self.env["ir.config_parameter"].sudo().set_param("bank_transaction_monitoring.high_value_threshold", "abc")
        self.assertEqual(self.Transaction._get_monitoring_params()["high_value_threshold"], 10_000_000.0)

    # ------------------------------------------------------------------
    # Review workflow
    # ------------------------------------------------------------------

    def test_approve_and_reject(self):
        approved = self._txn(amount=11_000_000)
        rejected = self._txn(amount=11_000_000, partner=self.other_customer)
        self._run()

        approved.action_approve()
        rejected.action_reject()
        self.assertEqual(approved.state, "approved")
        self.assertEqual(approved.alert_ids.state, "reviewed")
        self.assertEqual(rejected.state, "rejected")
        self.assertEqual(rejected.alert_ids.state, "escalated")
        self.assertEqual(approved.reviewed_by_id, self.env.user)

        with self.assertRaises(UserError):
            approved.action_approve()

    def test_generator_wizard_creates_pending_transactions(self):
        wizard = self.env["bank.transaction.generate"].create({
            "transaction_count": 300, "customer_count": 10, "burst_customers": 2,
        })
        wizard.action_generate()
        pending = self.Transaction.search([("reference", "=like", "GEN-%")])
        self.assertEqual(len(pending), 314)
        self._run()
        self.assertFalse(pending.filtered(lambda t: t.state == "pending"))
        self.assertTrue(pending.filtered("is_high_frequency"))
