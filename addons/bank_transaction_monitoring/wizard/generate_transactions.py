import random
import uuid
from datetime import timedelta

from odoo import _, fields, models
from odoo.exceptions import UserError

CHUNK_SIZE = 1000


class BankTransactionGenerate(models.TransientModel):
    """Creates realistic pending transactions to demonstrate volume handling."""

    _name = "bank.transaction.generate"
    _description = "Generate Test Transactions"

    transaction_count = fields.Integer(string="Transactions", default=10000, required=True)
    customer_count = fields.Integer(string="Customers", default=200, required=True)
    spread_hours = fields.Integer(string="Spread Over (hours)", default=24, required=True)
    high_value_ratio = fields.Float(string="High Value Share (%)", default=1.0)
    burst_customers = fields.Integer(
        string="Customers With Bursts", default=5,
        help="Customers who make 7 transactions within a few minutes.",
    )

    def _get_customers(self):
        Partner = self.env["res.partner"]
        customers = Partner.search([("ref", "=like", "BTM-CUST-%")], limit=self.customer_count)
        missing = self.customer_count - len(customers)
        if missing > 0:
            start = len(customers)
            customers |= Partner.create([
                {"name": f"Demo Customer {start + i + 1}", "ref": f"BTM-CUST-{start + i + 1:05d}"}
                for i in range(missing)
            ])
        return customers

    def action_generate(self):
        self.ensure_one()
        if min(self.transaction_count, self.customer_count, self.spread_hours) <= 0:
            raise UserError(_("Counts and spread must be greater than zero."))

        customer_ids = self._get_customers().ids
        end = fields.Datetime.now()
        start = end - timedelta(hours=self.spread_hours)
        span = (end - start).total_seconds()
        batch_prefix = uuid.uuid4().hex[:8].upper()

        vals_list = []
        for i in range(self.transaction_count):
            high_value = random.random() * 100 < self.high_value_ratio
            vals_list.append({
                "reference": f"GEN-{batch_prefix}-{i:07d}",
                "partner_id": random.choice(customer_ids),
                "amount": round(random.uniform(10_000_000, 50_000_000) if high_value
                                else random.uniform(500, 2_000_000), 2),
                "transaction_type": random.choice(["debit", "credit"]),
                "transaction_date": start + timedelta(seconds=random.uniform(0, span)),
            })

        # Deliberate bursts: 7 transactions inside 6 minutes for a few customers.
        for b in range(min(self.burst_customers, len(customer_ids))):
            partner_id = customer_ids[b]
            burst_start = start + timedelta(seconds=random.uniform(0, max(span - 600, 0)))
            for j in range(7):
                vals_list.append({
                    "reference": f"GEN-{batch_prefix}-B{b:03d}-{j}",
                    "partner_id": partner_id,
                    "amount": round(random.uniform(1_000, 200_000), 2),
                    "transaction_type": "debit",
                    "transaction_date": burst_start + timedelta(minutes=j),
                })

        Transaction = self.env["bank.transaction"]
        for offset in range(0, len(vals_list), CHUNK_SIZE):
            Transaction.create_from_feed(vals_list[offset:offset + CHUNK_SIZE])

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _("%s pending transactions created.", len(vals_list)),
                "next": self.env["ir.actions.act_window"]._for_xml_id(
                    "bank_transaction_monitoring.action_bank_transaction"
                ),
            },
        }
