CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    price REAL DEFAULT 0.0,
    dividend REAL DEFAULT 0.0,
    ex_dividend_date DATE,
    pe_ratio REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS long_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    opened DATE NOT NULL,
    closed DATE,
    shares INTEGER NOT NULL,
    buy_price REAL NOT NULL,
    adjusted_cost_basis_per_share REAL NOT NULL DEFAULT 0.0,
    adjusted_cost_basis_total REAL NOT NULL DEFAULT 0.0,
    exit_price REAL,
    account TEXT,
    fees REAL DEFAULT 0.0,
    ibkr_conid INTEGER,
    ibkr_account TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (symbol) REFERENCES symbols(symbol)
);

CREATE TABLE IF NOT EXISTS options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('Put', 'Call')),
    opened DATE NOT NULL,
    closed DATE,
    strike REAL NOT NULL,
    expiration DATE NOT NULL,
    premium REAL NOT NULL,
    contracts INTEGER NOT NULL,
    exit_price REAL,
    commission REAL DEFAULT 0.0,
    current_price REAL,
    account TEXT,
    ibkr_conid INTEGER,
    ibkr_account TEXT,
    ibkr_position REAL,
    ibkr_avg_cost REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (symbol) REFERENCES symbols(symbol)
);

CREATE TABLE IF NOT EXISTS dividends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    received DATE NOT NULL,
    amount REAL NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (symbol) REFERENCES symbols(symbol)
);

CREATE TABLE IF NOT EXISTS treasuries (
    cuspid TEXT PRIMARY KEY,
    purchased DATE NOT NULL,
    maturity DATE NOT NULL,
    amount REAL NOT NULL,
    yield REAL NOT NULL,
    buy_price REAL NOT NULL,
    current_value REAL,
    exit_price REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT,
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created DATETIME DEFAULT CURRENT_TIMESTAMP,
    type TEXT NOT NULL CHECK (type IN ('treasury_value', 'long_value', 'long_count', 'put_exposure', 'open_call_premium', 'open_call_count', 'open_put_premium', 'open_put_count', 'total_value')),
    value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS account_nicknames (
    account_id TEXT PRIMARY KEY,
    nickname TEXT NOT NULL
);

-- IBKR execution records (one row per trade execution)
CREATE TABLE IF NOT EXISTS ibkr_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    exec_id TEXT NOT NULL,
    order_id INTEGER,
    perm_id INTEGER,
    client_id INTEGER,
    conid INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    sec_type TEXT NOT NULL CHECK (sec_type IN ('STK', 'OPT')),
    currency TEXT,
    trade_time DATETIME NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    multiplier INTEGER,
    right TEXT CHECK (right IN ('C', 'P') OR right IS NULL),
    strike REAL,
    expiration DATE,
    exchange TEXT,
    liquidation INTEGER DEFAULT 0,
    raw_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    source TEXT DEFAULT 'API' CHECK (source IN ('API', 'FLEX')),
    reconciled INTEGER NOT NULL DEFAULT 0,
    reconciled_option_id INTEGER REFERENCES options(id),
    reconciled_position_id INTEGER REFERENCES long_positions(id),
    UNIQUE(account, exec_id)
);

-- IBKR commission reports linked to executions
CREATE TABLE IF NOT EXISTS ibkr_commission_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    exec_id TEXT NOT NULL,
    commission REAL,
    realized_pnl REAL,
    yield_value REAL,
    yield_redemption_date DATE,
    currency TEXT,
    fees_json TEXT,
    raw_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    source TEXT DEFAULT 'API' CHECK (source IN ('API', 'FLEX')),
    UNIQUE(account, exec_id)
);

-- IBKR activity events (assignments, exercises, expirations, dividends, fees)
CREATE TABLE IF NOT EXISTS ibkr_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    activity_id TEXT,
    activity_type TEXT NOT NULL CHECK (activity_type IN (
        'ASSIGNMENT', 'EXERCISE', 'EXPIRATION', 
        'DIVIDEND', 'FEE', 'TRANSFER', 'OTHER'
    )),
    conid INTEGER,
    symbol TEXT,
    event_time DATETIME NOT NULL,
    quantity REAL,
    amount REAL,
    currency TEXT,
    description TEXT,
    raw_text TEXT,
    raw_json TEXT,
    related_exec_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    source TEXT DEFAULT 'API' CHECK (source IN ('API', 'FLEX')),
    UNIQUE(account, activity_id)
);

-- Insert default POLYGON_API_KEY setting
INSERT OR IGNORE INTO settings (name, value, description) 
VALUES ('POLYGON_API_KEY', '', 'API key for Polygon.io stock market data integration');

-- Indexes for performance
-- Note: Primary key columns automatically have indexes, so we don't need explicit indexes for:
-- symbols(symbol), treasuries(cuspid), settings(name) - they have PRIMARY KEY
-- Foreign key and commonly queried columns:
CREATE INDEX IF NOT EXISTS idx_long_positions_symbol ON long_positions(symbol);
CREATE INDEX IF NOT EXISTS idx_long_positions_opened ON long_positions(opened);
CREATE INDEX IF NOT EXISTS idx_options_symbol ON options(symbol);
CREATE INDEX IF NOT EXISTS idx_options_expiration ON options(expiration);
CREATE INDEX IF NOT EXISTS idx_options_type ON options(type);
CREATE INDEX IF NOT EXISTS idx_dividends_symbol ON dividends(symbol);
CREATE INDEX IF NOT EXISTS idx_dividends_received ON dividends(received);
CREATE INDEX IF NOT EXISTS idx_treasuries_maturity ON treasuries(maturity);
CREATE INDEX IF NOT EXISTS idx_treasuries_purchased ON treasuries(purchased);
CREATE INDEX IF NOT EXISTS idx_metrics_created ON metrics(created);
CREATE INDEX IF NOT EXISTS idx_metrics_type ON metrics(type);
CREATE INDEX IF NOT EXISTS idx_options_account ON options(account);
CREATE INDEX IF NOT EXISTS idx_long_positions_account ON long_positions(account);

-- IBKR tables indexes
CREATE INDEX IF NOT EXISTS idx_ibkr_executions_trade_time ON ibkr_executions(trade_time);
CREATE INDEX IF NOT EXISTS idx_ibkr_executions_symbol ON ibkr_executions(symbol);
CREATE INDEX IF NOT EXISTS idx_ibkr_executions_conid ON ibkr_executions(conid);
CREATE INDEX IF NOT EXISTS idx_ibkr_executions_reconciled ON ibkr_executions(reconciled);
CREATE INDEX IF NOT EXISTS idx_ibkr_executions_account ON ibkr_executions(account);
CREATE INDEX IF NOT EXISTS idx_ibkr_commission_exec ON ibkr_commission_reports(account, exec_id);
CREATE INDEX IF NOT EXISTS idx_ibkr_activities_event_time ON ibkr_activities(event_time);
CREATE INDEX IF NOT EXISTS idx_ibkr_activities_symbol ON ibkr_activities(symbol);
CREATE INDEX IF NOT EXISTS idx_ibkr_activities_type ON ibkr_activities(activity_type);
CREATE INDEX IF NOT EXISTS idx_ibkr_activities_account ON ibkr_activities(account);

-- IBKR mapping column indexes
CREATE INDEX IF NOT EXISTS idx_options_ibkr_conid ON options(ibkr_conid);
CREATE INDEX IF NOT EXISTS idx_long_positions_ibkr_conid ON long_positions(ibkr_conid);

-- Unique constraints to prevent duplicate business records
-- (These replace the compound primary keys while allowing easier HTTP CRUD with integer IDs)
CREATE UNIQUE INDEX IF NOT EXISTS idx_options_unique ON options(symbol, type, opened, strike, expiration, premium, contracts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dividends_unique ON dividends(symbol, received, amount);
