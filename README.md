# Bank Transaction Monitoring (Odoo 17)

An Odoo 17 module (`bank_transaction_monitoring`) that monitors bank transactions in the background. A scheduled action:

1. **Flags high-value transactions.** Any transaction of **₦10,000,000 or more** is held for review.
2. **Detects high-frequency customers.** It raises an alert when a customer makes **more than 5 transactions within any 10-minute window**.
3. **Processes normal transactions exactly once.** Repeat runs or two runs at the same time never process a transaction twice.
4. **Handles errors and large volumes.** It works in batches, isolates failures to the record that caused them, and retries them automatically.

---

## Quick start (Docker)

Requirements: Docker with Docker Compose.

```bash
git clone <this-repo-url>
cd <repo>
docker compose up -d
```

Open http://localhost:8069, create a database, then go to **Apps** and install **Bank Transaction Monitoring**. You may need to click *Update Apps List* and remove the "Apps" filter first.

Or install from the command line:

```bash
docker compose run --rm odoo odoo -d btm -i bank_transaction_monitoring --stop-after-init
docker compose up -d
```

Then log in with the user you created (or admin/admin for the CLI-created database).

### Run the tests

```bash
docker compose run --rm odoo odoo -d btm_test -i bank_transaction_monitoring \
    --test-enable --test-tags /bank_transaction_monitoring --stop-after-init
```

Expected result: `0 failed, 0 error(s) of 20 tests`.

> On Windows Git Bash, prefix the command with `MSYS_NO_PATHCONV=1`, otherwise the `/bank_transaction_monitoring` test tag is rewritten into a Windows path and no tests run.

### Try it with volume

1. Go to **Transaction Monitoring → Configuration → Generate Test Transactions**. Create, for example, 10,000 transactions. The generator includes about 1% high-value transactions and a few customers with deliberate 7-transactions-in-6-minutes bursts.
2. Go to **Transactions → All Transactions** and click **Run Monitoring Now**. This queues the cron to run right away. Otherwise it runs every 5 minutes.
3. Check the **Review Queue** and the **Alerts** menu. The Odoo log shows a summary line for each run, for example:
   `Transaction monitoring run finished in 3.10s: 9861 processed, 146 flagged, 0 failed`
4. Click **Run Monitoring Now** again. Nothing is reprocessed.

### Using an existing Odoo 17 install

Copy `addons/bank_transaction_monitoring` into a directory on your `addons_path`, update the apps list, and install the module. It depends only on `base` and `mail`. PostgreSQL 12 or later is required.

---

## How requirements map to the implementation

| Requirement | Implementation |
|---|---|
| Runs periodically in the background | `ir.cron` *Transaction Monitoring: Process Pending Transactions* runs every 5 minutes and calls `bank.transaction._cron_process_transactions()`. "Run Monitoring Now" calls `cron._trigger()`, so manual runs also happen in the background and not in the web request. |
| Flag ≥ ₦10M for review | `amount >= threshold` (inclusive). The transaction goes to state `flagged` with `is_high_value` set, and a `high_value` alert is created. |
| More than 5 transactions in 10 minutes | For each transaction, SQL counts that customer's transactions in the window `(t - 10 min, t]`. If the count is **> 5**, the transaction is flagged with `is_high_frequency` and a `high_frequency` alert. History in every state counts, so a burst spread across batches or runs is still caught. |
| Process normal transactions without duplicates | A state machine (`pending → processed / flagged / failed`). Only `pending` and retryable `failed` rows are picked up, with `FOR UPDATE SKIP LOCKED`. UNIQUE constraints prevent duplicate transactions and duplicate alerts. |
| Error handling | Each batch runs in a savepoint. If the batch fails, every record is retried in its own savepoint. A bad record becomes `failed` with its error message and attempt count, and the rest of the batch still completes. Failed records are retried automatically up to `max_attempts`. |
| Large volumes | Batches of 1,000 (configurable). Rules run as a few set-based queries per batch, writes are grouped by outcome, there is a commit per batch, a time budget with automatic re-trigger, and dedicated indexes. |

---

## Data model

### `bank.transaction`
| Field | Notes |
|---|---|
| `reference` | Reference from the core banking system. **UNIQUE**, so the same transaction can't be loaded twice. |
| `partner_id` | The customer (`res.partner`). Reuses Odoo's standard contact model. |
| `amount`, `currency_id` | `Monetary`; defaults to NGN, which the module activates. SQL `CHECK (amount > 0)`. |
| `transaction_type`, `transaction_date` | Debit/credit, and the time the transaction happened (used by the frequency rule). |
| `state` | `pending`, `processed`, `flagged`, `approved`, `rejected`, `failed` |
| `is_high_value`, `is_high_frequency` | Which rule(s) fired. |
| `processed_on`, `attempt_count`, `error_message` | Processing audit trail. |
| `reviewed_by_id`, `reviewed_on`, `review_note` | Review audit trail. |

The model inherits `mail.thread`, so manual changes (approve, reject, edits) are tracked in the chatter.

### `bank.transaction.alert`
One alert per (transaction, rule), enforced by **UNIQUE(transaction_id, alert_type)**. Each alert has a sequence number (`ALR/2026/000001`), a readable explanation, the customer, amount and date (stored related fields, so you can filter and group on them), and its own review states (`new`, `reviewed`, `escalated`, `dismissed`).

I kept alerts in a separate model from the transaction flags because:
- one transaction can trip both rules, and each rule needs its own review trail;
- compliance teams usually work from an alert queue, not from the raw transaction ledger.

### Configuration
All thresholds are `ir.config_parameter` values, editable under **Transaction Monitoring → Configuration → Settings**:

| Parameter | Default |
|---|---|
| High value threshold | 10,000,000 |
| Max transactions in window | 5 |
| Window (minutes) | 10 |
| Batch size | 1,000 |
| Max attempts | 3 |
| Time budget per run (seconds) | 90 |

Invalid values (non-numeric or ≤ 0) are rejected in Settings. If one still gets in, the engine logs a warning and uses the default.

### Security
- **Analyst** (`group_monitoring_user`): can view and create transactions, review flagged transactions, and work alerts.
- **Manager**: everything an Analyst can do, plus deleting pending transactions, retrying failed ones, settings, the data generator, and "Run Monitoring Now".

The cron runs as the superuser.

---

## Processing engine: key design decisions

```
_cron_process_transactions()
 └─ loop until the queue is empty or the time budget is used up
     ├─ _claim_batch()        SELECT ... FOR UPDATE SKIP LOCKED, keyset on id
     ├─ _process_batch()
     │    ├─ savepoint: _evaluate_and_apply(batch)          ← fast path
     │    │     ├─ high value: in-memory comparison
     │    │     ├─ high frequency: 1 SQL query (LATERAL count, index range scans)
     │    │     ├─ alerts: 1 search for existing alerts + 1 bulk create
     │    │     └─ ≤ 4 bulk UPDATEs grouped by outcome
     │    └─ on error: each record in its own savepoint → failures marked 'failed'
     ├─ COMMIT
     └─ if over the time budget and work remains: cron._trigger() and stop
```

1. **A state machine is the main protection against duplicates.** A transaction is picked up only while it is `pending`, or `failed` with fewer than `max_attempts` attempts. Once it is `processed` or `flagged`, no later run touches it again.

2. **Concurrency control in the database: `FOR UPDATE SKIP LOCKED`.** Odoo already stops the same cron from running twice at once. But a manual trigger, a second cron, or several workers could still overlap. Row locks with `SKIP LOCKED` let concurrent runs share the queue: each run takes rows the others haven't locked, and never processes the same row twice. They also don't block each other.

3. **Keyset pagination (`id > last_id`) within a run.** Each row is visited at most once per run. This means a record that keeps failing cannot trap the loop, and it is simply retried on the next scheduled run.

4. **Idempotent at every layer.**
   - `UNIQUE(reference)` blocks duplicate ingestion. `create_from_feed()` silently skips references that are already loaded, so a feed file can be re-sent safely.
   - `UNIQUE(transaction_id, alert_type)` blocks duplicate alerts, and the engine checks for existing alerts before creating new ones. So even a transaction that is manually sent back to `pending` won't get a second alert.

5. **High-frequency detection is done in SQL and uses indexes.**
   ```sql
   SELECT t.id, w.cnt
     FROM bank_transaction t
     CROSS JOIN LATERAL (
       SELECT COUNT(*) AS cnt FROM bank_transaction h
        WHERE h.partner_id = t.partner_id
          AND h.transaction_date BETWEEN t.transaction_date - interval '10 min' AND t.transaction_date
     ) w
    WHERE t.id = ANY(:batch_ids) AND w.cnt > 5
   ```
   One query covers a whole batch. Each count is an index range scan on the composite index `(partner_id, transaction_date)`, so the cost depends on the batch size and not on the size of the table. I picked this over a window function over the batch's date range because a batch can cover a wide date span (for example during a backlog), and the lateral form stays precise and cheap either way.

   The rule is evaluated **at each transaction's own time**. The transaction that becomes the 6th (or later) within 10 minutes is the one flagged, with an alert saying how many transactions were in the window. The earlier transactions were legitimately normal when they happened.

6. **Set-based writes.** Results are grouped by outcome (processed / high value / high frequency / both), so a batch of 1,000 becomes at most 4 `UPDATE` statements plus one bulk `INSERT` for alerts. Chatter tracking is turned off for the engine's bulk writes (`tracking_disable`), because writing thousands of chatter messages would cost more than the monitoring itself. The alerts and audit fields record what happened instead.

7. **A commit per batch, a time budget, and re-triggering.** Committing after each batch means a crash or worker timeout loses at most one batch, and the state machine resumes from where it stopped. When the time budget (90 s by default, below Odoo's default `limit_time_real_cron`) runs out and work remains, the cron re-triggers itself instead of being killed partway through. Tests disable the commit through the `monitoring_commit=False` context key.

8. **Error isolation with savepoints.** The fast path runs the whole batch in one savepoint. If it fails (bad data, a constraint violation, a future rule raising an error), the batch is rolled back and every record is retried in its own savepoint. One bad record costs one failed row, not 1,000. Failed rows keep `error_message` and `attempt_count`. They are retried on later runs up to `max_attempts`, then left for a Manager to investigate and **Retry**.

9. **Indexes.**
   - `(partner_id, transaction_date)` is used by the frequency rule.
   - A partial index `ON (id) WHERE state IN ('pending','failed')` serves the cron pick-up query. It stays small because it only holds outstanding work, even when the table has millions of processed rows.
   - `transaction_date` and alert fields are indexed for the UI's filters and grouping.

10. **Data integrity guards.** The business fields (reference, customer, amount, currency, type, date) can't be changed after a transaction leaves `pending`, and only pending transactions can be deleted. These rules are enforced in the ORM, not only in the views. The database enforces the `CHECK` and `UNIQUE` constraints.

### Assumptions
- "More than five transactions within 10 minutes" means a **rolling** window: 6 or more transactions whose timestamps fall within any 10 minutes, boundaries inclusive. It is not a fixed clock window such as 10:00–10:10.
- The ₦10M threshold is **inclusive** ("₦10 million or more").
- Flagged transactions are **held** (`flagged`) until a reviewer approves or rejects them. Normal transactions are marked `processed`. "Processing" here means monitoring and clearing the transaction; posting to the accounting ledger is outside the scope.
- Amounts are assumed to be in NGN. A multi-currency bank would convert to NGN before comparing with the threshold.
- Transactions arrive from an upstream system, through `create_from_feed()`, an import, or the API. The generator wizard is for demonstration and load testing only.

### Possible extensions
- Use the OCA `queue_job` module to fan batches out across several workers.
- Add a REST/webhook ingestion endpoint that calls `create_from_feed()`.
- Partition `bank_transaction` by month, and archive old processed rows, for very large ledgers.
- Add more rules (new beneficiary, geography, structuring just below the threshold) behind a small rule registry.
- Send email or activity notifications to the compliance team when alerts are created.

---

## Tests

`addons/bank_transaction_monitoring/tests/test_monitoring.py` covers:
- the threshold boundary (₦10,000,000 is flagged; ₦9,999,999.99 is not);
- frequency boundaries: 6 transactions in 10 minutes is flagged, 5 is not, 6 transactions spread over 12.5 minutes is not, and counts are per customer;
- bursts split across runs and across batches;
- a transaction that trips both rules;
- idempotency: a second run does nothing, duplicate references are rejected, feed duplicates are skipped, and re-queuing a transaction doesn't duplicate its alert;
- error isolation: one failing record doesn't block the batch, a failed record is retried and recovers, and retries stop at `max_attempts`;
- invalid configuration falls back to defaults;
- the approve/reject workflow and the lock on edits and deletions;
- the generator wizard combined with a full run.

---

## Self-assessment

| Criteria | Max | Self-rating |
|---|---|---|
| Understanding and interpretation of requirements | 5 | 5 |
| Odoo ORM and model design | 5 | 3 |
| Python implementation and business logic | 5 | 3 |
| Scheduled actions and background processing | 5 | 4 |
| Error handling and data integrity | 5 | 3 |
| Performance and query optimisation | 5 | 3 |
| **Total** | **30** | 21 |
