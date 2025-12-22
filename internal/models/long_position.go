package models

import (
	"database/sql"
	"fmt"
	"log"
	"time"
)

type LongPositionService struct {
	db *sql.DB
}

func NewLongPositionService(db *sql.DB) *LongPositionService {
	return &LongPositionService{db: db}
}

func (s *LongPositionService) Create(symbol string, opened time.Time, shares int, buyPrice float64) (*LongPosition, error) {
	query := `INSERT INTO long_positions (symbol, opened, shares, buy_price, adjusted_cost_basis_per_share, adjusted_cost_basis_total) 
			  VALUES (?, ?, ?, ?, ?, ?) 
			  RETURNING id, symbol, opened, closed, shares, buy_price, adjusted_cost_basis_per_share, adjusted_cost_basis_total, exit_price, created_at, updated_at`

	var position LongPosition
	err := s.db.QueryRow(query, symbol, opened, shares, buyPrice, buyPrice, buyPrice*float64(shares)).Scan(
		&position.ID, &position.Symbol, &position.Opened, &position.Closed, &position.Shares,
		&position.BuyPrice, &position.AdjustedCostBasisPerShare, &position.AdjustedCostBasisTotal, &position.ExitPrice, &position.CreatedAt, &position.UpdatedAt,
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create long position: %w", err)
	}

	if err := s.RecalculateAdjustedCostBasisForSymbol(symbol); err != nil {
		return nil, fmt.Errorf("failed to recalculate cost basis after create: %w", err)
	}

	return s.GetByID(position.ID)
}

func (s *LongPositionService) GetBySymbol(symbol string) ([]*LongPosition, error) {
	query := `SELECT id, symbol, opened, closed, shares, buy_price, adjusted_cost_basis_per_share, adjusted_cost_basis_total, exit_price, created_at, updated_at 
			  FROM long_positions WHERE symbol = ? ORDER BY opened DESC`

	rows, err := s.db.Query(query, symbol)
	if err != nil {
		return nil, fmt.Errorf("failed to get long positions: %w", err)
	}
	defer rows.Close()

	var positions []*LongPosition
	for rows.Next() {
		var position LongPosition
		if err := rows.Scan(&position.ID, &position.Symbol, &position.Opened, &position.Closed, &position.Shares,
			&position.BuyPrice, &position.AdjustedCostBasisPerShare, &position.AdjustedCostBasisTotal, &position.ExitPrice, &position.CreatedAt, &position.UpdatedAt); err != nil {
			return nil, fmt.Errorf("failed to scan long position: %w", err)
		}
		positions = append(positions, &position)
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("error iterating long positions: %w", err)
	}

	return positions, nil
}

func (s *LongPositionService) GetAll() ([]*LongPosition, error) {
	query := `SELECT id, symbol, opened, closed, shares, buy_price, adjusted_cost_basis_per_share, adjusted_cost_basis_total, exit_price, created_at, updated_at 
			  FROM long_positions ORDER BY opened DESC`

	rows, err := s.db.Query(query)
	if err != nil {
		return nil, fmt.Errorf("failed to get long positions: %w", err)
	}
	defer rows.Close()

	var positions []*LongPosition
	for rows.Next() {
		var position LongPosition
		if err := rows.Scan(&position.ID, &position.Symbol, &position.Opened, &position.Closed, &position.Shares,
			&position.BuyPrice, &position.AdjustedCostBasisPerShare, &position.AdjustedCostBasisTotal, &position.ExitPrice, &position.CreatedAt, &position.UpdatedAt); err != nil {
			return nil, fmt.Errorf("failed to scan long position: %w", err)
		}
		positions = append(positions, &position)
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("error iterating long positions: %w", err)
	}

	return positions, nil
}

func (s *LongPositionService) Close(symbol string, opened time.Time, shares int, buyPrice float64, closed time.Time, exitPrice float64) error {
	query := `UPDATE long_positions 
			  SET closed = ?, exit_price = ?, updated_at = CURRENT_TIMESTAMP 
			  WHERE symbol = ? AND opened = ? AND shares = ? AND buy_price = ?`

	result, err := s.db.Exec(query, closed, exitPrice, symbol, opened, shares, buyPrice)
	if err != nil {
		return fmt.Errorf("failed to close long position: %w", err)
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		return fmt.Errorf("failed to get rows affected: %w", err)
	}

	if rowsAffected == 0 {
		return fmt.Errorf("long position not found")
	}

	return nil
}

func (s *LongPositionService) Delete(symbol string, opened time.Time, shares int, buyPrice float64) error {
	query := `DELETE FROM long_positions WHERE symbol = ? AND opened = ? AND shares = ? AND buy_price = ?`
	result, err := s.db.Exec(query, symbol, opened, shares, buyPrice)
	if err != nil {
		return fmt.Errorf("failed to delete long position: %w", err)
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		return fmt.Errorf("failed to get rows affected: %w", err)
	}

	if rowsAffected == 0 {
		return fmt.Errorf("long position not found")
	}

	return nil
}

// GetByID retrieves a long position by its ID
func (s *LongPositionService) GetByID(id int) (*LongPosition, error) {
	query := `SELECT id, symbol, opened, closed, shares, buy_price, adjusted_cost_basis_per_share, adjusted_cost_basis_total, exit_price, created_at, updated_at 
			  FROM long_positions WHERE id = ?`

	var position LongPosition
	err := s.db.QueryRow(query, id).Scan(
		&position.ID, &position.Symbol, &position.Opened, &position.Closed, &position.Shares,
		&position.BuyPrice, &position.AdjustedCostBasisPerShare, &position.AdjustedCostBasisTotal, &position.ExitPrice, &position.CreatedAt, &position.UpdatedAt,
	)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil, fmt.Errorf("long position not found")
		}
		return nil, fmt.Errorf("failed to get long position: %w", err)
	}

	return &position, nil
}

// UpdateByID updates a long position by its ID
func (s *LongPositionService) UpdateByID(id int, symbol string, opened time.Time, shares int, buyPrice float64, closed *time.Time, exitPrice *float64) (*LongPosition, error) {
	query := `UPDATE long_positions 
			  SET symbol = ?, opened = ?, shares = ?, buy_price = ?, closed = ?, exit_price = ?, updated_at = CURRENT_TIMESTAMP 
			  WHERE id = ? 
			  RETURNING id, symbol, opened, closed, shares, buy_price, adjusted_cost_basis_per_share, adjusted_cost_basis_total, exit_price, created_at, updated_at`

	var position LongPosition
	err := s.db.QueryRow(query, symbol, opened, shares, buyPrice, closed, exitPrice, id).Scan(
		&position.ID, &position.Symbol, &position.Opened, &position.Closed, &position.Shares,
		&position.BuyPrice, &position.AdjustedCostBasisPerShare, &position.AdjustedCostBasisTotal, &position.ExitPrice, &position.CreatedAt, &position.UpdatedAt,
	)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil, fmt.Errorf("long position not found")
		}
		return nil, fmt.Errorf("failed to update long position: %w", err)
	}

	if err := s.RecalculateAdjustedCostBasisForSymbol(symbol); err != nil {
		return nil, fmt.Errorf("failed to recalculate cost basis after update: %w", err)
	}

	return s.GetByID(position.ID)
}

// DeleteByID deletes a long position by its ID
func (s *LongPositionService) DeleteByID(id int) error {
	query := `DELETE FROM long_positions WHERE id = ?`
	result, err := s.db.Exec(query, id)
	if err != nil {
		return fmt.Errorf("failed to delete long position: %w", err)
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		return fmt.Errorf("failed to get rows affected: %w", err)
	}

	if rowsAffected == 0 {
		return fmt.Errorf("long position not found")
	}

	return nil
}

// CloseByID closes a long position by its ID
func (s *LongPositionService) CloseByID(id int, closed time.Time, exitPrice float64) error {
	query := `UPDATE long_positions 
			  SET closed = ?, exit_price = ?, updated_at = CURRENT_TIMESTAMP 
			  WHERE id = ?`

	result, err := s.db.Exec(query, closed, exitPrice, id)
	if err != nil {
		return fmt.Errorf("failed to close long position: %w", err)
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		return fmt.Errorf("failed to get rows affected: %w", err)
	}

	if rowsAffected == 0 {
		return fmt.Errorf("long position not found")
	}

	return nil
}

func (s *LongPositionService) DeleteBySymbol(symbol string) error {
	query := `DELETE FROM long_positions WHERE symbol = ?`
	result, err := s.db.Exec(query, symbol)
	if err != nil {
		return fmt.Errorf("failed to delete long positions for symbol %s: %w", symbol, err)
	}

	_, err = result.RowsAffected()
	if err != nil {
		return fmt.Errorf("failed to get rows affected: %w", err)
	}

	return nil
}

// GetOpenPositions retrieves all open long positions (where closed is NULL)
func (s *LongPositionService) GetOpenPositions() ([]*LongPosition, error) {
	query := `SELECT id, symbol, opened, closed, shares, buy_price, adjusted_cost_basis_per_share, adjusted_cost_basis_total, exit_price, created_at, updated_at 
			  FROM long_positions WHERE closed IS NULL ORDER BY opened DESC`

	rows, err := s.db.Query(query)
	if err != nil {
		return nil, fmt.Errorf("failed to get open long positions: %w", err)
	}
	defer rows.Close()

	var positions []*LongPosition
	for rows.Next() {
		var position LongPosition
		if err := rows.Scan(&position.ID, &position.Symbol, &position.Opened, &position.Closed, &position.Shares,
			&position.BuyPrice, &position.AdjustedCostBasisPerShare, &position.AdjustedCostBasisTotal, &position.ExitPrice, &position.CreatedAt, &position.UpdatedAt); err != nil {
			return nil, fmt.Errorf("failed to scan open long position: %w", err)
		}
		positions = append(positions, &position)
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("error iterating open long positions: %w", err)
	}

	return positions, nil
}

// RecalculateAdjustedCostBasisForSymbol recomputes adjusted cost basis values for all lots of a symbol
// based on assigned put premiums and covered call premiums collected while shares are held.
func (s *LongPositionService) RecalculateAdjustedCostBasisForSymbol(symbol string) error {
	tx, err := s.db.Begin()
	if err != nil {
		return fmt.Errorf("failed to begin transaction: %w", err)
	}
	defer tx.Rollback()

	// Load positions in chronological order to allocate coverage FIFO
	posRows, err := tx.Query(`SELECT id, opened, closed, shares, buy_price FROM long_positions WHERE symbol = ? ORDER BY opened ASC`, symbol)
	if err != nil {
		return fmt.Errorf("failed to load positions: %w", err)
	}
	defer posRows.Close()

	type positionCalc struct {
		id       int
		opened   time.Time
		closed   *time.Time
		shares   int
		buyPrice float64
		adjust   float64
	}

	var positions []positionCalc
	for posRows.Next() {
		var (
			p  positionCalc
			cl sql.NullTime
		)
		if err := posRows.Scan(&p.id, &p.opened, &cl, &p.shares, &p.buyPrice); err != nil {
			return fmt.Errorf("failed to scan position: %w", err)
		}
		if cl.Valid {
			p.closed = &cl.Time
		}
		positions = append(positions, p)
	}
	if err := posRows.Err(); err != nil {
		return fmt.Errorf("error iterating positions: %w", err)
	}

	// Load options for symbol
	optRows, err := tx.Query(`SELECT id, type, opened, closed, strike, expiration, premium, contracts, exit_price, commission FROM options WHERE symbol = ? ORDER BY opened ASC`, symbol)
	if err != nil {
		return fmt.Errorf("failed to load options: %w", err)
	}
	defer optRows.Close()

	var callOptions []*Option
	var putOptions []*Option
	for optRows.Next() {
		var (
			opt  Option
			cl   sql.NullTime
			exit sql.NullFloat64
		)
		if err := optRows.Scan(&opt.ID, &opt.Type, &opt.Opened, &cl, &opt.Strike, &opt.Expiration, &opt.Premium, &opt.Contracts, &exit, &opt.Commission); err != nil {
			return fmt.Errorf("failed to scan option: %w", err)
		}
		if cl.Valid {
			opt.Closed = &cl.Time
		}
		if exit.Valid {
			val := exit.Float64
			opt.ExitPrice = &val
		}
		if opt.Type == "Call" {
			callOptions = append(callOptions, &opt)
		} else if opt.Type == "Put" {
			putOptions = append(putOptions, &opt)
		}
	}
	if err := optRows.Err(); err != nil {
		return fmt.Errorf("error iterating options: %w", err)
	}

	// Apply cash-secured put assignment premiums: match puts closed on the lot's open date
	for idx := range positions {
		p := &positions[idx]
		for _, opt := range putOptions {
			if opt.Closed == nil {
				continue
			}
			if sameDay(opt.Closed, &p.opened) {
				exit := opt.GetExitPriceValue()
				netPremium := (opt.Premium - exit) * float64(opt.Contracts) * 100
				netPremium -= opt.Commission
				p.adjust += netPremium
			}
		}
	}

	// Apply covered call premiums to lots that were active when the calls were opened
	for _, opt := range callOptions {
		remainingCoverage := opt.Contracts * 100
		exit := opt.GetExitPriceValue()
		netPremium := (opt.Premium - exit) * float64(opt.Contracts) * 100
		netPremium -= opt.Commission
		if netPremium == 0 || remainingCoverage == 0 {
			continue
		}
		for idx := range positions {
			if remainingCoverage == 0 {
				break
			}
			p := &positions[idx]
			if !positionActiveOn(p.opened, p.closed, opt.Opened) {
				continue
			}
			allocShares := minInt(remainingCoverage, p.shares)
			allocationRatio := float64(allocShares) / float64(opt.Contracts*100)
			p.adjust += netPremium * allocationRatio
			remainingCoverage -= allocShares
		}
	}

	// Persist recalculated values
	for _, p := range positions {
		baseTotal := p.buyPrice * float64(p.shares)
		adjustedTotal := baseTotal - p.adjust
		if adjustedTotal < 0 {
			log.Printf("[COST BASIS] Adjusted cost basis below zero for symbol %s (position %d). Base=%.2f, adjustments=%.2f", symbol, p.id, baseTotal, p.adjust)
			adjustedTotal = baseTotal
		}
		var adjustedPerShare float64
		if p.shares > 0 {
			adjustedPerShare = adjustedTotal / float64(p.shares)
		}
		if _, err := tx.Exec(`UPDATE long_positions SET adjusted_cost_basis_per_share = ?, adjusted_cost_basis_total = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?`, adjustedPerShare, adjustedTotal, p.id); err != nil {
			return fmt.Errorf("failed to update adjusted cost basis: %w", err)
		}
	}

	return tx.Commit()
}

func sameDay(a *time.Time, b *time.Time) bool {
	if a == nil || b == nil {
		return false
	}
	ay, am, ad := a.Date()
	by, bm, bd := b.Date()
	return ay == by && am == bm && ad == bd
}

func positionActiveOn(opened time.Time, closed *time.Time, t time.Time) bool {
	if opened.After(t) {
		return false
	}
	if closed == nil {
		return true
	}
	// Treat same day as active to include assignments or rolls recorded on the closing date
	if sameDay(closed, &t) {
		return true
	}
	return closed.After(t)
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
