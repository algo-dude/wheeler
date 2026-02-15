package test

import (
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"stonks/internal/database"
)

type ownedOptionsResponse struct {
	Options []struct {
		Symbol    string `json:"symbol"`
		Contracts int    `json:"contracts"`
	} `json:"options"`
}

func TestIBKROwnedOptions_LongToggle(t *testing.T) {
	dbPath, err := database.GetCurrentDatabasePath()
	if err != nil {
		t.Fatalf("failed to get current db path: %v", err)
	}

	db, err := database.NewDB(dbPath)
	if err != nil {
		t.Fatalf("failed to open db: %v", err)
	}
	defer db.Close()

	// Ensure a clean slate for this test symbol.
	if _, err := db.Exec("DELETE FROM options WHERE symbol = ?", "IBKRTEST"); err != nil {
		t.Fatalf("failed to cleanup options: %v", err)
	}
	if _, err := db.Exec("DELETE FROM symbols WHERE symbol = ?", "IBKRTEST"); err != nil {
		t.Fatalf("failed to cleanup symbols: %v", err)
	}

	if _, err := db.Exec("INSERT INTO symbols (symbol, price, dividend) VALUES (?, ?, ?)", "IBKRTEST", 100.0, 0.0); err != nil {
		t.Fatalf("failed to insert symbol: %v", err)
	}

	opened := time.Now().Add(-24 * time.Hour).Format("2006-01-02")
	expiration := time.Now().Add(30 * 24 * time.Hour).Format("2006-01-02")

	// Insert one short (-1) and one long (+1) open option.
	if _, err := db.Exec(
		"INSERT INTO options (symbol, type, opened, strike, expiration, premium, contracts) VALUES (?, ?, ?, ?, ?, ?, ?)",
		"IBKRTEST", "Put", opened, 100.0, expiration, 1.23, -1,
	); err != nil {
		t.Fatalf("failed to insert short option: %v", err)
	}
	if _, err := db.Exec(
		"INSERT INTO options (symbol, type, opened, strike, expiration, premium, contracts) VALUES (?, ?, ?, ?, ?, ?, ?)",
		"IBKRTEST", "Call", opened, 105.0, expiration, 0.77, 1,
	); err != nil {
		t.Fatalf("failed to insert long option: %v", err)
	}

	client := &http.Client{Timeout: 5 * time.Second}

	// Default: exclude long positions.
	{
		resp, err := client.Get("http://localhost:8081/api/ibkr/owned-options")
		if err != nil {
			t.Fatalf("failed to call owned-options: %v", err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("unexpected status: got %d", resp.StatusCode)
		}

		var payload ownedOptionsResponse
		if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
			t.Fatalf("failed to decode response: %v", err)
		}

		if len(payload.Options) != 1 {
			t.Fatalf("expected 1 option by default, got %d", len(payload.Options))
		}
		if payload.Options[0].Symbol != "IBKRTEST" || payload.Options[0].Contracts >= 0 {
			t.Fatalf("expected only short IBKRTEST option by default, got %+v", payload.Options[0])
		}
	}

	// includeLong=true: include both long and short.
	{
		resp, err := client.Get("http://localhost:8081/api/ibkr/owned-options?includeLong=true")
		if err != nil {
			t.Fatalf("failed to call owned-options includeLong: %v", err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("unexpected status: got %d", resp.StatusCode)
		}

		var payload ownedOptionsResponse
		if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
			t.Fatalf("failed to decode response: %v", err)
		}

		if len(payload.Options) != 2 {
			t.Fatalf("expected 2 options when includeLong=true, got %d", len(payload.Options))
		}
	}
}
