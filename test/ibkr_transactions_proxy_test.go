package test

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"
)

func TestIBKRTransactionsSyncProxy_EmptyBodySendsConfig(t *testing.T) {
	var received map[string]any

	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("unexpected method: %s", r.Method)
		}
		if r.URL.Path != "/api/ibkr/transactions/sync" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &received)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"success":true,"executions_upserted":0,"commission_reports_upserted":0,"warnings":[],"errors":[]}`))
	}))
	defer stub.Close()

	old := os.Getenv("IBKR_SERVICE_URL")
	t.Cleanup(func() {
		_ = os.Setenv("IBKR_SERVICE_URL", old)
	})
	_ = os.Setenv("IBKR_SERVICE_URL", stub.URL)

	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Post("http://localhost:8081/api/ibkr/transactions/sync", "application/json", bytes.NewReader(nil))
	if err != nil {
		t.Fatalf("request failed: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("unexpected status %d: %s", resp.StatusCode, string(body))
	}

	if received == nil {
		t.Fatalf("expected stub to receive JSON")
	}

	// Default config values are derived from env/DB defaults.
	if received["host"] == "" {
		t.Fatalf("expected host in payload, got: %+v", received)
	}
	if received["port"] == nil {
		t.Fatalf("expected port in payload, got: %+v", received)
	}
	if received["client_id"] == nil {
		t.Fatalf("expected client_id in payload, got: %+v", received)
	}
}

func TestIBKRTransactionsSyncProxy_ForwardsBodyVerbatim(t *testing.T) {
	var received map[string]any

	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("unexpected method: %s", r.Method)
		}
		if r.URL.Path != "/api/ibkr/transactions/sync" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &received)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"success":true}`))
	}))
	defer stub.Close()

	old := os.Getenv("IBKR_SERVICE_URL")
	t.Cleanup(func() {
		_ = os.Setenv("IBKR_SERVICE_URL", old)
	})
	_ = os.Setenv("IBKR_SERVICE_URL", stub.URL)

	body := []byte(`{"start":"2025-12-01","end":"2025-12-02","lookback_days":7,"dry_run":true}`)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Post("http://localhost:8081/api/ibkr/transactions/sync", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("request failed: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("unexpected status %d: %s", resp.StatusCode, string(b))
	}

	if received["start"] != "2025-12-01" {
		t.Fatalf("expected start forwarded, got %+v", received)
	}
	if received["end"] != "2025-12-02" {
		t.Fatalf("expected end forwarded, got %+v", received)
	}
	if received["dry_run"] != true {
		t.Fatalf("expected dry_run forwarded, got %+v", received)
	}
}

func TestIBKRTransactionsStatusProxy_ForwardsResponse(t *testing.T) {
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Fatalf("unexpected method: %s", r.Method)
		}
		if r.URL.Path != "/api/ibkr/transactions/status" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"connected":true,"last_transactions_sync":null,"last_transactions_sync_result":null,"database":{"path":"/tmp/test.db","ibkr_executions":{"rows":0}}}`))
	}))
	defer stub.Close()

	old := os.Getenv("IBKR_SERVICE_URL")
	t.Cleanup(func() {
		_ = os.Setenv("IBKR_SERVICE_URL", old)
	})
	_ = os.Setenv("IBKR_SERVICE_URL", stub.URL)

	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get("http://localhost:8081/api/ibkr/transactions/status")
	if err != nil {
		t.Fatalf("request failed: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("unexpected status %d: %s", resp.StatusCode, string(b))
	}

	var payload map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}
	if payload["connected"] != true {
		t.Fatalf("expected connected true, got %+v", payload)
	}
}
