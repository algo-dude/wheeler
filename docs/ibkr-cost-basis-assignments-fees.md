# IBKR Ledger Ingestion: Cost Basis, Assignments & Fees (Canonical)

This is the canonical reference for Wheeler’s IBKR transaction/ledger ingestion:
- What exists today (Phase 1: API-based executions ingestion + derived activities + cost basis + fees)
- What is missing / known limitations
- The recommended plan for Phase 2 (Flex Web Service ingestion)
- Rationale for “assignment close reason” and where it fits

If you are implementing Flex ingestion or troubleshooting assignment/cost-basis behavior, start here.

---

## Terminology & high-level model

Wheeler effectively has two IBKR-related data flows:

1) **Position sync ("what do I hold now")**
- Reconciles Wheeler’s open positions to IBKR’s current positions list.
- When a position disappears from IBKR’s current positions, Wheeler closes it by presence/absence.
- This is convenient, but it does not tell you *why* it closed (sold vs assigned vs transferred).

2) **Ledger/transactions sync ("what executions happened")**
- Ingests executions (trades/fills) and commissions/fees.
- Derived computations then:
  - infer and store assignment/exercise/expiration events
  - recompute adjusted cost basis
  - roll up fees to Wheeler positions

**Phase 2 (Flex)** should feed into the same ledger tables, then reuse the same derived steps.

---

## Status overview

### Phase 1 (API-based): implemented

Phase 1 uses the IBKR API path (executions + commission reports) and then runs DB-driven computations.

**What Phase 1 provides:**
- Execution ingestion (limited history window)
- Commission/fee tracking (limited detail)
- Heuristic assignment/exercise detection (inferred)
- Cost basis adjustment driven by inferred assignment/exercise events
- Fee aggregation onto Wheeler’s `options` and `long_positions`

### Phase 2 (Flex Web Service): not implemented

Phase 2 is required for:
- full historical backfill (years, not ~days)
- more authoritative labeling of assignments/exercises/corporate actions
- richer fee breakdowns and statement-grade auditability

---

## Phase 1 details (what exists)

### SQLite tables used for the ledger

Phase 1 stores transaction evidence in three tables:

- `ibkr_executions`
  - one row per execution
  - natural uniqueness is `(account, exec_id)`

- `ibkr_commission_reports`
  - one row per execution (linked by `exec_id`)
  - natural uniqueness is `(account, exec_id)`

- `ibkr_activities`
  - derived semantic events such as `ASSIGNMENT`, `EXERCISE`, `EXPIRATION`
  - natural uniqueness is `(account, activity_id)`

These tables are the landing zone for both Phase 1 (API) and Phase 2 (Flex). The recommended Phase 2 approach is to keep Flex ingestion additive and idempotent by upserting into these same tables.

### Derived computations (run after ledger ingestion)

After executions + commission reports exist, Wheeler’s IBKR service runs DB-driven steps:

- **Detect assignment/exercise events**
  - Produces `ibkr_activities` rows.
  - The current implementation infers these from execution patterns.

- **Recompute cost basis**
  - Uses activities to update adjusted cost basis fields (particularly for assignment/called-away situations).

- **Update fee totals**
  - Sums commission/fees and updates position-level totals.

### Phase 1 limitations (important)

The Phase 1 IBKR API approach has inherent gaps:

- **Limited history**
  - Executions are only available over a limited recent window via the current approach.

- **Heuristic assignment detection**
  - Assignment is inferred from patterns (e.g., option disappears + stock trade at strike).
  - This is high-signal for classic covered calls, but can miss edge cases (early assignment, partials, complex rolls) or produce occasional false positives.

- **Incomplete fee detail**
  - Commission reports may not include a complete statement-grade breakdown (regulatory/exchange/clearing/etc.).

Phase 2 (Flex) exists to address these.

---

## Assignment “close reason” (API vs Flex tradeoff)

### What Wheeler can represent today

- Wheeler can already infer and store an assignment event as an `ibkr_activities` row (activity type `ASSIGNMENT`).
- Wheeler does **not** currently store a first-class “close reason” field on `long_positions` (e.g., “ASSIGNED_COVERED_CALL”).
- Position sync closes positions by presence/absence, which cannot identify the reason a position closed.

### Why this matters

Users often want to answer: “Why was this stock position closed?”
- normal sell
- assignment (covered call)
- exercise
- transfer / journal
- manual close

Today, the best available audit trail is: `ibkr_executions` + `ibkr_activities`.

### API-only strategy (works now, inference-based)

An API-only strategy can label “assigned via covered call” by:
1) ingest executions
2) detect assignment activities (already exists)
3) optionally propagate that semantic label into the Wheeler UI/domain (future code work)

Pros:
- no Flex setup required
- already mostly implemented (assignment activity inference exists)

Cons:
- inference can be wrong in some cases
- mapping to specific lots is ambiguous if Wheeler is not lot-based

### Flex strategy (recommended for maximum correctness)

Flex reports are more statement-like and can include explicit assignment/exercise/corporate-action-style entries.

Pros:
- more authoritative labeling
- better long-range backfills and auditability

Cons:
- requires user configuration (token + query id)
- adds an external polling flow and more parsing/mapping work

Pragmatic recommendation:
- keep Phase 1 inference behavior
- implement Phase 2 Flex ingestion for backfill + correctness improvements

---

## Phase 2 (Flex Web Service): recommended design

### Why Flex is needed

Flex should address:
- multi-year transaction history
- explicit assignment/exercise/expiration/corporate actions in report outputs
- richer fee detail

### Endpoint shape

Add a new IBKR service endpoint and a Wheeler Go proxy:

- `POST /api/ibkr/transactions/sync-flex`

The endpoint should:
- fetch a Flex report using the configured token/query id
- parse and upsert rows into the ledger tables
- (optionally) run the same derived steps used after Phase 1 ingestion

For a step-by-step implementer prompt (Python service + Go proxy + tests), see:
- `docs/working_docs/ibkr-flex-phase2-implementation-prompt.md`

### User configuration (minimal)

Flex requires two user-provided values:
- **Flex Web Service token**
- **Flex query id**

Keep configuration options minimal:
- Support environment variables for headless/Docker deployments.
- Optionally support Wheeler Settings for non-CLI users.

Document clearly:
- where in IBKR to generate the token and find the query id
- where in Wheeler to paste them (or which env vars to set)

### Flex query setup (high-level)

At minimum, a Flex query should include sections that cover:
- Trade confirmations / executions
- Cash transactions / exercises / assignments
- Fee / commission breakdown (as available)

### Flex fetch/poll flow (conceptual)

Flex is typically a two-step process:

1) **SendRequest**
- Submit the Flex query with `token` + `queryId`.
- Receive a `referenceCode`.

2) **GetStatement**
- Poll until the statement is ready.
- Download the report content (prefer XML first).

### Parsing plan (XML-first)

Start with XML parsing (most robust for typed fields), then consider CSV later if needed.

Map Flex report sections into existing tables:

- Trade confirmations / executions → `ibkr_executions`
  - preserve `exec_id` (or a stable surrogate if Flex doesn’t provide exec ids for some record types)
  - store the full original record in `raw_json`/`raw_text`

- Commission / fee details → `ibkr_commission_reports`
  - store structured fee detail as `fees_json`

- Cash transactions / exercises / assignments / corporate actions → `ibkr_activities`
  - prefer stable ids from Flex if available (or derive a deterministic id)

### Idempotency strategy

Flex ingestion must be safe to re-run.

- Use upserts into the ledger tables.
- Prefer stable unique keys:
  - executions: `(account, exec_id)`
  - commission reports: `(account, exec_id)`
  - activities: `(account, activity_id)`

If Flex provides no stable id for certain items, define a deterministic `activity_id` derived from:
- account
- event type
- event timestamp
- symbol/conid
- quantity
- amount

### Configuration + storage plan (minimal)

Keep configuration minimal and consistent with existing settings patterns.

Minimum required inputs:
- **Flex Web Service token**
- **Flex query id**

Storage options:
- environment variables (good for headless / docker)
- Wheeler Settings UI (good for non-CLI users)

Regardless of mechanism, document:
- which values users need to retrieve from IBKR
- where to paste them
- how to rotate them

### Data provenance (optional)

If/when it becomes useful to distinguish API vs Flex rows, add a `source` column on ledger tables with values `API` and `FLEX`.

This is not strictly required to implement Flex ingestion, but can simplify debugging.

If you add `source`, prefer defaulting existing rows to `API` and writing Flex-ingested rows as `FLEX`.

---

## Operational constraints

- Keep ledger ingestion **additive and idempotent**.
- Do not introduce production-only DB switching behavior; respect `IBKR_DATABASE_PATH` patterns and shared SQLite usage.
- Prefer using activity rows as audit evidence; avoid mutating cost basis unless explicitly intended (recompute step should remain explicit/opt-in).

---

## Notable open questions for the implementer

- Flex record identifiers: which Flex sections provide stable IDs for executions/activities in practice, and what deterministic fallback strategy is best when they do not?
- Activity typing: which Flex activity codes map cleanly into Wheeler’s activity types (`ASSIGNMENT`, `EXERCISE`, `EXPIRATION`, `DIVIDEND`, `FEE`, `TRANSFER`, `OTHER`)?
- Cost basis recompute semantics: should the Flex ingestion endpoint automatically call recompute/fees updates, or should it only ingest and leave derived steps as separate explicit calls?
- Multi-account behavior: how to scope ingestion when users have multiple IBKR accounts?
