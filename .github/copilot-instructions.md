# Copilot Instructions for Wheeler

# Subagent Instructions BEGIN
 
## Agent Role: ORCHESTRATOR ONLY
 
You are the **orchestrating agent**. You **NEVER** read files or edit code yourself. ALL work is done via subagents.
 
---
 
### ⚠️ ABSOLUTE RULES
 
1. **NEVER read files yourself** — spawn a subagent to do it
2. **NEVER edit/create code yourself** — spawn a subagent to do it
3. **ALWAYS use default subagent** — NEVER use `agentName: "Plan"` (omit `agentName` entirely)
 
---
 
### Mandatory Workflow (NO EXCEPTIONS)

Note that the example below is IN SERIAL — one subagent after another. You MUST run subagents in serial.  You can spawn as many subagents as needed, but they must run one after another, passing outputs back to you before you make a decision on spawning another.  
Multiple subagents CANNOT run in parallel.

---

### Docs Convention

- `docs/working_docs/` is for persistent prompts/handoff docs intended for future agents/sessions.
- `docs/SubAgent docs/` is for transient artifacts produced within a single chat/subagent workflow.

```
User Request
    ↓
SUBAGENT #1: Research & Spec
    - Reads files, analyzes codebase
    - Creates spec/analysis doc in docs/SubAgent docs/
    - Returns summary to you
    ↓
YOU: Receive results, spawn next subagent
    ↓
SUBAGENT #2: Implementation (FRESH context)
    - Receives the spec file path
    - Implements/codes based on spec
    - Returns completion summary
```
 
---
 
### runSubagent Tool Usage
 
```
runSubagent(
  description: "3-5 word summary",  // REQUIRED
  prompt: "Detailed instructions"   // REQUIRED
)
```
 
**NEVER include `agentName`** — always use default subagent (has full read/write capability).
 
**If you get errors:**
- "disabled by user" → You may have included `agentName`. Remove it.
- "missing required property" → Include BOTH `description` and `prompt`
 
---
 
### Subagent Prompt Templates
 
**Research Subagent:**
```
Research [topic]. Analyze relevant files in the codebase.
Create a spec/analysis doc at: docs/SubAgent docs/[NAME].md
Return: summary of findings and the spec file path.
```
 
**Implementation Subagent:**
```
Read the spec at: docs/SubAgent docs/[NAME].md
Implement according to the spec.
Return: summary of changes made.
```
 
---
 
### What YOU Do (Orchestrator)
 
✅ Receive user requests  
✅ Spawn subagents with clear prompts  
✅ Pass spec paths between subagents  
✅ Run terminal commands  
 
### What YOU DON'T Do
 
❌ Read files (use subagent)  
❌ Edit/create code (use subagent)  
❌ Use `agentName: "Plan"` (always omit it)  
❌ "Quick look" at files before delegating  


# Subagent Instructions END


## Big Picture

Wheeler is a Go web app for tracking options + stock portfolios (wheel strategy) with a SQLite datastore and a server-rendered UI.

High-level flow:
`main.go` → `internal/web/server.go` (routes/templates) → `internal/models/*` (services/domain) → `internal/database/db.go` + `internal/database/schema.sql` (SQLite)

Key areas:
- Go web/UI: `internal/web/` (handlers, templates, static assets)
- Domain/services: `internal/models/` (e.g., `OptionService`, `SymbolService`, `TreasuryService`)
- Market data: `internal/polygon/` (Polygon.io client/service)
- IBKR integration: `internal/web/ibkr_handlers.go` (proxy) + `ibkr_service/` (Python/FastAPI)

## Dev Workflows

Run locally:
```bash
go run main.go  # http://localhost:8080
```

Notes:
- On macOS, local testing does not require Docker. Use `go run main.go` for the Go app.
- To run the IBKR service locally without Docker (optional), use:
    ```bash
    ./ibkr_service/run_local.sh
    ```
- SQLite uses `github.com/mattn/go-sqlite3` (CGO). Local builds need a working C toolchain.
- `go.mod` module path is `stonks` and declares Go `1.24.4`.

Build:
```bash
go build . && ./wheeler
```

Run tests:
```bash
go test ./...
go test ./test/...  # integration-style tests
```

Chart data structure suite:
```bash
./test/run_chart_tests.sh
```

Docker (Go app + IBKR service):
```bash
docker compose up -d  # typically exposes Wheeler at http://localhost:8077
```

### Production/Remote Docker Compose (GHCR images)

If you are deploying on a remote host (Unraid/Linux/etc), prefer using the published GHCR images instead of `build:`.

```yaml
services:
    wheeler:
        image: ghcr.io/algo-dude/wheeler:latest
        ports:
            - "8077:8080"
        volumes:
            - ./data:/app/data
        depends_on:
            - ibkr-service
    ibkr-service:
        image: ghcr.io/algo-dude/wheeler-ibkr-service:latest
        volumes:
            - ./data:/app/data
        environment:
            - IBKR_DATABASE_PATH=/app/data/wheeler.db
```

Note: pin `IBKR_DATABASE_PATH` so the IBKR service writes to the expected SQLite DB.

(`Makefile` exists but is tailored to a specific Linux environment; for most dev, prefer the `go ...` commands above.)

App-driven DB workflows:
- Generate sample data: Help → Tutorial → “Generate Test Data” (also available via `POST /api/generate-test-data`).
- Manage DBs/backups: Admin → Database (create/switch/delete + manual backups).

## Repo Conventions (Follow These)

- Handler pattern: page/API handlers live in `internal/web/*_handlers.go` and log with a `[PAGE_NAME]` prefix; they typically fetch via services and render via `s.renderTemplate(...)`.
- Routing: routes are registered on `http.DefaultServeMux` in `internal/web/server.go` (`setupRoutes()` via `http.HandleFunc`). Add/adjust endpoints there.
- Templates: `internal/web/templates/` uses Go templates; shared partials start with `_` (e.g., `_navigation.html`, `_symbol_modal.html`).
- Template funcs: see `internal/web/server.go` for custom helpers like `add`, `mul`, `div`, `replace`, `groupByExpiration`.
- Static JS/CSS: `internal/web/static/js/` (navigation state, symbol modal, table sorting) and `internal/web/static/css/styles.css` (dark theme).

## Data & SQLite

- Schema is the single source of truth: `internal/database/schema.sql` (embedded by Go code).
- Table keying convention: transactional tables use `INTEGER PRIMARY KEY AUTOINCREMENT`; reference tables use natural keys (e.g., `symbols.symbol`, `treasuries.cuspid`).
- Data integrity: schema uses foreign keys + uniqueness constraints; update `schema.sql` first when changing data shape.
- SQLite runtime config: connections enable WAL, foreign keys, and a busy timeout (see `internal/database/db.go`); small schema evolutions are handled in `runMigrations()`.
- Multiple DBs: active DB is tracked by `./data/currentdb` (tests use their own `integration_test.db`).

## External Dependencies

- SQLite driver: `github.com/mattn/go-sqlite3` (Go) and direct SQLite access from `ibkr_service/` (Python) when using IBKR sync.
- Charts/UI: server-rendered templates + Chart.js (data passed via template rendering).
- Market data: Polygon.io client/service in `internal/polygon/` (API key configured via Settings UI / env).

## IBKR Service Integration

Architecture: Wheeler (Go) → HTTP proxy handlers → IBKR Service (Python/FastAPI) → `ib_async` → TWS/Gateway.

IBKR service entrypoints:
- API: `ibkr_service/main.py` (`/api/ibkr/test`, `/api/ibkr/sync`, `/api/ibkr/status`, `/api/ibkr/greeks`)
- Sync logic: `ibkr_service/sync_service.py` writing directly to the shared SQLite

Configuration is via `IBKR_*` env vars (host/port/client id/db path) and Wheeler settings; see `ibkr_service/README.md` for defaults.
