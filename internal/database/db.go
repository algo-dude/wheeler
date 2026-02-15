package database

import (
	"database/sql"
	"embed"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	_ "github.com/mattn/go-sqlite3"
)

//go:embed schema.sql
var schemaFS embed.FS

type DB struct {
	*sql.DB
}

func NewDB(dataSourceName string) (*DB, error) {
	// Add SQLite connection parameters for better reliability
	connStr := dataSourceName + "?_busy_timeout=10000&_journal_mode=WAL&_foreign_keys=on"
	
	db, err := sql.Open("sqlite3", connStr)
	if err != nil {
		return nil, fmt.Errorf("failed to open database: %w", err)
	}

	// Configure connection pool
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(5)

	if err := db.Ping(); err != nil {
		return nil, fmt.Errorf("failed to ping database: %w", err)
	}

	dbWrapper := &DB{DB: db}
	if err := dbWrapper.InitSchema(); err != nil {
		return nil, fmt.Errorf("failed to initialize schema: %w", err)
	}

	return dbWrapper, nil
}

func (db *DB) InitSchema() error {
	schemaSQL, err := schemaFS.ReadFile("schema.sql")
	if err != nil {
		return fmt.Errorf("failed to read schema file: %w", err)
	}

	// Schema contains both CREATE TABLE statements and CREATE INDEX statements.
	// On older DBs, CREATE TABLE IF NOT EXISTS does not add new columns, so index
	// creation can fail when an index targets a column added later via migrations.
	// To make DB switching robust, run table/seed schema first, then migrations,
	// then indexes.
	schemaText := string(schemaSQL)
	indexMarker := "-- Indexes for performance"
	markerPos := strings.Index(schemaText, indexMarker)

	preIndexSQL := schemaText
	indexSQL := ""
	if markerPos != -1 {
		preIndexSQL = schemaText[:markerPos]
		indexSQL = schemaText[markerPos:]
	}

	if _, err := db.Exec(preIndexSQL); err != nil {
		return fmt.Errorf("failed to execute schema: %w", err)
	}

	if err := db.runMigrations(); err != nil {
		return fmt.Errorf("failed to run migrations: %w", err)
	}

	if indexSQL != "" {
		if _, err := db.Exec(indexSQL); err != nil {
			return fmt.Errorf("failed to execute schema indexes: %w", err)
		}
	}

	return nil
}

func (db *DB) runMigrations() error {
	var hasCurrentPrice bool
	err := db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('options') WHERE name = 'current_price'").Scan(&hasCurrentPrice)
	if err != nil {
		return fmt.Errorf("failed to check for current_price column: %w", err)
	}

	if !hasCurrentPrice {
		_, err := db.Exec("ALTER TABLE options ADD COLUMN current_price REAL")
		if err != nil {
			return fmt.Errorf("failed to add current_price column: %w", err)
		}
	}

	// Migration: Add account column to options table for IBKR multi-account support
	var hasOptionsAccount bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('options') WHERE name = 'account'").Scan(&hasOptionsAccount)
	if err != nil {
		return fmt.Errorf("failed to check for options account column: %w", err)
	}
	if !hasOptionsAccount {
		_, err := db.Exec("ALTER TABLE options ADD COLUMN account TEXT")
		if err != nil {
			return fmt.Errorf("failed to add account column to options: %w", err)
		}
		// Create index for account filtering
		_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_options_account ON options(account)")
	}

	// Migration: Add account column to long_positions table for IBKR multi-account support
	var hasLongPositionsAccount bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('long_positions') WHERE name = 'account'").Scan(&hasLongPositionsAccount)
	if err != nil {
		return fmt.Errorf("failed to check for long_positions account column: %w", err)
	}
	if !hasLongPositionsAccount {
		_, err := db.Exec("ALTER TABLE long_positions ADD COLUMN account TEXT")
		if err != nil {
			return fmt.Errorf("failed to add account column to long_positions: %w", err)
		}
		// Create index for account filtering
		_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_long_positions_account ON long_positions(account)")
	}

	// Migration: Create account_nicknames table for IBKR account friendly names
	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS account_nicknames (
		account_id TEXT PRIMARY KEY,
		nickname TEXT NOT NULL
	)`)
	if err != nil {
		return fmt.Errorf("failed to create account_nicknames table: %w", err)
	}

	// Migration: Add fees column to long_positions for IBKR transaction tracking
	var hasLongPositionsFees bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('long_positions') WHERE name = 'fees'").Scan(&hasLongPositionsFees)
	if err != nil {
		return fmt.Errorf("failed to check for long_positions fees column: %w", err)
	}
	if !hasLongPositionsFees {
		_, err := db.Exec("ALTER TABLE long_positions ADD COLUMN fees REAL DEFAULT 0.0")
		if err != nil {
			return fmt.Errorf("failed to add fees column to long_positions: %w", err)
		}
	}

	// Migration: Add IBKR mapping columns to options table
	var hasOptionsIbkrConid bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('options') WHERE name = 'ibkr_conid'").Scan(&hasOptionsIbkrConid)
	if err != nil {
		return fmt.Errorf("failed to check for options ibkr_conid column: %w", err)
	}
	if !hasOptionsIbkrConid {
		_, _ = db.Exec("ALTER TABLE options ADD COLUMN ibkr_conid INTEGER")
		_, _ = db.Exec("ALTER TABLE options ADD COLUMN ibkr_account TEXT")
		_, _ = db.Exec("ALTER TABLE options ADD COLUMN ibkr_position REAL")
		_, _ = db.Exec("ALTER TABLE options ADD COLUMN ibkr_avg_cost REAL")
		_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_options_ibkr_conid ON options(ibkr_conid)")
	}

	// Migration: Add IBKR mapping columns to long_positions table
	var hasLongPositionsIbkrConid bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('long_positions') WHERE name = 'ibkr_conid'").Scan(&hasLongPositionsIbkrConid)
	if err != nil {
		return fmt.Errorf("failed to check for long_positions ibkr_conid column: %w", err)
	}
	if !hasLongPositionsIbkrConid {
		_, _ = db.Exec("ALTER TABLE long_positions ADD COLUMN ibkr_conid INTEGER")
		_, _ = db.Exec("ALTER TABLE long_positions ADD COLUMN ibkr_account TEXT")
		_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_long_positions_ibkr_conid ON long_positions(ibkr_conid)")
	}

	// Migration: Create IBKR transaction tables
	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS ibkr_executions (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		account TEXT NOT NULL,
		exec_id TEXT NOT NULL,
		order_id INTEGER,
		perm_id INTEGER,
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
		reconciled INTEGER NOT NULL DEFAULT 0,
		reconciled_option_id INTEGER REFERENCES options(id),
		reconciled_position_id INTEGER REFERENCES long_positions(id),
		UNIQUE(account, exec_id)
	)`)
	if err != nil {
		return fmt.Errorf("failed to create ibkr_executions table: %w", err)
	}

	// Migration: ensure reconciled exists for older DBs where ibkr_executions was
	// created before this column was added. This must run before schema index
	// creation (idx_ibkr_executions_reconciled).
	var hasIbkrExecutionsReconciled bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('ibkr_executions') WHERE name = 'reconciled'").Scan(&hasIbkrExecutionsReconciled)
	if err != nil {
		return fmt.Errorf("failed to check for ibkr_executions reconciled column: %w", err)
	}
	if !hasIbkrExecutionsReconciled {
		_, err := db.Exec("ALTER TABLE ibkr_executions ADD COLUMN reconciled INTEGER NOT NULL DEFAULT 0")
		if err != nil {
			return fmt.Errorf("failed to add reconciled column to ibkr_executions: %w", err)
		}
	}

	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_executions_trade_time ON ibkr_executions(trade_time)")
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_executions_symbol ON ibkr_executions(symbol)")
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_executions_conid ON ibkr_executions(conid)")
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_executions_reconciled ON ibkr_executions(reconciled)")
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_executions_account ON ibkr_executions(account)")

	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS ibkr_commission_reports (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		account TEXT NOT NULL,
		exec_id TEXT NOT NULL,
		commission REAL,
		realized_pnl REAL,
		yield_value REAL,
		yield_redemption_date DATE,
		currency TEXT,
		fees_json TEXT,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
		UNIQUE(account, exec_id)
	)`)
	if err != nil {
		return fmt.Errorf("failed to create ibkr_commission_reports table: %w", err)
	}
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_commission_exec ON ibkr_commission_reports(account, exec_id)")

	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS ibkr_activities (
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
		UNIQUE(account, activity_id)
	)`)
	if err != nil {
		return fmt.Errorf("failed to create ibkr_activities table: %w", err)
	}
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_activities_event_time ON ibkr_activities(event_time)")
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_activities_symbol ON ibkr_activities(symbol)")
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_activities_type ON ibkr_activities(activity_type)")
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_ibkr_activities_account ON ibkr_activities(account)")

	// Migration: Add Flex-related columns to IBKR tables (client_id, updated_at, source)
	var hasIbkrExecutionsClientId bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('ibkr_executions') WHERE name = 'client_id'").Scan(&hasIbkrExecutionsClientId)
	if err != nil {
		return fmt.Errorf("failed to check for ibkr_executions client_id column: %w", err)
	}
	if !hasIbkrExecutionsClientId {
		_, _ = db.Exec("ALTER TABLE ibkr_executions ADD COLUMN client_id INTEGER")
	}

	var hasIbkrExecutionsUpdatedAt bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('ibkr_executions') WHERE name = 'updated_at'").Scan(&hasIbkrExecutionsUpdatedAt)
	if err != nil {
		return fmt.Errorf("failed to check for ibkr_executions updated_at column: %w", err)
	}
	if !hasIbkrExecutionsUpdatedAt {
		_, _ = db.Exec("ALTER TABLE ibkr_executions ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP")
	}

	var hasIbkrExecutionsSource bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('ibkr_executions') WHERE name = 'source'").Scan(&hasIbkrExecutionsSource)
	if err != nil {
		return fmt.Errorf("failed to check for ibkr_executions source column: %w", err)
	}
	if !hasIbkrExecutionsSource {
		_, _ = db.Exec("ALTER TABLE ibkr_executions ADD COLUMN source TEXT DEFAULT 'API'")
	}

	// Migration: Add updated_at, source, raw_json to ibkr_commission_reports
	var hasCommissionUpdatedAt bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('ibkr_commission_reports') WHERE name = 'updated_at'").Scan(&hasCommissionUpdatedAt)
	if err != nil {
		return fmt.Errorf("failed to check for ibkr_commission_reports updated_at column: %w", err)
	}
	if !hasCommissionUpdatedAt {
		_, _ = db.Exec("ALTER TABLE ibkr_commission_reports ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP")
	}

	var hasCommissionSource bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('ibkr_commission_reports') WHERE name = 'source'").Scan(&hasCommissionSource)
	if err != nil {
		return fmt.Errorf("failed to check for ibkr_commission_reports source column: %w", err)
	}
	if !hasCommissionSource {
		_, _ = db.Exec("ALTER TABLE ibkr_commission_reports ADD COLUMN source TEXT DEFAULT 'API'")
	}

	var hasCommissionRawJson bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('ibkr_commission_reports') WHERE name = 'raw_json'").Scan(&hasCommissionRawJson)
	if err != nil {
		return fmt.Errorf("failed to check for ibkr_commission_reports raw_json column: %w", err)
	}
	if !hasCommissionRawJson {
		_, _ = db.Exec("ALTER TABLE ibkr_commission_reports ADD COLUMN raw_json TEXT")
	}

	// Migration: Add updated_at and source to ibkr_activities
	var hasActivitiesUpdatedAt bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('ibkr_activities') WHERE name = 'updated_at'").Scan(&hasActivitiesUpdatedAt)
	if err != nil {
		return fmt.Errorf("failed to check for ibkr_activities updated_at column: %w", err)
	}
	if !hasActivitiesUpdatedAt {
		_, _ = db.Exec("ALTER TABLE ibkr_activities ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP")
	}

	var hasActivitiesSource bool
	err = db.QueryRow("SELECT COUNT(*) FROM pragma_table_info('ibkr_activities') WHERE name = 'source'").Scan(&hasActivitiesSource)
	if err != nil {
		return fmt.Errorf("failed to check for ibkr_activities source column: %w", err)
	}
	if !hasActivitiesSource {
		_, _ = db.Exec("ALTER TABLE ibkr_activities ADD COLUMN source TEXT DEFAULT 'API'")
	}

	return nil
}

func (db *DB) Close() error {
	return db.DB.Close()
}

// GetCurrentDatabase reads the current database filename from ./data/currentdb
func GetCurrentDatabase() (string, error) {
	// Ensure data directory exists
	if err := os.MkdirAll("./data", 0755); err != nil {
		return "", fmt.Errorf("failed to create data directory: %w", err)
	}

	currentDBPath := "./data/currentdb"
	
	// Check if currentdb file exists
	if _, err := os.Stat(currentDBPath); os.IsNotExist(err) {
		// Create default currentdb file with wheeler.db
		if err := os.WriteFile(currentDBPath, []byte("wheeler.db"), 0644); err != nil {
			return "", fmt.Errorf("failed to create currentdb file: %w", err)
		}
		return "wheeler.db", nil
	}

	// Read the current database name
	data, err := os.ReadFile(currentDBPath)
	if err != nil {
		return "", fmt.Errorf("failed to read currentdb file: %w", err)
	}

	dbName := strings.TrimSpace(string(data))
	if dbName == "" {
		dbName = "wheeler.db"
	}

	return dbName, nil
}

// SetCurrentDatabase writes the current database filename to ./data/currentdb
func SetCurrentDatabase(dbName string) error {
	// Ensure data directory exists
	if err := os.MkdirAll("./data", 0755); err != nil {
		return fmt.Errorf("failed to create data directory: %w", err)
	}

	currentDBPath := "./data/currentdb"
	if err := os.WriteFile(currentDBPath, []byte(dbName), 0644); err != nil {
		return fmt.Errorf("failed to write currentdb file: %w", err)
	}

	return nil
}

// GetCurrentDatabasePath returns the full path to the current database
func GetCurrentDatabasePath() (string, error) {
	dbName, err := GetCurrentDatabase()
	if err != nil {
		return "", err
	}
	
	return filepath.Join("./data", dbName), nil
}

// CreateNewDatabase creates a new SQLite database in the data directory
func CreateNewDatabase(name string) error {
	// Ensure data directory exists
	if err := os.MkdirAll("./data", 0755); err != nil {
		return fmt.Errorf("failed to create data directory: %w", err)
	}

	// Add .db extension if not present
	if !strings.HasSuffix(name, ".db") {
		name = name + ".db"
	}

	dbPath := filepath.Join("./data", name)
	
	// Check if database already exists
	if _, err := os.Stat(dbPath); err == nil {
		return fmt.Errorf("database %s already exists", name)
	}

	// Create new database with schema
	_, err := NewDB(dbPath)
	if err != nil {
		return fmt.Errorf("failed to create database: %w", err)
	}

	return nil
}

// ListDatabases returns a list of all .db files in the data directory
func ListDatabases() ([]string, error) {
	dataDir := "./data"
	
	// Ensure data directory exists
	if err := os.MkdirAll(dataDir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create data directory: %w", err)
	}

	files, err := os.ReadDir(dataDir)
	if err != nil {
		return nil, fmt.Errorf("failed to read data directory: %w", err)
	}

	var databases []string
	for _, file := range files {
		if file.IsDir() {
			continue
		}
		if strings.HasSuffix(strings.ToLower(file.Name()), ".db") {
			databases = append(databases, file.Name())
		}
	}

	return databases, nil
}