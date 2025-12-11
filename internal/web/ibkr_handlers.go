package web

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
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

// ibkrSettingsHandler serves the IBKR settings management page
func (s *Server) ibkrSettingsHandler(w http.ResponseWriter, r *http.Request) {
	log.Printf("[IBKR SETTINGS] Handling IBKR settings page request")

	// Get all symbols for navigation
	symbols, err := s.symbolService.GetDistinctSymbols()
	if err != nil {
		log.Printf("[IBKR SETTINGS] Error getting symbols: %v", err)
		symbols = []string{}
	}

	// Get IBKR settings from settings table
	twsHost := s.settingService.GetValueWithDefault("IBKR_TWS_HOST", "127.0.0.1")
	twsPort := s.settingService.GetValueWithDefault("IBKR_TWS_PORT", "7497")
	clientID := s.settingService.GetValueWithDefault("IBKR_CLIENT_ID", "1")

	data := IBKRSettingsData{
		AllSymbols:     symbols,
		CurrentDB:      s.getCurrentDatabaseName(),
		ActivePage:     "settings-ibkr",
		TWS_Host:       twsHost,
		TWS_Port:       twsPort,
		ClientID:       clientID,
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

	// Forward request to IBKR microservice
	serviceURL := getIBKRServiceURL() + "/api/ibkr/test"
	resp, err := http.Post(serviceURL, "application/json", nil)
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

	// Save connection config to settings if provided in original request
	if len(body) > 0 {
		var config IBKRConnectionConfig
		if err := json.Unmarshal(body, &config); err == nil {
			if config.Host != "" {
				s.settingService.SetValue("IBKR_TWS_HOST", config.Host, "IBKR TWS/Gateway hostname")
			}
			if config.Port > 0 {
				s.settingService.SetValue("IBKR_TWS_PORT", string(rune(config.Port)), "IBKR TWS/Gateway port")
			}
			if config.ClientID > 0 {
				s.settingService.SetValue("IBKR_CLIENT_ID", string(rune(config.ClientID)), "IBKR client ID")
			}
		}
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

	// Forward request to IBKR microservice
	serviceURL := getIBKRServiceURL() + "/api/ibkr/sync"
	resp, err := http.Post(serviceURL, "application/json", nil)
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
