# Wheeler IBKR Service

A Python microservice for integrating Interactive Brokers with Wheeler portfolio tracking.

## Overview

This microservice connects to Interactive Brokers TWS (Trader Workstation) or IB Gateway using the `ib_async` library to sync positions with Wheeler's SQLite database. It also supports IBKR Flex Web Service for importing historical transaction data.

## Features

- **Connection Management**: Connect/disconnect from TWS/Gateway
- **Position Sync**: Import stock and options positions from IBKR
- **Flex Query Sync**: Import historical transactions, assignments, exercises, and fees via Flex Web Service
- **Status Monitoring**: Check connection status and sync history
- **Shared Database**: Direct access to Wheeler's SQLite database
- **Greeks & Vol Surface**: Wheeler UI now displays option Greeks (delta, gamma, theta, vega, rho) for synced positions and plots a simple volatility surface for owned strikes/expirations using Polygon snapshots when available.

### Current Scope & Pending Work

- The latest changes add IBKR Greeks/IV sourcing and UI source tagging.
- Flex Web Service integration provides statement-level data (assignments, exercises, expirations, fees).
- Cost basis construction from IBKR transactions is **not yet implemented** and needs a follow-up issue/implementation.

## Requirements

- Python 3.10+
- Interactive Brokers TWS or IB Gateway
- API connections enabled in TWS/Gateway

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `IBKR_TWS_HOST` | `127.0.0.1` | TWS/Gateway hostname |
| `IBKR_TWS_PORT` | `7497` | TWS/Gateway port |
| `IBKR_CLIENT_ID` | `1` | Unique client ID |
| `IBKR_DATABASE_PATH` | *(unset)* | Optional explicit path to Wheeler's SQLite database. If unset, the service reads `data/currentdb` and writes to `data/<active_db>` |
| `IBKR_DATA_DIR` | *(unset)* | Optional override for the directory containing `currentdb` + DB files (defaults to `./data` in the container or `../data` in the repo) |
| `WHEELER_DATA_DIR` | *(unset)* | Alias for `IBKR_DATA_DIR` |
| `IBKR_FLEX_TOKEN` | *(unset)* | Flex Web Service token from IBKR Account Management |
| `IBKR_FLEX_QUERY_ID` | *(unset)* | Flex Query ID from IBKR Account Management |
| `IBKR_SERVICE_HOST` | `0.0.0.0` | Service bind address |
| `IBKR_SERVICE_PORT` | `8081` | Service port |

## Common Ports

- **7497**: TWS Paper Trading
- **7496**: TWS Live Trading
- **4002**: IB Gateway Paper Trading
- **4001**: IB Gateway Live Trading

## API Endpoints

### Health Check
```
GET /
GET /health
```

### Test Connection
```
POST /api/ibkr/test
```
Tests connection to TWS/Gateway. Returns account info if successful.

### Sync Positions
```
POST /api/ibkr/sync
```
Syncs all positions from IBKR to Wheeler database.

### Get Status
```
GET /api/ibkr/status
```
Returns current connection status and last sync results.

### Get Greeks for Owned Options
```
GET /api/ibkr/greeks
```
Returns option Greeks and implied volatility for positions currently held in IBKR. If the service cannot connect to TWS/Gateway, an empty list is returned with an error message so Wheeler can fall back to Polygon data.

### Disconnect
```
POST /api/ibkr/disconnect
```
Disconnects from TWS/Gateway.

---

## Flex Query Support

### What Are Flex Queries?

IBKR Flex Web Service is a powerful API for retrieving historical account data. Unlike the real-time API (which requires TWS/Gateway running), Flex Queries can pull statement-level data directly from IBKR servers.

**Key benefits:**
- **No TWS/Gateway required**: Works without running trading software
- **Historical data**: Access past transactions, not just current positions
- **Statement-level events**: Captures assignments, exercises, expirations, and fees
- **Complementary to real-time sync**: Use both together for complete data

### Data Provided by Flex Queries

| Data Type | Description | Maps To |
|-----------|-------------|---------|
| Trades | Execution records (buys/sells) | `ibkr_executions` |
| Commissions | Fee details per trade | `ibkr_commission_reports` |
| Cash Transactions | Dividends, fees, withholding tax | `ibkr_activities` |
| Corporate Actions | Assignments, exercises, expirations | `ibkr_activities` |

### Flex Query Endpoints

#### Sync Transactions from Flex
```
POST /api/ibkr/transactions/sync-flex
```
Triggers a Flex Query sync. IBKR may take 30-60 seconds to generate the report.

**Request Body (optional):**
```json
{
  "token": "override_token",
  "query_id": "override_query_id",
  "start": "2024-01-01",
  "end": "2024-12-31",
  "dry_run": false
}
```

**Response:**
```json
{
  "success": true,
  "synced_at": "2024-12-30T10:30:00",
  "executions_upserted": 150,
  "commission_reports_upserted": 150,
  "activities_upserted": 45,
  "warnings": []
}
```

#### Get Flex Sync Status
```
GET /api/ibkr/flex/status
```
Returns the current status of Flex sync and last sync results.

### Example Usage

```bash
# Sync using environment variables for credentials
curl -X POST http://localhost:8081/api/ibkr/transactions/sync-flex

# Sync with explicit credentials
curl -X POST http://localhost:8081/api/ibkr/transactions/sync-flex \
  -H "Content-Type: application/json" \
  -d '{"token": "your_flex_token", "query_id": "your_query_id"}'

# Dry run (parse but don't save)
curl -X POST http://localhost:8081/api/ibkr/transactions/sync-flex \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```

### Safe to Run Both Syncs

You can safely run both real-time API sync and Flex sync:
- All upserts use unique keys (exec_id, activity_id) to prevent duplicates
- The `source` column distinguishes data origin: `API` vs `FLEX`
- Flex data complements real-time data with historical events

### Polling Behavior

IBKR Flex Web Service works in two steps:
1. **SendRequest**: Submit query, receive a reference code
2. **GetStatement**: Poll until report is ready (may take 30-60 seconds)

The service handles polling automatically with configurable timeouts.

---

## Running Locally

```bash
cd ibkr_service
pip install -r requirements.txt
python main.py
```

## Docker Deployment

The service is included in Wheeler's Docker Compose configuration:

```bash
docker-compose up -d
```

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Wheeler Go    │────▶│  IBKR Service    │────▶│   TWS/Gateway   │
│   Web App       │     │  (Python/FastAPI)│     │   (IBKR API)    │
└────────┬────────┘     └────────┬─────────┘     └─────────────────┘
         │                       │
         │                       │
         ▼                       ▼
    ┌─────────────────────────────────┐
    │      SQLite Database            │
    │      (Shared Volume)            │
    └─────────────────────────────────┘
```

## TWS/Gateway Setup

1. Open TWS or IB Gateway and log in
2. Go to Edit → Global Configuration → API → Settings
3. Enable "Enable ActiveX and Socket Clients"
4. Set "Socket port" (default: 7497 for paper, 7496 for live)
5. Add the IP of the IBKR service to "Trusted IPs" if running remotely
6. Uncheck "Read-Only API" if needed

## Best Practices

- Use a unique `client_id` for each application connecting to TWS
- Keep TWS/Gateway running and logged in for continuous sync
- Use IB Gateway for headless/server deployments
- Consider Tailscale for secure remote connections
