# Wheeler IBKR Service

A Python microservice for integrating Interactive Brokers with Wheeler portfolio tracking.

## Overview

This microservice connects to Interactive Brokers TWS (Trader Workstation) or IB Gateway using the `ib_async` library to sync positions with Wheeler's SQLite database.

## Features

- **Connection Management**: Connect/disconnect from TWS/Gateway
- **Position Sync**: Import stock and options positions from IBKR
- **Status Monitoring**: Check connection status and sync history
- **Shared Database**: Direct access to Wheeler's SQLite database
- **Greeks & Vol Surface**: Wheeler UI now displays option Greeks (delta, gamma, theta, vega, rho) for synced positions and plots a simple volatility surface for owned strikes/expirations using Polygon snapshots when available.

### Current Scope & Pending Work

- The latest changes add IBKR Greeks/IV sourcing and UI source tagging.
- Cost basis construction, assignment detection, and IBKR fee ingestion from transactions are **not yet implemented** and need a follow-up issue/implementation.

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
| `IBKR_DATABASE_PATH` | `/app/data/wheeler.db` | Path to Wheeler's SQLite database |
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
