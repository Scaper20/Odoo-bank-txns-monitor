{
    "name": "Bank Transaction Monitoring",
    "version": "17.0.1.0.0",
    "summary": "Background monitoring of bank transactions: high-value and high-frequency detection",
    "description": """
Periodically processes incoming bank transactions in the background:

* flags transactions of NGN 10,000,000 or more for review
* detects customers making more than 5 transactions within 10 minutes
* processes normal transactions exactly once (no duplicate processing)
* isolates failures per record and processes large volumes in batches
""",
    "category": "Accounting/Banking",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_sequence.xml",
        "data/ir_config_parameter.xml",
        "data/ir_cron.xml",
        "views/bank_transaction_views.xml",
        "views/bank_transaction_alert_views.xml",
        "views/res_config_settings_views.xml",
        "wizard/generate_transactions_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": True,
}
