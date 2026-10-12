from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

PREFIX = "bank_transaction_monitoring."


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    btm_high_value_threshold = fields.Float(
        string="High Value Threshold", config_parameter=PREFIX + "high_value_threshold", default=10_000_000.0,
    )
    btm_velocity_max_count = fields.Integer(
        string="Max Transactions in Window", config_parameter=PREFIX + "velocity_max_count", default=5,
    )
    btm_velocity_window_minutes = fields.Integer(
        string="Window (minutes)", config_parameter=PREFIX + "velocity_window_minutes", default=10,
    )
    btm_batch_size = fields.Integer(string="Batch Size", config_parameter=PREFIX + "batch_size", default=1000)
    btm_max_attempts = fields.Integer(string="Max Attempts", config_parameter=PREFIX + "max_attempts", default=3)
    btm_time_budget_seconds = fields.Integer(
        string="Time Budget (seconds)", config_parameter=PREFIX + "time_budget_seconds", default=90,
    )

    @api.constrains(
        "btm_high_value_threshold", "btm_velocity_max_count", "btm_velocity_window_minutes",
        "btm_batch_size", "btm_max_attempts", "btm_time_budget_seconds",
    )
    def _check_monitoring_params(self):
        for settings in self:
            values = [
                settings.btm_high_value_threshold, settings.btm_velocity_max_count,
                settings.btm_velocity_window_minutes, settings.btm_batch_size,
                settings.btm_max_attempts, settings.btm_time_budget_seconds,
            ]
            if any(value <= 0 for value in values):
                raise ValidationError(_("Transaction monitoring settings must all be greater than zero."))
