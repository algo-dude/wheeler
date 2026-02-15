package models

import (
	"database/sql"
	"fmt"
	"strings"
	"time"
)

type Setting struct {
	Name        string     `json:"name"`
	Value       *string    `json:"value"`
	Description *string    `json:"description"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   time.Time  `json:"updated_at"`
}

type SettingService struct {
	db *sql.DB
}

func NewSettingService(db *sql.DB) *SettingService {
	return &SettingService{db: db}
}

func (s *SettingService) Create(name, value, description string) (*Setting, error) {
	name = strings.TrimSpace(strings.ToUpper(name))
	if name == "" {
		return nil, fmt.Errorf("setting name cannot be empty")
	}

	query := `INSERT INTO settings (name, value, description) VALUES (?, ?, ?) RETURNING name, value, description, created_at, updated_at`
	var setting Setting
	err := s.db.QueryRow(query, name, nullableString(value), nullableString(description)).Scan(
		&setting.Name, &setting.Value, &setting.Description, &setting.CreatedAt, &setting.UpdatedAt)
	if err != nil {
		return nil, fmt.Errorf("failed to create setting: %w", err)
	}

	return &setting, nil
}

func (s *SettingService) GetByName(name string) (*Setting, error) {
	query := `SELECT name, value, description, created_at, updated_at FROM settings WHERE name = ?`
	var setting Setting
	err := s.db.QueryRow(query, name).Scan(&setting.Name, &setting.Value, &setting.Description, &setting.CreatedAt, &setting.UpdatedAt)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil, fmt.Errorf("setting not found")
		}
		return nil, fmt.Errorf("failed to get setting: %w", err)
	}

	return &setting, nil
}

func (s *SettingService) GetAll() ([]*Setting, error) {
	query := `SELECT name, value, description, created_at, updated_at FROM settings ORDER BY name`
	rows, err := s.db.Query(query)
	if err != nil {
		return nil, fmt.Errorf("failed to get settings: %w", err)
	}
	defer rows.Close()

	var settings []*Setting
	for rows.Next() {
		var setting Setting
		if err := rows.Scan(&setting.Name, &setting.Value, &setting.Description, &setting.CreatedAt, &setting.UpdatedAt); err != nil {
			return nil, fmt.Errorf("failed to scan setting: %w", err)
		}
		settings = append(settings, &setting)
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("error iterating settings: %w", err)
	}

	return settings, nil
}

func (s *SettingService) Update(name, value, description string) (*Setting, error) {
	name = strings.TrimSpace(strings.ToUpper(name))
	if name == "" {
		return nil, fmt.Errorf("setting name cannot be empty")
	}

	query := `UPDATE settings SET value = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ? RETURNING name, value, description, created_at, updated_at`
	var setting Setting
	err := s.db.QueryRow(query, nullableString(value), nullableString(description), name).Scan(
		&setting.Name, &setting.Value, &setting.Description, &setting.CreatedAt, &setting.UpdatedAt)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil, fmt.Errorf("setting not found")
		}
		return nil, fmt.Errorf("failed to update setting: %w", err)
	}

	return &setting, nil
}

func (s *SettingService) Upsert(name, value, description string) (*Setting, error) {
	// Try to update first
	setting, err := s.Update(name, value, description)
	if err == nil {
		return setting, nil
	}

	// If update failed because setting doesn't exist, create it
	if strings.Contains(err.Error(), "setting not found") {
		return s.Create(name, value, description)
	}

	return nil, err
}

func (s *SettingService) Delete(name string) error {
	query := `DELETE FROM settings WHERE name = ?`
	result, err := s.db.Exec(query, name)
	if err != nil {
		return fmt.Errorf("failed to delete setting: %w", err)
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		return fmt.Errorf("failed to get rows affected: %w", err)
	}

	if rowsAffected == 0 {
		return fmt.Errorf("setting not found")
	}

	return nil
}

// GetValue returns the setting value as a string, or empty string if not found
func (s *SettingService) GetValue(name string) string {
	setting, err := s.GetByName(name)
	if err != nil || setting.Value == nil {
		return ""
	}
	return *setting.Value
}

// SetValue creates or updates a setting value
func (s *SettingService) SetValue(name, value, description string) error {
	_, err := s.Upsert(name, value, description)
	return err
}

// GetValueWithDefault returns the setting value or a default if not found
func (s *SettingService) GetValueWithDefault(name, defaultValue string) string {
	value := s.GetValue(name)
	if value == "" {
		return defaultValue
	}
	return value
}

// Helper function to convert empty strings to nil for database storage
func nullableString(s string) interface{} {
	if s == "" {
		return nil
	}
	return s
}

// GetValueAsString returns setting value as string
func (s *Setting) GetValueAsString() string {
	if s.Value == nil {
		return ""
	}
	return *s.Value
}

// GetDescriptionAsString returns setting description as string
func (s *Setting) GetDescriptionAsString() string {
	if s.Description == nil {
		return ""
	}
	return *s.Description
}

// GetAccountNickname returns the nickname for a given IBKR account ID
func GetAccountNickname(db *sql.DB, accountID string) (string, error) {
	var nickname string
	err := db.QueryRow("SELECT nickname FROM account_nicknames WHERE account_id = ?", accountID).Scan(&nickname)
	if err != nil {
		if err == sql.ErrNoRows {
			return "", nil // No nickname set, return empty string
		}
		return "", fmt.Errorf("failed to get account nickname: %w", err)
	}
	return nickname, nil
}

// SetAccountNickname sets or updates the nickname for a given IBKR account ID
func SetAccountNickname(db *sql.DB, accountID, nickname string) error {
	accountID = strings.TrimSpace(accountID)
	nickname = strings.TrimSpace(nickname)

	if accountID == "" {
		return fmt.Errorf("account ID cannot be empty")
	}

	if nickname == "" {
		// If nickname is empty, delete the entry
		_, err := db.Exec("DELETE FROM account_nicknames WHERE account_id = ?", accountID)
		if err != nil {
			return fmt.Errorf("failed to delete account nickname: %w", err)
		}
		return nil
	}

	// Upsert the nickname
	_, err := db.Exec(`INSERT INTO account_nicknames (account_id, nickname) VALUES (?, ?)
		ON CONFLICT(account_id) DO UPDATE SET nickname = excluded.nickname`,
		accountID, nickname)
	if err != nil {
		return fmt.Errorf("failed to set account nickname: %w", err)
	}
	return nil
}

// GetAllAccountNicknames returns a map of account IDs to their nicknames
func GetAllAccountNicknames(db *sql.DB) (map[string]string, error) {
	rows, err := db.Query("SELECT account_id, nickname FROM account_nicknames")
	if err != nil {
		return nil, fmt.Errorf("failed to get account nicknames: %w", err)
	}
	defer rows.Close()

	nicknames := make(map[string]string)
	for rows.Next() {
		var accountID, nickname string
		if err := rows.Scan(&accountID, &nickname); err != nil {
			return nil, fmt.Errorf("failed to scan account nickname: %w", err)
		}
		nicknames[accountID] = nickname
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("error iterating account nicknames: %w", err)
	}

	return nicknames, nil
}