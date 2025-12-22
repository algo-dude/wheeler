package database

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// TestMigrationAdjustedCostBasisColumns tests that migration adds missing columns
func TestMigrationAdjustedCostBasisColumns(t *testing.T) {
	// Create a temp database without the adjusted cost basis columns
	tmpDir := t.TempDir()
	dbPath := filepath.Join(tmpDir, "test_migration.db")

	// Create database with old schema (without adjusted cost basis columns)
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatalf("Failed to open test database: %v", err)
	}

	// Create long_positions table WITHOUT adjusted cost basis columns (simulating old schema)
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS long_positions (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			symbol TEXT NOT NULL,
			opened DATE NOT NULL,
			closed DATE,
			shares INTEGER NOT NULL,
			buy_price REAL NOT NULL,
			exit_price REAL,
			created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
			updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
		)
	`)
	if err != nil {
		t.Fatalf("Failed to create old schema: %v", err)
	}

	// Also create the symbols table as it's required by schema
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS symbols (
			symbol TEXT PRIMARY KEY,
			price REAL DEFAULT 0.0,
			dividend REAL DEFAULT 0.0,
			ex_dividend_date DATE,
			pe_ratio REAL,
			created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
			updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
		)
	`)
	if err != nil {
		t.Fatalf("Failed to create symbols table: %v", err)
	}

	// Insert test data with old schema
	_, err = db.Exec(`
		INSERT INTO symbols (symbol, price) VALUES ('AAPL', 100.0)
	`)
	if err != nil {
		t.Fatalf("Failed to insert test symbol: %v", err)
	}

	_, err = db.Exec(`
		INSERT INTO long_positions (symbol, opened, shares, buy_price) 
		VALUES ('AAPL', '2024-01-01', 100, 50.00)
	`)
	if err != nil {
		t.Fatalf("Failed to insert test position: %v", err)
	}

	db.Close()

	// Now open the database through our NewDB function which should run migrations
	wrappedDB, err := NewDB(dbPath)
	if err != nil {
		t.Fatalf("Failed to open database with migrations: %v", err)
	}
	defer wrappedDB.Close()

	// Verify columns exist
	var hasAdjustedPerShare bool
	err = wrappedDB.QueryRow("SELECT COUNT(*) FROM pragma_table_info('long_positions') WHERE name = 'adjusted_cost_basis_per_share'").Scan(&hasAdjustedPerShare)
	if err != nil {
		t.Fatalf("Failed to check for adjusted_cost_basis_per_share column: %v", err)
	}
	if !hasAdjustedPerShare {
		t.Error("Migration did not add adjusted_cost_basis_per_share column")
	}

	var hasAdjustedTotal bool
	err = wrappedDB.QueryRow("SELECT COUNT(*) FROM pragma_table_info('long_positions') WHERE name = 'adjusted_cost_basis_total'").Scan(&hasAdjustedTotal)
	if err != nil {
		t.Fatalf("Failed to check for adjusted_cost_basis_total column: %v", err)
	}
	if !hasAdjustedTotal {
		t.Error("Migration did not add adjusted_cost_basis_total column")
	}

	// Verify that existing data was initialized correctly
	var adjustedPerShare, adjustedTotal float64
	err = wrappedDB.QueryRow(`
		SELECT adjusted_cost_basis_per_share, adjusted_cost_basis_total 
		FROM long_positions 
		WHERE symbol = 'AAPL'
	`).Scan(&adjustedPerShare, &adjustedTotal)
	if err != nil {
		t.Fatalf("Failed to query migrated position: %v", err)
	}

	// adjusted_cost_basis_per_share should equal buy_price (50.00)
	if adjustedPerShare != 50.0 {
		t.Errorf("Expected adjusted_cost_basis_per_share = 50.0, got %v", adjustedPerShare)
	}

	// adjusted_cost_basis_total should equal buy_price * shares (50.00 * 100 = 5000.00)
	if adjustedTotal != 5000.0 {
		t.Errorf("Expected adjusted_cost_basis_total = 5000.0, got %v", adjustedTotal)
	}
}

// TestMigrationIdempotent tests that migration can run multiple times without error
func TestMigrationIdempotent(t *testing.T) {
	tmpDir := t.TempDir()
	dbPath := filepath.Join(tmpDir, "test_idempotent.db")

	// Create database first time
	db1, err := NewDB(dbPath)
	if err != nil {
		t.Fatalf("First NewDB call failed: %v", err)
	}
	db1.Close()

	// Open again - should not fail even if columns already exist
	db2, err := NewDB(dbPath)
	if err != nil {
		t.Fatalf("Second NewDB call failed: %v", err)
	}
	db2.Close()

	// Third time to be sure
	db3, err := NewDB(dbPath)
	if err != nil {
		t.Fatalf("Third NewDB call failed: %v", err)
	}
	db3.Close()
}

// TestQueryWithAdjustedCostBasisColumns tests that queries work with the migrated columns
func TestQueryWithAdjustedCostBasisColumns(t *testing.T) {
	tmpDir := t.TempDir()
	dbPath := filepath.Join(tmpDir, "test_query.db")

	// Create database (will have full schema with adjusted cost basis columns)
	wrappedDB, err := NewDB(dbPath)
	if err != nil {
		t.Fatalf("Failed to create test database: %v", err)
	}
	defer wrappedDB.Close()

	// Insert test data
	_, err = wrappedDB.Exec(`INSERT INTO symbols (symbol, price) VALUES ('TEST', 100.0)`)
	if err != nil {
		t.Fatalf("Failed to insert test symbol: %v", err)
	}

	_, err = wrappedDB.Exec(`
		INSERT INTO long_positions (symbol, opened, shares, buy_price, adjusted_cost_basis_per_share, adjusted_cost_basis_total) 
		VALUES ('TEST', '2024-01-01', 100, 50.00, 45.00, 4500.00)
	`)
	if err != nil {
		t.Fatalf("Failed to insert test position: %v", err)
	}

	// Run the same query that was failing before
	query := `SELECT id, symbol, opened, closed, shares, buy_price, adjusted_cost_basis_per_share, adjusted_cost_basis_total, exit_price, created_at, updated_at 
			  FROM long_positions WHERE symbol = ? ORDER BY opened DESC`

	rows, err := wrappedDB.Query(query, "TEST")
	if err != nil {
		t.Fatalf("Query failed (this was the original issue): %v", err)
	}
	defer rows.Close()

	var count int
	for rows.Next() {
		var id int
		var symbol string
		var opened string
		var closed sql.NullString
		var shares int
		var buyPrice, adjustedPerShare, adjustedTotal float64
		var exitPrice sql.NullFloat64
		var createdAt, updatedAt string

		err := rows.Scan(&id, &symbol, &opened, &closed, &shares, &buyPrice, &adjustedPerShare, &adjustedTotal, &exitPrice, &createdAt, &updatedAt)
		if err != nil {
			t.Fatalf("Failed to scan row: %v", err)
		}
		count++

		if adjustedPerShare != 45.0 {
			t.Errorf("Expected adjusted_cost_basis_per_share = 45.0, got %v", adjustedPerShare)
		}
		if adjustedTotal != 4500.0 {
			t.Errorf("Expected adjusted_cost_basis_total = 4500.0, got %v", adjustedTotal)
		}
	}

	if count != 1 {
		t.Errorf("Expected 1 row, got %d", count)
	}
}
