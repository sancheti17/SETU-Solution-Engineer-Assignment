# Demo recording outline (about 5-7 minutes)

This is a script to record, not a generated screen recording. Use your real public deployment and keep API keys/secrets hidden. Run the collection once before recording.

## 0:00 — Show the reviewer can run it

Show the repository README, real hosted base URL, and a successful `/health` response. Mention Flask, SQLite and a persistent disk. Briefly show the local quick-start alternative. Avoid showing credentials.

## 0:40 — Ingestion and idempotency

Use Postman requests 02-04. Show `201` for first initiation, `200`/`duplicate:true` on identical retry, then a processed event. Explain the database event-ID primary key, canonical payload hash and single atomic write transaction.

## 1:30 — Filtered transactions

Run request 05. Point out merchant, status, date range, amount sorting and pagination. Show SQL indexes in `schema.sql`; explain that ordering/filtering/pagination occur in SQL rather than Python loops. Mention UTC half-open dates and stable tie-breakers.

## 2:10 — Overdue and settled flows

Run request 06 with a fixed evaluation clock to show the grace-period discrepancy. Run requests 07-08 to settle and fetch details. Show three accepted events despite four deliveries and independent payment/settlement fields. Explain that out-of-order initiation cannot reverse settlement.

## 3:00 — Reconciliation summary

Run request 09, then a summary for the seeded five merchants. Change `group_by` to merchant only and demonstrate per-currency groups. Point out transaction counts, outstanding amounts and distinct discrepancy counts.

## 3:45 — Conflicts and validation

Run request 10: the same event ID with changed amount returns 409 and is retained for investigation, without changing the transaction amount. Run requests 11-12 to show conflicting payment outcomes and settlement of a failed payment. Run requests 13-14 for invalid money precision and the event/conflict audit.

## 4:50 — Evidence and limitations

Show `python -m unittest discover -s tests -v` or the verified CI run. Mention the fixture's 14,870 deliveries, 5,000 transactions, 5 merchants, duplicates and out-of-order data. Explain SQLite's single-writer limitation, the one-instance deployment, absence of partial settlement/refunds and the path to PostgreSQL. Say that `as_of` evaluates the overdue threshold, not historical state.

## 5:40 — Submission links and AI disclosure

Show that deployment data survives a restart if time permits. Provide public repository, live API and recording links in the hiring-team submission, with credentials separately. Mention AI assistance honestly and explain the parts you reviewed/tested yourself.

## Recording quality checklist

- Use a fresh demo database or acknowledge extra demo records from prior runs.
- Do not show an API key, account tokens, billing details or private data.
- All five required endpoints appear in the video, with readable responses.
- The recording's sharing permissions let the reviewer open it.
- Open the actual public URL from a separate browser/session before submitting.
- Do not label a localhost walkthrough as proof of public hosting.
