package models

import (
	"database/sql"
	"math"
	"testing"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

func setupLongPositionTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	if err != nil {
		t.Fatalf("failed to open in-memory db: %v", err)
	}

	schema := `
	CREATE TABLE options (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		symbol TEXT NOT NULL,
		type TEXT NOT NULL,
		opened DATE NOT NULL,
		closed DATE,
		strike REAL NOT NULL,
		expiration DATE NOT NULL,
		premium REAL NOT NULL,
		contracts INTEGER NOT NULL,
		exit_price REAL,
		commission REAL DEFAULT 0.0
	);

	CREATE TABLE long_positions (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		symbol TEXT NOT NULL,
		opened DATE NOT NULL,
		closed DATE,
		shares INTEGER NOT NULL,
		buy_price REAL NOT NULL,
		adjusted_cost_basis_per_share REAL NOT NULL DEFAULT 0.0,
		adjusted_cost_basis_total REAL NOT NULL DEFAULT 0.0,
		exit_price REAL,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
		updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
	);
	`
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("failed to create schema: %v", err)
	}
	return db
}

func TestRecalculateAdjustedCostBasisForSymbol(t *testing.T) {
	db := setupLongPositionTestDB(t)
	defer db.Close()

	lpService := NewLongPositionService(db)

	symbol := "AAA"
	opened := time.Date(2025, 1, 10, 0, 0, 0, 0, time.UTC)
	closed := time.Date(2025, 1, 25, 0, 0, 0, 0, time.UTC)

	// Assignment: put closed on opened date
	if _, err := db.Exec(`
		INSERT INTO options (symbol, type, opened, closed, strike, expiration, premium, contracts, exit_price, commission)
		VALUES (?, 'Put', ?, ?, 50.0, ?, 1.50, 1, 0.0, 1.00)
	`, symbol, opened, opened, opened); err != nil {
		t.Fatalf("failed to insert put option: %v", err)
	}

	// Covered call during holding window
	callOpened := time.Date(2025, 1, 15, 0, 0, 0, 0, time.UTC)
	if _, err := db.Exec(`
		INSERT INTO options (symbol, type, opened, closed, strike, expiration, premium, contracts, exit_price, commission)
		VALUES (?, 'Call', ?, ?, 55.0, ?, 0.50, 1, 0.10, 1.00)
	`, symbol, callOpened, callOpened, callOpened); err != nil {
		t.Fatalf("failed to insert call option: %v", err)
	}

	first, err := lpService.Create(symbol, opened, 100, 50.0)
	if err != nil {
		t.Fatalf("failed to create long position: %v", err)
	}
	if err := lpService.CloseByID(first.ID, closed, 0); err != nil {
		t.Fatalf("failed to close position: %v", err)
	}

	// Second lot should not inherit earlier adjustments
	secondOpened := time.Date(2025, 2, 1, 0, 0, 0, 0, time.UTC)
	second, err := lpService.Create(symbol, secondOpened, 100, 55.0)
	if err != nil {
		t.Fatalf("failed to create second long position: %v", err)
	}

	if err := lpService.RecalculateAdjustedCostBasisForSymbol(symbol); err != nil {
		t.Fatalf("recalculate failed: %v", err)
	}

	updatedFirst, err := lpService.GetByID(first.ID)
	if err != nil {
		t.Fatalf("failed to fetch first position: %v", err)
	}

	// Net put premium: (1.50 - 0) * 100 - 1.00 = 149.00
	// Net call premium: (0.50 - 0.10) * 100 - 1.00 = 39.00
	// Total adjustment: 188.00 => adjusted total = 5000 - 188 = 4812 -> per share 48.12
	if got := updatedFirst.AdjustedCostBasisPerShare; math.Abs(got-48.12) > 0.01 {
		t.Fatalf("expected adjusted basis per share ~48.12, got %.2f", got)
	}
	if got := updatedFirst.AdjustedCostBasisTotal; math.Abs(got-4812.0) > 0.5 {
		t.Fatalf("expected adjusted total ~4812, got %.2f", got)
	}

	updatedSecond, err := lpService.GetByID(second.ID)
	if err != nil {
		t.Fatalf("failed to fetch second position: %v", err)
	}
	if got := updatedSecond.AdjustedCostBasisPerShare; math.Abs(got-55.0) > 0.001 {
		t.Fatalf("second position basis should remain unchanged, got %.2f", got)
	}
}
