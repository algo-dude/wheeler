> **📋 Canonical Setup Guide**: This is the authoritative guide for configuring IBKR Flex Queries with Wheeler's IBKR service.

# IBKR Flex Query Setup Guide

This guide walks you through setting up IBKR Flex Queries to import historical transaction data into Wheeler.

## Overview

IBKR Flex Web Service allows Wheeler to import:
- **Trade executions** (buys/sells of stocks and options)
- **Commission reports** (fees per trade)
- **Cash transactions** (dividends, withholding taxes, fees)
- **Corporate actions** (assignments, exercises, expirations)

**Key advantage**: Works without TWS/Gateway running—pulls directly from IBKR servers.

---

## Step 1: Create a Flex Query in IBKR

### Navigate to Flex Queries

1. Log in to **IBKR Account Management** (portal.interactivebrokers.com)
2. Go to **Reports** → **Flex Queries**
3. Click **Create** (or **+** button) to create a new Activity Flex Query

### Configure the Flex Query

#### Basic Settings

| Setting | Value |
|---------|-------|
| Query Name | `Wheeler Sync` (or any descriptive name) |
| Date Period | `Last 365 Days` (or custom range) |
| Format | `XML` |

#### Required Sections

Enable these sections in your Flex Query:

##### 1. Trades Section
Click **Trades** and select all fields, or at minimum:
- `Account ID`
- `Trade ID` / `Execution ID`
- `Order ID`
- `Symbol`
- `Security Type` (Asset Category)
- `Date/Time`
- `Buy/Sell`
- `Quantity`
- `Price`
- `Commission`
- `Currency`
- `Conid`

For **options**, also include:
- `Put/Call`
- `Strike`
- `Expiry`
- `Multiplier`

##### 2. Cash Transactions Section
Click **Cash Transactions** and select:
- `Account ID`
- `Date/Time`
- `Type` (Dividends, Withholding Tax, etc.)
- `Symbol`
- `Amount`
- `Currency`
- `Description`
- `Conid`

##### 3. Corporate Actions Section
Click **Corporate Actions** and select:
- `Account ID`
- `Date/Time`
- `Type` (Assignment, Exercise, Expiration)
- `Symbol`
- `Quantity`
- `Proceeds/Value`
- `Description`
- `Conid`

### Save the Query

1. Click **Continue** / **Save**
2. Note the **Query ID** displayed (e.g., `123456`)
3. You'll need this ID for Wheeler configuration

---

## Step 2: Generate a Flex Token

### Navigate to Flex Web Service Settings

1. In Account Management, go to **Settings** → **Flex Web Service** (or search for "Flex")
2. Or navigate via **Reports** → **Flex Queries** → **Flex Web Service Token**

### Create Token

1. Click **Create** or **Generate Token**
2. Copy the token string (e.g., `abc123def456...`)
3. **Store this securely**—it provides API access to your account data

> ⚠️ **Security Note**: Treat this token like a password. It allows read access to your account history.

---

## Step 3: Configure Wheeler

### Option A: Environment Variables (Recommended for Docker)

Set these environment variables:

```bash
export IBKR_FLEX_TOKEN="your_flex_token_here"
export IBKR_FLEX_QUERY_ID="123456"
```

For Docker Compose, add to your `docker-compose.yml`:

```yaml
services:
  ibkr-service:
    image: ghcr.io/algo-dude/wheeler-ibkr-service:latest
    environment:
      - IBKR_FLEX_TOKEN=your_flex_token_here
      - IBKR_FLEX_QUERY_ID=123456
    volumes:
      - ./data:/app/data
```

### Option B: Wheeler Settings UI

1. Open Wheeler in your browser
2. Navigate to **Settings** → **IBKR**
3. Enter your Flex Token and Query ID
4. Click **Save**

---

## Step 4: Run Flex Sync

### Via Wheeler UI

1. Navigate to **IBKR** → **Transactions**
2. Click **Sync from Flex**
3. Wait for sync to complete (may take 30-60 seconds)

### Via API

```bash
# Using environment variables
curl -X POST http://localhost:8081/api/ibkr/transactions/sync-flex

# Using Wheeler's Go proxy (if running full stack)
curl -X POST http://localhost:8080/api/ibkr/transactions/sync-flex

# With explicit credentials
curl -X POST http://localhost:8081/api/ibkr/transactions/sync-flex \
  -H "Content-Type: application/json" \
  -d '{
    "token": "your_flex_token",
    "query_id": "123456"
  }'
```

### Expected Response

```json
{
  "success": true,
  "synced_at": "2024-12-30T15:30:00",
  "executions_upserted": 150,
  "commission_reports_upserted": 150,
  "activities_upserted": 45,
  "warnings": []
}
```

---

## Step 5: Verify Data

After syncing, check that data was imported:

### Via SQL (optional)

```sql
-- Check executions
SELECT COUNT(*), source FROM ibkr_executions GROUP BY source;

-- Check activities (assignments, dividends, etc.)
SELECT activity_type, COUNT(*) FROM ibkr_activities GROUP BY activity_type;

-- Check commissions
SELECT COUNT(*) FROM ibkr_commission_reports;
```

### Via Wheeler UI

- **IBKR** → **Transactions**: View imported trades
- **IBKR** → **Activities**: View assignments, dividends, fees

---

## Troubleshooting

### "Flex statement not ready" Error

IBKR may take 30-60 seconds to generate reports. The service retries automatically, but if it times out:
- Wait a minute and try again
- Check that your Query ID is correct
- Verify the query runs successfully in IBKR Account Management

### "Invalid Token" Error

- Tokens expire—regenerate in IBKR Account Management
- Ensure no extra whitespace in the token
- Check that the token matches the account containing the query

### "Query Not Found" Error

- Verify the Query ID in IBKR Account Management
- Ensure the query is an **Activity** Flex Query (not Trade Confirmation)
- Check the query is saved and active

### Empty Results

If sync succeeds but no data appears:
- Check the date range in your Flex Query settings
- Ensure trades exist in the selected period
- Verify XML format is selected (not CSV)

### Duplicate Detection

The sync is idempotent—running multiple times won't create duplicates:
- Executions use `exec_id` as unique key
- Activities use deterministic `activity_id` based on content
- The `source` column shows `API` or `FLEX` origin

---

## Running Both Real-Time and Flex Sync

You can safely use both sync methods together:

| Sync Type | Data Source | Best For |
|-----------|-------------|----------|
| Real-time API | TWS/Gateway | Current positions, live Greeks |
| Flex Query | IBKR Servers | Historical trades, assignments, fees |

The `source` column in the database tracks where each record originated:
- `API` = from real-time TWS/Gateway sync
- `FLEX` = from Flex Web Service

---

## Flex Query Best Practices

1. **Set appropriate date range**: Start with "Last 365 Days" to get history, then use shorter ranges for regular syncs

2. **Schedule regular syncs**: Flex Queries are cheap to run—sync daily or weekly to keep data current

3. **Use both syncs**: Real-time for current positions, Flex for historical events like assignments

4. **Secure your token**: Store in environment variables, not in code

5. **Monitor warnings**: Check the `warnings` array in sync responses for parsing issues

---

## Technical Details

### Flex Web Service Flow

```
┌─────────────────┐     SendRequest      ┌──────────────────┐
│   Wheeler       │ ───────────────────► │  IBKR Flex       │
│   IBKR Service  │   token + queryId    │  Service         │
└─────────────────┘                      └──────────────────┘
        │                                        │
        │         referenceCode                  │
        │ ◄──────────────────────────────────────┘
        │
        │         GetStatement (poll)
        │ ──────────────────────────────────────►
        │         token + referenceCode
        │
        │         (retry until ready)
        │ ◄──────────────────────────────────────┘
        │              XML Report
        ▼
┌─────────────────┐
│  Parse XML      │
│  Upsert to DB   │
└─────────────────┘
```

### Database Tables Affected

| Table | Records From |
|-------|--------------|
| `ibkr_executions` | Trade records |
| `ibkr_commission_reports` | Commission/fee details |
| `ibkr_activities` | Dividends, assignments, exercises, expirations, fees |

### Activity Type Mapping

| Flex Type | Wheeler Activity Type |
|-----------|----------------------|
| Dividends | `DIVIDEND` |
| Withholding Tax | `FEE` |
| Commission Adjustments | `FEE` |
| Option Assignment (TC) | `ASSIGNMENT` |
| Option Exercise (EX) | `EXERCISE` |
| Option Expiration (EP) | `EXPIRATION` |
