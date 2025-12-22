package web

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"stonks/internal/polygon"
	"strconv"
	"strings"
)

// IBKRSettingsData holds data for the IBKR settings template
type IBKRSettingsData struct {
	AllSymbols     []string `json:"allSymbols"`
	CurrentDB      string   `json:"currentDB"`
	ActivePage     string   `json:"activePage"`
	TWS_Host       string   `json:"tws_host"`
	TWS_Port       string   `json:"tws_port"`
	ClientID       string   `json:"client_id"`
	IBKRServiceURL string   `json:"ibkr_service_url"`
}

// getIBKRServiceURL returns the URL of the IBKR microservice
func getIBKRServiceURL() string {
	url := os.Getenv("IBKR_SERVICE_URL")
	if url == "" {
		url = "http://localhost:8081"
	}
	return url
}

func ibkrEnvOrDefault(key, fallback string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return fallback
}

func (s *Server) ibkrConnectionConfig() IBKRConnectionConfig {
	host := s.settingService.GetValueWithDefault("IBKR_TWS_HOST", ibkrEnvOrDefault("IBKR_TWS_HOST", "127.0.0.1"))
	portStr := s.settingService.GetValueWithDefault("IBKR_TWS_PORT", ibkrEnvOrDefault("IBKR_TWS_PORT", "7497"))
	clientStr := s.settingService.GetValueWithDefault("IBKR_CLIENT_ID", ibkrEnvOrDefault("IBKR_CLIENT_ID", "1"))

	port, err := strconv.Atoi(portStr)
	if err != nil {
		log.Printf("[IBKR SETTINGS] Invalid port value %q, using default: %v", portStr, err)
		port = 7497
	}
	if port == 0 {
		port = 7497
	}
	clientID, err := strconv.Atoi(clientStr)
	if err != nil {
		log.Printf("[IBKR SETTINGS] Invalid client ID %q, using default: %v", clientStr, err)
		clientID = 1
	}
	if clientID == 0 {
		clientID = 1
	}

	return IBKRConnectionConfig{
		Host:     host,
		Port:     port,
		ClientID: clientID,
	}
}

// ibkrSettingsHandler serves the IBKR settings management page
func (s *Server) ibkrSettingsHandler(w http.ResponseWriter, r *http.Request) {
	log.Printf("[IBKR SETTINGS] Handling IBKR settings page request")

	// Get all symbols for navigation
	symbols, err := s.symbolService.GetDistinctSymbols()
	if err != nil {
		log.Printf("[IBKR SETTINGS] Error getting symbols: %v", err)
		symbols = []string{}
	}

	config := s.ibkrConnectionConfig()

	data := IBKRSettingsData{
		AllSymbols:     symbols,
		CurrentDB:      s.getCurrentDatabaseName(),
		ActivePage:     "settings-ibkr",
		TWS_Host:       config.Host,
		TWS_Port:       strconv.Itoa(config.Port),
		ClientID:       strconv.Itoa(config.ClientID),
		IBKRServiceURL: getIBKRServiceURL(),
	}

	s.renderTemplate(w, "settings-ibkr.html", data)
}

// IBKRConnectionConfig represents connection configuration for IBKR
type IBKRConnectionConfig struct {
	Host     string `json:"host"`
	Port     int    `json:"port"`
	ClientID int    `json:"client_id"`
}

// OwnedOptionView represents an IBKR-owned option with Greeks
type OwnedOptionView struct {
	ID           int                    `json:"id"`
	Symbol       string                 `json:"symbol"`
	Type         string                 `json:"type"`
	Strike       float64                `json:"strike"`
	Expiration   string                 `json:"expiration"`
	Contracts    int                    `json:"contracts"`
	Premium      float64                `json:"premium"`
	Greeks       *polygon.OptionGreeks  `json:"greeks,omitempty"`
	ImpliedVol   *float64               `json:"implied_volatility,omitempty"`
	SurfacePoint *VolSurfacePoint       `json:"surface_point,omitempty"`
	Metadata     map[string]interface{} `json:"metadata,omitempty"`
	DataSource   string                 `json:"data_source,omitempty"`
}

// VolSurfacePoint represents a point in the volatility surface visualization
type VolSurfacePoint struct {
	Symbol      string   `json:"symbol"`
	Strike      float64  `json:"strike"`
	Expiration  string   `json:"expiration"`
	ExpiryMs    int64    `json:"expiry_ms"`
	IV          *float64 `json:"iv,omitempty"`
	IsOwned     bool     `json:"is_owned"`
	OptionType  string   `json:"option_type"`
	Contracts   int      `json:"contracts"`
	Description string   `json:"description"`
}

// ibkrTestHandler proxies test connection request to IBKR microservice
func (s *Server) ibkrTestHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	log.Printf("[IBKR API] Handling test connection request")

	// Read request body
	body, err := io.ReadAll(r.Body)
	if err != nil {
		log.Printf("[IBKR API] Error reading request body: %v", err)
		http.Error(w, "Failed to read request", http.StatusBadRequest)
		return
	}
	defer r.Body.Close()

	config := s.ibkrConnectionConfig()
	if len(body) > 0 {
		if err := json.Unmarshal(body, &config); err != nil {
			log.Printf("[IBKR API] Error parsing request config: %v", err)
		}
		if config.Host != "" {
			s.settingService.SetValue("IBKR_TWS_HOST", config.Host, "IBKR TWS/Gateway hostname")
		}
		if config.Port > 0 {
			s.settingService.SetValue("IBKR_TWS_PORT", strconv.Itoa(config.Port), "IBKR TWS/Gateway port")
		}
		if config.ClientID > 0 {
			s.settingService.SetValue("IBKR_CLIENT_ID", strconv.Itoa(config.ClientID), "IBKR client ID")
		}
	}

	payload, err := json.Marshal(config)
	if err != nil {
		log.Printf("[IBKR API] Failed to marshal config for test: %v", err)
		http.Error(w, "Failed to marshal request", http.StatusInternalServerError)
		return
	}

	// Forward request to IBKR microservice
	serviceURL := getIBKRServiceURL() + "/api/ibkr/test"
	resp, err := http.Post(serviceURL, "application/json", bytes.NewReader(payload))
	if err != nil {
		log.Printf("[IBKR API] Error connecting to IBKR service: %v", err)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"success":   false,
			"connected": false,
			"error":     "IBKR service unavailable: " + err.Error(),
		})
		return
	}
	defer resp.Body.Close()

	// Forward response back to client
	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Printf("[IBKR API] Error reading response: %v", err)
		http.Error(w, "Failed to read response", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.Write(responseBody)
}

// ibkrSyncHandler proxies sync request to IBKR microservice
func (s *Server) ibkrSyncHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	log.Printf("[IBKR API] Handling sync positions request")

	config := s.ibkrConnectionConfig()
	payload, err := json.Marshal(config)
	if err != nil {
		log.Printf("[IBKR API] Failed to marshal config for sync: %v", err)
		http.Error(w, "Failed to marshal request", http.StatusInternalServerError)
		return
	}

	// Forward request to IBKR microservice
	serviceURL := getIBKRServiceURL() + "/api/ibkr/sync"
	resp, err := http.Post(serviceURL, "application/json", bytes.NewReader(payload))
	if err != nil {
		log.Printf("[IBKR API] Error connecting to IBKR service: %v", err)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"success": false,
			"errors":  []string{"IBKR service unavailable: " + err.Error()},
		})
		return
	}
	defer resp.Body.Close()

	// Forward response back to client
	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Printf("[IBKR API] Error reading response: %v", err)
		http.Error(w, "Failed to read response", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.Write(responseBody)
}

// ibkrStatusHandler proxies status request to IBKR microservice
func (s *Server) ibkrStatusHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	log.Printf("[IBKR API] Handling status request")

	// Forward request to IBKR microservice
	serviceURL := getIBKRServiceURL() + "/api/ibkr/status"
	resp, err := http.Get(serviceURL)
	if err != nil {
		log.Printf("[IBKR API] Error connecting to IBKR service: %v", err)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"connected":  false,
			"last_sync":  nil,
			"database":   map[string]interface{}{"error": "IBKR service unavailable"},
			"service_up": false,
		})
		return
	}
	defer resp.Body.Close()

	// Forward response back to client
	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Printf("[IBKR API] Error reading response: %v", err)
		http.Error(w, "Failed to read response", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.Write(responseBody)
}

// ibkrOwnedOptionsHandler returns owned options with Greeks and surface points
func (s *Server) ibkrOwnedOptionsHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	openOptions, err := s.optionService.GetOpen()
	if err != nil {
		http.Error(w, fmt.Sprintf("failed to fetch options: %v", err), http.StatusInternalServerError)
		return
	}

	payload := struct {
		Options []OwnedOptionView `json:"options"`
		Surface []VolSurfacePoint `json:"surface"`
		Warning string            `json:"warning,omitempty"`
	}{
		Options: []OwnedOptionView{},
		Surface: []VolSurfacePoint{},
	}

	ibkrGreeks, ibkrWarning := s.fetchIBKRGreeks(r.Context())
	if ibkrWarning != "" {
		payload.Warning = ibkrWarning
	}

	for _, opt := range openOptions {
		view := OwnedOptionView{
			ID:         opt.ID,
			Symbol:     opt.Symbol,
			Type:       opt.Type,
			Strike:     opt.Strike,
			Contracts:  opt.Contracts,
			Premium:    opt.Premium,
			Expiration: opt.Expiration.Format("2006-01-02"),
		}

		lookupKey := optionKey(view.Symbol, view.Type, view.Strike, view.Expiration)
		if g, ok := ibkrGreeks[lookupKey]; ok {
			view.Greeks = g.Greeks
			view.ImpliedVol = g.ImpliedVolatility
			view.DataSource = "IBKR"
			if sp := makeSurfacePoint(opt.Symbol, opt.Type, view.Expiration, opt.Expiration.UnixMilli(), opt.Strike, opt.Contracts, g.ImpliedVolatility); sp != nil {
				view.SurfacePoint = sp
				payload.Surface = append(payload.Surface, *sp)
			}
		}

		if s.polygonService != nil {
			g, gErr := s.polygonService.GetOptionGreeks(r.Context(), opt)
			if gErr != nil {
				payload.Warning = appendWarning(payload.Warning, fmt.Sprintf("Greeks unavailable: %v", gErr))
			}
			if g != nil {
				// Prefer IBKR if available; otherwise use Polygon as fallback
				if view.Greeks == nil {
					view.Greeks = g
					view.ImpliedVol = g.ImpliedVolatility
					view.DataSource = "Polygon"
				}
				if view.SurfacePoint == nil {
					if sp := makeSurfacePoint(opt.Symbol, opt.Type, view.Expiration, opt.Expiration.UnixMilli(), opt.Strike, opt.Contracts, g.ImpliedVolatility); sp != nil {
						view.SurfacePoint = sp
						payload.Surface = append(payload.Surface, *sp)
					}
				}
			}
		}

		if view.DataSource == "" && view.Greeks != nil {
			view.DataSource = "Polygon"
		}
		if view.DataSource == "" {
			view.DataSource = "Unavailable"
		}

		payload.Options = append(payload.Options, view)
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(payload)
}

type ibkrGreekOption struct {
	Symbol            string                `json:"symbol"`
	Right             string                `json:"right,omitempty"`
	Type              string                `json:"type,omitempty"`
	Strike            float64               `json:"strike"`
	Expiration        string                `json:"expiration"`
	Greeks            *polygon.OptionGreeks `json:"greeks,omitempty"`
	ImpliedVolatility *float64              `json:"implied_volatility,omitempty"`
	DataSource        string                `json:"data_source,omitempty"`
}

type ibkrGreekResponse struct {
	Options []ibkrGreekOption `json:"options"`
	Errors  []string          `json:"errors"`
}

func optionKey(symbol, optionType string, strike float64, expiration string) string {
	return fmt.Sprintf("%s|%s|%.4f|%s", strings.ToUpper(symbol), strings.ToUpper(optionType), strike, expiration)
}

func appendWarning(existing, newMsg string) string {
	if existing == "" {
		return newMsg
	}
	return existing + "; " + newMsg
}

func makeSurfacePoint(symbol, optionType, expiration string, expiryMs int64, strike float64, contracts int, iv *float64) *VolSurfacePoint {
	if iv == nil {
		return nil
	}
	return &VolSurfacePoint{
		Symbol:      symbol,
		Strike:      strike,
		Expiration:  expiration,
		ExpiryMs:    expiryMs,
		IV:          iv,
		IsOwned:     true,
		OptionType:  optionType,
		Contracts:   contracts,
		Description: fmt.Sprintf("%s %s %.2f", symbol, optionType, strike),
	}
}

func (s *Server) fetchIBKRGreeks(ctx context.Context) (map[string]ibkrGreekOption, string) {
	result := make(map[string]ibkrGreekOption)

	config := s.ibkrConnectionConfig()
	query := url.Values{}
	if config.Host != "" {
		query.Set("host", config.Host)
	}
	if config.Port > 0 {
		query.Set("port", strconv.Itoa(config.Port))
	}
	if config.ClientID > 0 {
		query.Set("client_id", strconv.Itoa(config.ClientID))
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, getIBKRServiceURL()+"/api/ibkr/greeks?"+query.Encode(), nil)
	if err != nil {
		return result, fmt.Sprintf("Failed to build IBKR Greeks request: %v", err)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return result, fmt.Sprintf("IBKR Greeks unavailable: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return result, fmt.Sprintf("IBKR Greeks service responded with %d", resp.StatusCode)
	}

	var payload ibkrGreekResponse
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return result, fmt.Sprintf("Failed to parse IBKR Greeks response: %v", err)
	}

	for _, opt := range payload.Options {
		optionType := opt.Type
		if optionType == "" && opt.Right != "" {
			if strings.ToUpper(opt.Right) == "C" {
				optionType = "Call"
			} else if strings.ToUpper(opt.Right) == "P" {
				optionType = "Put"
			}
		}
		if optionType == "" {
			continue
		}
		key := optionKey(opt.Symbol, optionType, opt.Strike, opt.Expiration)
		result[key] = opt
	}

	if len(payload.Errors) > 0 {
		return result, strings.Join(payload.Errors, "; ")
	}

	return result, ""
}

// ibkrDisconnectHandler proxies disconnect request to IBKR microservice
func (s *Server) ibkrDisconnectHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	log.Printf("[IBKR API] Handling disconnect request")

	// Forward request to IBKR microservice
	serviceURL := getIBKRServiceURL() + "/api/ibkr/disconnect"
	resp, err := http.Post(serviceURL, "application/json", nil)
	if err != nil {
		log.Printf("[IBKR API] Error connecting to IBKR service: %v", err)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"success": false,
			"error":   "IBKR service unavailable: " + err.Error(),
		})
		return
	}
	defer resp.Body.Close()

	// Forward response back to client
	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Printf("[IBKR API] Error reading response: %v", err)
		http.Error(w, "Failed to read response", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.Write(responseBody)
}
