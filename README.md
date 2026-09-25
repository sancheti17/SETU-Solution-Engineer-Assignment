# Payment Events & Reconciliation Service

A small Flask + SQLite service for the Solutions Engineer take-home. It accepts payment lifecycle evidence, preserves accepted events, projects transaction state, and exposes SQL-backed operational and reconciliation APIs.

**Submission status:** source code, generated sample data, automated tests, Postman collection, and deployment configuration are included. A public repository, public service URL, and recorded demo have **not** been created by this package. Publish/deploy/record before claiming the full hosted submission is complete. The local path below is the assignment's runnable fallback.

## 1. Run locally

Requirements: Python 3.12+ (3.13 used for sandbox validation) or Docker with Compose. No external database account is needed.

### Python — quickest path

From this repository's root:

```sh
python -m venv .venv
# macOS/Linux:
. .venv/bin/activate
# Windows PowerShell instead: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m tools.seed --expect-conflicts 20
python -m flask --app wsgi:app run --port 8000
```

The release archive includes `sample_events.json`. If checking out source without it, run `python -m tools.generate_data` before seeding. The generator refuses to overwrite an existing file; use `--output another-file.json` for another copy. Database and tables initialize automatically. `data/payments.db` persists across restarts.

In another terminal:

```sh
curl http://localhost:8000/health
curl 'http://localhost:8000/transactions?page_size=3'
python -m unittest discover -s tests -v
```

The default local server has no authentication and binds to localhost. Do not expose the Flask development server publicly. For a non-Docker Unix deployment, use Gunicorn:

```sh
export DATABASE_PATH=data/payments.db
export API_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
export REQUIRE_API_KEY=true
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 30 wsgi:app
```

Pass `X-API-Key` on all application routes when `API_KEY` is set. `/health` is deliberately unauthenticated. `.env.example` is a reference, not an automatically loaded Python configuration; export variables in your shell. Keep credentials out of Git and recordings. Production startup refuses a required API key shorter than 24 characters.

### Docker Compose

```sh
docker compose up --build -d --wait
docker compose exec -T api python -m tools.seed --expect-conflicts 20
curl http://localhost:8000/health
docker compose logs -f api
```

Compose binds only `127.0.0.1:8000`, persists SQLite on a named volume, and runs the container as a non-root user. `docker compose down` keeps the volume. **Do not add `-v` unless you intentionally want to delete all database data.** Docker/Compose configuration is supplied but was not executed in the sandbox, which has no Docker runtime. CI includes a container smoke-test job for independent verification.

## 2. Architecture

```text
Multiple event producers
        |
        v
Flask: validation, shared API key, consistent errors, request IDs
        |
        v
BEGIN IMMEDIATE -> accepted event OR rejected-payload audit
        |                   |
        v                   v
transaction projection   durable conflict record
        \___________________/
                 |
                 v
SQLite on persistent local disk (foreign keys, WAL, FULL sync)
                 |
                 v
SQL filtering / pagination / aggregation / discrepancies
```

- `payments/domain.py`: canonicalization, idempotency, ingestion, deterministic projection.
- `payments/queries.py`: allowlisted SQL query construction and serialization.
- `payments/db.py`: connection lifecycle, explicit transactions and schema initialization.
- `payments/__init__.py`: HTTP boundary, authentication, error handling and routing.
- `payments/schema.sql`: reviewable schema, constraints and indexes; `PRAGMA user_version=1`.
- `tools/`: repeatable data generation, seeding and collection verification.
- `tests/test_api.py`: endpoint, concurrency, rollback and full-volume correctness tests.

### Tables

| Table | Purpose | Important constraints |
|---|---|---|
| `merchants` | Merchant identity and first-observed name | Primary key `merchant_id` |
| `transactions` | Current payment and settlement projection | PK `transaction_id`, merchant FK, exact minor-unit amount, constrained statuses |
| `payment_events` | Canonical accepted event history | PK `event_id`, transaction FK, canonical payload + SHA-256 hash |
| `ingestion_conflicts` | Valid-but-rejected identity/payload attempts | Transaction FK, unique `(event_id,payload_hash,reason)` |

The event history is append-only through this API; a database administrator can still modify the database. This is not cryptographic tamper-proof storage. Rejected malformed requests are not saved as business events. Exact transport retries return a receipt but do not create a separate delivery-attempt table.

### Why SQLite rather than a larger stack?

It makes the submission reproducible without another service while still exercising real SQL, constraints, indexes and transactions. SQLite WAL allows concurrent readers with a single writer. `BEGIN IMMEDIATE` serializes competing writers before the idempotency check. The unique event key remains a final database guard. Changes to event history, projection and conflict audit commit atomically.

This is intentionally **one service instance on a local persistent filesystem**, not a high-throughput distributed payment processor. A PostgreSQL migration with per-transaction row locking, database-native conflict handling and a proper migration tool would be the next step for sustained concurrent writes or multiple instances. Do not mount the SQLite file on a network filesystem for multi-host use.

## 3. State and idempotency decisions

### Stable identities and exact money

Every event must contain exactly the eight fields shown below. IDs may be UUIDs or 1-128 character ASCII identifiers starting with a letter/digit and containing letters, digits, `.`, `:`, `_`, `-`. Merchant names are required, printable and <=200 characters. The first observed merchant name is retained; later names remain in each event's payload, but this is not a merchant master-data update API.

`amount` may be a JSON number or decimal string. It must be positive, <=9,999,999,999.99, and exactly representable in two decimal places. Supported currencies are explicitly **INR, USD, EUR**; all use two minor-unit digits. Other currencies are rejected rather than assuming the wrong exponent. HTTP JSON decimal numbers are parsed with `Decimal`, stored as integer minor units and returned as a decimal string plus `amount_minor`. No binary-float arithmetic is used in business calculations.

The first accepted arrival fixes a transaction's merchant, currency and amount. A later different value is rejected with `409 transaction_mismatch`. There is no FX conversion, partial settlement, refund, chargeback or payment-attempt ID model.

### Idempotency

1. Normalize timestamps to UTC microseconds, currency to uppercase, money to two decimals and merchant-name edge whitespace.
2. Hash the complete canonical payload, including IDs and merchant name.
3. Within one write transaction, check the unique `event_id`.
4. Same ID + same canonical payload: `200`, `duplicate: true`; no business-state mutation.
5. Same ID + different payload: `409 idempotency_key_reuse`; retain the rejected payload once and flag the canonical transaction for investigation.
6. New valid ID: insert the immutable event, update the projection and return `201`.

If a reused event ID points at another transaction, the conflict is associated with the **original** transaction; the rejected target ID is recorded, but no phantom target transaction is created. Repeated identical rejected attempts do not inflate the audit. A later corrected event with a new valid payload can be accepted; the historical audit flag remains. Conflict resolution/acknowledgement is not implemented.

### Out-of-order delivery and status

For identity-consistent events, projection uses accumulated evidence, not arrival order. An initiation received after settlement cannot downgrade a completed payment. Payment `processed` and `failed` evidence together is **conflicted**, not silently last-write-wins. This treats contradictory outcomes as a discrepancy instead of guessing which producer is authoritative.

| Observed evidence | `payment_status` | `settlement_status` | `status` filter |
|---|---|---|---|
| Initiated only | initiated | unsettled | initiated |
| Processed, no settlement | processed | unsettled | processed |
| Processed + settlement | processed | settled | settled |
| Failed, no processed | failed | depends on evidence | failed |
| Both processed and failed | conflicted | depends on evidence | conflicted |
| Settlement only | unknown | settled | unknown |

A new distinct settlement event is retained, but >1 settlement IDs is a **possible duplicate settlement** discrepancy, not proof that money was transferred twice. Distinct repeated processed events alone do not create a conflict. Event timestamps are evidence timestamps, not transition authority.

## 4. API reference

All successful bodies are JSON. Error shape: `{"error":{"code":"...","message":"...","request_id":"..."}}`. Conflict errors also include `event_id` and canonical `transaction_id`. All responses carry `X-Request-ID`, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`.

Common errors: `400` malformed JSON/duplicate JSON keys, `401` invalid key, `404` missing transaction, `409` identity/payload conflict, `413` body >16 KiB, `415` non-JSON event body, `422` validation error, `503` database write contention (`Retry-After: 1`). Retry a transient failure with backoff **and the same event ID**.

### POST /events

```json
{
  "event_id": "demo-event-1",
  "event_type": "payment_initiated",
  "transaction_id": "demo-transaction-1",
  "merchant_id": "merchant_2",
  "merchant_name": "FreshBasket",
  "amount": "15248.29",
  "currency": "INR",
  "timestamp": "2026-01-08T12:11:58.085567+00:00"
}
```

`event_type`: `payment_initiated`, `payment_processed`, `payment_failed`, `settled`. Timestamp requires a timezone and a year in 1970-2100. Offsets are normalized to UTC; precision beyond microseconds is not retained. Synthetic/past/future event times within that range are allowed; no clock-skew rejection is applied.

First acceptance (`201`):

```json
{"event_id":"demo-event-1","transaction_id":"demo-transaction-1","duplicate":false}
```

An identical replay returns the same identifiers with `duplicate:true` and `200`. Both include a relative `Location` pointing to the transaction details.

### GET /transactions

| Query parameter | Default / accepted values |
|---|---|
| `merchant_id` | Optional exact merchant identifier |
| `status` | Optional `unknown`, `initiated`, `processed`, `failed`, `conflicted`, `settled` |
| `currency` | Optional `INR`, `USD`, `EUR` |
| `from`, `to` | Optional timezone-aware datetimes; inclusive lower, exclusive upper |
| `page`, `page_size` | 1 and 50; size 1-200, page 1-1,000,000 |
| `sort_by` | `created_at` (default), `last_event_at`, `amount`, `transaction_id` |
| `sort_order` | `desc` (default), `asc` |

Date filters apply to the **earliest observed event timestamp for the transaction**, not ingestion time. This can move earlier when older evidence arrives. Equal sort values use `transaction_id` as a deterministic tie-breaker. `amount` ordering compares minor-unit numeric values; add a currency filter for financially meaningful comparisons.

Example:

```sh
curl 'http://localhost:8000/transactions?merchant_id=merchant_2&status=settled&from=2026-01-01T00:00:00Z&to=2026-02-01T00:00:00Z&page=1&page_size=20&sort_by=created_at&sort_order=desc'
```

Response: `{"data":[...],"pagination":{"page":1,"page_size":20,"total":...}}`. Each transaction has amount/string + minor units, merchant object, current statuses, evidence counters, event count, first/last event times, earliest processed/settled times and conflict count. IDs are stable; projected dates and statuses may change as evidence arrives.

### GET /transactions/{transaction_id}

Returns the transaction plus `events` and `conflicts`. Each event includes canonical `payload`, payload hash, `occurred_at` and `received_at`. Conflicts include reason, rejected payload and detection time.

Both histories are paginated independently to avoid unbounded responses: `history_page`, `history_page_size`, `conflict_page`, `conflict_page_size`; defaults 1/100, sizes <=200. Pagination metadata includes each history's total. Events sort by `(occurred_at,event_id)`; conflicts by audit sequence. Iterate pages to retrieve full history. No transaction returns `404`.

### GET /reconciliation/summary

Supports the common merchant/status/currency/date filters and `page`/`page_size`. `group_by` is any nonempty distinct comma-separated subset of `merchant,date,status`; default all three. **Currency is always an additional group**, so no mixed-currency totals appear. Date is UTC transaction-created date, not settlement date.

```sh
curl 'http://localhost:8000/reconciliation/summary?group_by=merchant,date,status&as_of=2026-02-15T00:00:00Z&page_size=200'
```

Each group returns:

- `transaction_count`, `total_amount_minor` / `total_amount`.
- `processed_amount_minor` / `processed_amount`: amount of transactions with processed evidence.
- `settled_amount_minor` / `settled_amount`: amount of transactions with any settlement evidence, even if failed.
- `outstanding_amount_minor` / `outstanding_amount`: processed evidence with no settlement, including within grace.
- `discrepancy_count`: unique transactions matching one or more discrepancy predicates; multiple reasons do not double-count.

Amounts count each transaction once per metric, not once per event. Processed and settled totals can overlap and are not independent buckets to add together. An anomalous settlement is still evidence; the summary is not a certified bank-ledger balance. Pagination total is `total_groups`.

### GET /reconciliation/discrepancies

Same common filters and pagination. Optional `reason` limits to one reason; every returned row includes all matching `reasons`.

| Reason | Predicate |
|---|---|
| `processed_not_settled` | Processed exists, no settlement, earliest processed time <= evaluation clock minus grace |
| `settled_failed_payment` | Both failed and settlement evidence |
| `settled_without_processed` | Settlement without processed evidence |
| `conflicting_payment_outcomes` | Both processed and failed evidence |
| `multiple_settlement_events` | More than one accepted settlement ID |
| `ingestion_conflict` | At least one retained rejected-payload/identity conflict |

Both reconciliation endpoints accept `grace_hours` (integer 0-8760; default 24 or environment setting) and `as_of` (UTC-normalized evaluation clock; default now). Exactly reaching the grace boundary is overdue. Responses expose `as_of`, `grace_hours`, and `cutoff` for reproducibility.

**Important:** `as_of` changes the overdue threshold only. It is **not** a historical snapshot or event cutoff. Both endpoints use all currently accepted evidence, including events whose timestamp is later than `as_of`. Non-overdue conflict predicates are flagged immediately; late-arriving processing evidence can clear `settled_without_processed`.

```sh
curl 'http://localhost:8000/reconciliation/discrepancies?reason=processed_not_settled&as_of=2026-02-15T00:00:00Z&grace_hours=24&page_size=20'
```

Unknown parameters, repeated query keys, invalid sort names and inverted date ranges return `422` rather than being silently ignored. URL-encode `+` as `%2B` in timezone offsets in query strings, or use `Z` as above.

## 5. Sample data and reproducibility

No actual supplied `sample_events.json` attachment was available, so this submission uses its own deterministic generator (`seed=42`). The archive includes the generated JSON and manifest.

| Item | Count |
|---|---:|
| Merchants | 5 |
| Transactions | 5,000 |
| Accepted unique events | 13,500 |
| Exact duplicate deliveries | 1,350 |
| Reused-ID conflicting deliveries | 20 |
| Total records in sample_events.json | **14,870** |

Transactions include 2,000 normal successes and 500 each of failed, processed-unsettled, failed-but-settled, contradictory outcomes, multiple settlement IDs, and settlement-only records. Currencies mix INR/USD/EUR. Events are shuffled to exercise out-of-order arrival; reused-key conflicting attempts are appended after originals to make first-writer outcomes reproducible. Names are synthetic. All primary event times are in January 2026.

First seed on a clean database: **13,500 HTTP-equivalent `201`, 1,350 `200`, 20 expected `409`**. Re-seeding: **14,850 `200`, 20 `409`**, with unchanged accepted history/audit counts. Default full dataset has **2,505 unique discrepant transactions** at `as_of=2026-02-15T00:00:00Z`, grace 24. A Postman run intentionally adds separate demo transactions, so aggregate counts change afterward.

Direct seeding batches 500 events per transaction through the same domain function used by the endpoint; it is not an HTTP throughput benchmark. To demonstrate full HTTP ingestion:

```sh
# Export API_KEY first if the target requires it; keep the actual key private.
python -m tools.seed --url http://localhost:8000 --expect-conflicts 20
```

`--expect-conflicts` acknowledges this fixture's deliberately rejected events. Unexpected response codes/counts make the CLI fail. Retryable HTTP/network failures use bounded backoff. Direct batch loads are atomic per batch, not for the entire file. Re-running safely resumes accepted IDs.

## 6. SQL and performance awareness

Queries never load all events into Python for filtering or aggregations. Python only serializes the selected page. Dashboard summary aggregates the transaction projection, avoiding event-join fanout and repeat-counted money.

| Index | Query supported |
|---|---|
| `(created_at,transaction_id)` | Date ordering/filtering and stable pagination |
| `(merchant_id,created_at,transaction_id)` | Merchant transaction list |
| `(status,created_at,transaction_id)` | Status transaction list |
| `(merchant_id,status,created_at,transaction_id)` | Combined operational filters |
| Events `(transaction_id,occurred_at,event_id)` | Paginated ordered history |
| Conflicts `(transaction_id,conflict_id)` | Paginated rejected-payload audit |
| Partial `(processed_at,transaction_id)` where processed + no settlement | Specific overdue-unsettled query |

Sort expressions and grouping dimensions come only from allowlists; values are SQL-bound parameters. Read responses use one read transaction for count + page consistency within that response. Offset pagination is chosen for reviewer simplicity; it can be slow at deep offsets and can shift across requests during concurrent ingestion. For large installations, add cursor pagination, workload-based indexes and rollups. Aggregations and the all-reasons OR query can scan the projection; an index for every sort/reason would increase write cost without evidence it is needed at this scale.

The test suite verifies that the merchant+status+date list uses the intended composite index and that event-derived counts/dates match materialized transaction state for every sample transaction. `verification/` contains run evidence and local query timings when present in the release. Sandbox timings are observations, not a production SLA or a scalability claim. Integer SUM has SQLite's signed-64-bit limit; a substantially larger monetary dataset needs an explicit overflow policy or a larger numeric database type.

## 7. Tests and Postman

```sh
python -m unittest discover -s tests -v
```

Tests exercise successful flows, canonical retries, reused-key conflicts, identity mismatches, all arrival permutations of a normal lifecycle, contradictory outcomes, grace boundaries, duplicate settlement evidence, filtering/pagination/sorting, exact money, SQL grouping, auth, malformed bodies, concurrency, busy retries, rollback, persistence and the full 14,870-record fixture.

### Verified on September 24, 2026

- **31 automated tests passed**, with no failures or errors in the final run.
- All **14,870 deliveries** passed through the authenticated Flask API served by Gunicorn: 13,500 created, 1,350 exact retries, 20 expected conflicts. This sequential sandbox run took 51.272 seconds; it is not a throughput or durability guarantee for another host.
- The collection's **14 requests and 14 JavaScript test groups passed** using the included Node smoke runner (not the Postman GUI/Newman).
- Database integrity was `ok`, foreign-key violations were zero, and all 5,000 transactions survived a Gunicorn restart.
- Query plans used the intended merchant/status composite index and overdue partial index. Timings and full evidence are included in `verification/`.

The sandbox's TCP loopback bind failed with `OSError: [Errno 99] Cannot assign requested address`. This was an environment limitation; integration verification used real HTTP over a local **Unix domain socket** instead. Normal TCP/HTTPS hosting and Docker still require verification on your machine/provider. The included runner supports an optional `SOCKET_PATH` environment variable for this diagnostic mode. An initial fixture-count assertion was also corrected before the final successful runs; no failed run is presented as passing.

Import `postman/Setu-Reconciliation.postman_collection.json` into Postman. Set collection variables `baseUrl` and `apiKey`. Run all **14 requests in order**; the initiation request creates new IDs for each run. The collection covers every required endpoint and includes assertions for successful settlement, exact retries, grace expiry, `409` conflicts and `422` invalid precision. Do not run later requests without request 02 first. API keys are never embedded in the collection.

For a dependency-free command-line check of this exact collection's requests and JavaScript scripts (Node 20+):

```sh
node tools/run_collection.mjs http://localhost:8000
```

This small runner implements only the Postman scripting features used here; it is not Newman or proof of testing inside the Postman GUI. Import the collection and run it in your own Postman environment before submitting the recording. Repeated demo runs retain prior demo data; use a fresh database for deterministic presentation counts.

## 8. Public deployment — Render

The supplied `render.yaml` creates a native Python web service with Gunicorn and a persistent disk at `/var/data`. SQLite must remain on that disk, including `-wal`/`-shm` sidecar files. A free ephemeral filesystem is not a durable deployment. Render disks require a paid service and constrain the service to one instance; disk-backed deployments do not provide zero-downtime rollout. The configured `starter` plan name remains an accepted legacy Blueprint alias; review current provider pricing before creating paid resources.

Deployment steps:

1. Create your public GitHub/GitLab repository and push the contents of this folder, including generated sample data. Do not commit `.env`, database files, secrets or private account details.
2. In your Render account, create a Blueprint from that repository using `render.yaml`. Review the paid service/disk configuration before confirming. If setting up manually: Python runtime, build `pip install -r requirements.txt`, start command from `render.yaml`, health path `/health`, persistent disk `/var/data`, database path `/var/data/payments.db`.
3. Choose a supported Python 3.12+ runtime (3.13 recommended here). Keep one service instance. The Blueprint generates an API key and requires it; retain that key privately for the reviewer.
4. Wait for a successful deployment, then check the **actual issued service URL** at `/health`. No placeholder hostname is a working deployment.
5. Export that real origin as `BASE_URL` and the generated key as `API_KEY` locally. Seed over HTTPS: `python -m tools.seed --url "$BASE_URL" --expect-conflicts 20`. Alternatively, run the direct seed command in a runtime shell that can access the mounted disk. Do not seed during build/pre-deploy: the persistent disk is only available at runtime.
6. Run `node tools/run_collection.mjs "$BASE_URL"`, then import/run Postman against that same origin. Restart the service and confirm the transaction history survives.
7. Put the real base URL, tested deployment date, repository link and demo recording link in your submission. Share reviewer credentials privately, not in the public README.

TLS terminates at the hosting platform. The single shared API key is a demo operations key, **not** tenant isolation or signed webhook authentication. Add per-source signatures/replay policies, tenant RBAC, rate limiting and alerting before accepting real partner payments. No background reconciliation job is needed here: the grace-based discrepancy query changes with its evaluation time.

Back up with SQLite's online backup mechanism rather than copying only the live `.db` file while WAL writes are active. No backup scheduler, restore automation or point-in-time recovery has been implemented. New schema changes need explicit sequential migrations; v1 bootstrap is not a general migration framework.

Official documentation consulted: Flask production deployment guidance; SQLite WAL and transaction semantics; Render Blueprint, persistent-disk and compute-plan documentation. These are operational choices, not proof that this package has been hosted.

## 9. Tradeoffs and next steps

- No queue, Redis, ORM or microservices: bounded assignment scope and directly inspectable SQL.
- Single writer and a five-second lock wait; requests return retryable 503 instead of waiting indefinitely.
- Evidence-based flags, not an automatic financial settlement/correction engine. There is no authoritative bank settlement reference or full ledger.
- Arrival order is irrelevant for valid same-identity evidence; conflicting immutable attributes are explicitly first-valid-arrival wins.
- Shared-key authentication has no merchant-scoped access. Only use synthetic data for a public demo.
- Paginated histories preserve all stored events, but a single detail response is deliberately bounded.
- No artificial latency/throughput promises. Measure the target host before capacity claims.
- Docker/Render/CI configuration needs real-provider verification. The local API and test evidence are the verified portion.

## 10. AI disclosure and final checklist

**AI assistance disclosure:** SuperApp was used to draft the implementation, schema, tests, sample generator, Postman collection, deployment configuration and documentation, and to run sandbox validation. The candidate should personally review the code, run the service, verify the hosted deployment and record the walkthrough. Do not claim independent authorship or deployment/testing you did not perform. Add any other AI tools you use.

Before submitting:

- [ ] Public repository exists and can be cloned without special access.
- [ ] Hosted API really responds, has persistent storage and has been seeded.
- [ ] Reviewer can use the actual API key supplied privately.
- [ ] All tests and the Postman collection pass in your environment.
- [ ] Screen recording demonstrates all APIs and edge cases; link is shareable.
- [ ] README/submission includes actual deployment and recording details.
- [ ] AI disclosure is accurate and you can explain all tradeoffs.

See `DEMO_WALKTHROUGH.md` for a short recording outline. The assignment's three-day deadline begins when the hiring team actually shared it; this package does not assume a receipt time or submit anything on your behalf.
