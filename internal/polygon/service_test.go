package polygon

import (
	"stonks/internal/models"
	"testing"
	"time"
)

func TestBuildOptionContractSymbol(t *testing.T) {
	exp := time.Date(2025, time.January, 17, 0, 0, 0, 0, time.UTC)
	opt := &models.Option{
		Symbol:     "AAPL",
		Type:       "Call",
		Strike:     150.0,
		Expiration: exp,
	}

	got := buildOptionContractSymbol(opt)
	want := "O:AAPL250117C00150000"
	if got != want {
		t.Fatalf("expected %s, got %s", want, got)
	}
}
