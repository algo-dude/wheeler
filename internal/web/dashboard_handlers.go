package web

import (
	"encoding/json"
	"io"
	"log"
	"math"
	"net/http"
	"sort"
	"stonks/internal/models"
)

// dashboardHandler serves the TraderVue-style dashboard
func (s *Server) dashboardHandler(w http.ResponseWriter, r *http.Request) {
	symbols, err := s.symbolService.GetDistinctSymbols()
	if err != nil {
		log.Printf("[DASHBOARD] Error getting symbols: %v", err)
		symbols = []string{}
	}

	// Sort symbols alphabetically for consistent legend colors across charts
	sort.Strings(symbols)

	// Parse selected accounts from query parameters
	selectedAccounts := r.URL.Query()["account"]

	log.Printf("[DASHBOARD] Found %d symbols for navigation: %v", len(symbols), symbols)

	// Build comprehensive dashboard data
	data, err := s.buildDashboardData(symbols, selectedAccounts)
	if err != nil {
		log.Printf("Error building dashboard data: %v", err)
		// Fallback to basic data structure
		data = DashboardData{
			Symbols:    symbols,
			AllSymbols: symbols, // For navigation compatibility
			CurrentDB:  s.getCurrentDatabaseName(),
			ActivePage: "dashboard",
		}
	}

	s.renderTemplate(w, "dashboard.html", data)
}

// buildDashboardData creates comprehensive dashboard data
func (s *Server) buildDashboardData(symbols []string, selectedAccounts []string) (DashboardData, error) {
	// Get all data
	options, _ := s.optionService.GetAll()
	longPositions, _ := s.longPositionService.GetAll()
	dividends, _ := s.dividendService.GetAll()
	treasuries, _ := s.treasuryService.GetAll()

	// Get distinct IBKR accounts from both options and long positions
	optionAccounts, _ := s.optionService.GetDistinctAccounts()
	positionAccounts, _ := s.longPositionService.GetDistinctAccounts()

	// Merge and deduplicate accounts
	accountSet := make(map[string]bool)
	for _, acc := range optionAccounts {
		accountSet[acc] = true
	}
	for _, acc := range positionAccounts {
		accountSet[acc] = true
	}
	var ibkrAccounts []string
	for acc := range accountSet {
		ibkrAccounts = append(ibkrAccounts, acc)
	}
	sort.Strings(ibkrAccounts)

	hasIBKRData := len(ibkrAccounts) > 0

	// Get account nicknames
	accountNicknames, err := models.GetAllAccountNicknames(s.db)
	if err != nil {
		log.Printf("[DASHBOARD] Error getting account nicknames: %v", err)
		accountNicknames = make(map[string]string)
	}

	// Filter options and long positions by selected accounts if any are selected
	if len(selectedAccounts) > 0 {
		selectedSet := make(map[string]bool)
		for _, acc := range selectedAccounts {
			selectedSet[acc] = true
		}

		var filteredOptions []*models.Option
		for _, opt := range options {
			// Include options without account (manual entries) or matching selected accounts
			if opt.Account == nil || *opt.Account == "" || selectedSet[*opt.Account] {
				filteredOptions = append(filteredOptions, opt)
			}
		}
		options = filteredOptions

		var filteredPositions []*models.LongPosition
		for _, pos := range longPositions {
			// Include positions without account (manual entries) or matching selected accounts
			if pos.Account == nil || *pos.Account == "" || selectedSet[*pos.Account] {
				filteredPositions = append(filteredPositions, pos)
			}
		}
		longPositions = filteredPositions
	}

	// Build symbol summaries
	symbolSummaries := s.buildSymbolSummaries(symbols, options, longPositions, dividends)

	// Build chart data
	longByTicker := s.buildLongByTickerChart(longPositions)
	putsByTicker := s.buildPutsByTickerChart(options)
	totalAllocation := s.buildTotalAllocationChart(longPositions, options, treasuries)

	// Calculate totals
	totals := s.calculateDashboardTotals(symbolSummaries, treasuries)

	// Fetch margin data from IBKR status if available
	accountMargins, totalMargin := s.fetchMarginData()

	log.Printf("[DASHBOARD] Building dashboard data with %d symbols: %v", len(symbols), symbols)
	log.Printf("[DASHBOARD] Built %d symbol summaries", len(symbolSummaries))

	return DashboardData{
		Symbols:          symbols,
		AllSymbols:       symbols, // For navigation compatibility
		SymbolSummaries:  symbolSummaries,
		LongByTicker:     longByTicker,
		PutsByTicker:     putsByTicker,
		TotalAllocation:  totalAllocation,
		Totals:           totals,
		CurrentDB:        s.getCurrentDatabaseName(),
		ActivePage:       "dashboard",
		IBKRAccounts:     ibkrAccounts,
		SelectedAccounts: selectedAccounts,
		HasIBKRData:      hasIBKRData,
		AccountNicknames: accountNicknames,
		AccountMargins:   accountMargins,
		TotalMargin:      totalMargin,
	}, nil
}

// fetchMarginData retrieves maintenance margin data from IBKR status endpoint
func (s *Server) fetchMarginData() (map[string]float64, float64) {
	accountMargins := make(map[string]float64)
	var totalMargin float64

	// Call IBKR status endpoint
	serviceURL := getIBKRServiceURL() + "/api/ibkr/status"
	resp, err := http.Get(serviceURL)
	if err != nil {
		log.Printf("[DASHBOARD] Error fetching IBKR status for margin: %v", err)
		return accountMargins, totalMargin
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Printf("[DASHBOARD] Error reading IBKR status response: %v", err)
		return accountMargins, totalMargin
	}

	// Parse the status response - account_summary is now keyed by account ID
	var statusResp struct {
		AccountSummary map[string]map[string]struct {
			Value    float64 `json:"value"`
			Currency string  `json:"currency"`
		} `json:"account_summary"`
	}
	if err := json.Unmarshal(body, &statusResp); err != nil {
		log.Printf("[DASHBOARD] Error parsing IBKR status response: %v", err)
		return accountMargins, totalMargin
	}

	// Extract MaintMarginReq per account
	if statusResp.AccountSummary != nil {
		for account, values := range statusResp.AccountSummary {
			if margin, ok := values["MaintMarginReq"]; ok {
				accountMargins[account] = margin.Value
				totalMargin += margin.Value
				log.Printf("[DASHBOARD] Account %s maintenance margin: $%.2f", account, margin.Value)
			}
		}
		if totalMargin > 0 {
			log.Printf("[DASHBOARD] Total maintenance margin: $%.2f", totalMargin)
		}
	}

	return accountMargins, totalMargin
}

func (s *Server) buildSymbolSummaries(symbols []string, options []*models.Option, longPositions []*models.LongPosition, dividends []*models.Dividend) []SymbolSummary {
	summaryMap := make(map[string]*SymbolSummary)

	// Initialize all symbols with current prices from database
	for _, symbol := range symbols {
		currentPrice := 0.0
		if symbolData, err := s.symbolService.GetBySymbol(symbol); err == nil {
			currentPrice = symbolData.Price
		}

		summaryMap[symbol] = &SymbolSummary{
			Ticker:       symbol,
			CurrentPrice: currentPrice,
		}
	}

	// Process long positions
	for _, pos := range longPositions {
		if summary, exists := summaryMap[pos.Symbol]; exists {
			// Only count current open positions for LongAmount
			if pos.Closed == nil {
				summary.LongAmount += pos.CalculateAmount()
			}
			// Only count realized gains from closed positions
			if pos.ExitPrice != nil && pos.Closed != nil {
				summary.CapGains += pos.CalculateProfitLoss(*pos.ExitPrice)
			}
		}
	}

	// Build map of symbols with open call coverage for optionable calculation
	callCoverage := make(map[string]bool)
	
	// Process options
	for _, opt := range options {
		if summary, exists := summaryMap[opt.Symbol]; exists {
			if opt.Type == "Put" {
				// Count put exposure for all open puts
				if opt.Closed == nil {
					summary.PutExposed += opt.Strike * math.Abs(float64(opt.Contracts)) * 100
				}
				// Count premium for all puts (closed and open)
				premium := opt.CalculateTotalProfit()
				summary.Puts += premium
			} else {
				// Count premium for all calls (closed and open)
				premium := opt.CalculateTotalProfit()
				summary.Calls += premium
				// Track call coverage for open calls
				if opt.Closed == nil {
					callCoverage[opt.Symbol] = true
				}
			}
		}
	}

	// Calculate optionable amounts (long positions without call coverage and with 100+ shares)
	for _, pos := range longPositions {
		if summary, exists := summaryMap[pos.Symbol]; exists {
			if pos.Closed == nil { // Only open positions
				if !callCoverage[pos.Symbol] && pos.Shares >= 100 { // No call coverage and enough shares for options
					summary.Optionable += pos.CalculateAmount()
				}
			}
		}
	}

	// Process dividends
	for _, div := range dividends {
		if summary, exists := summaryMap[div.Symbol]; exists {
			summary.Dividends += div.Amount
		}
	}

	// Calculate net and cash on cash, filter out symbols with no trade data
	var summaries []SymbolSummary
	for _, summary := range summaryMap {
		summary.Net = summary.CapGains + summary.Puts + summary.Calls + summary.Dividends
		if summary.LongAmount > 0 {
			summary.CashOnCash = (summary.Net / summary.LongAmount) * 100
		}

		// Only include symbols that have some trade activity
		if summary.LongAmount > 0 || summary.PutExposed > 0 || summary.Puts != 0 ||
			summary.Calls != 0 || summary.CapGains != 0 || summary.Dividends > 0 {
			summaries = append(summaries, *summary)
		}
	}

	return summaries
}

func (s *Server) buildLongByTickerChart(longPositions []*models.LongPosition) []ChartData {
	tickerAmounts := make(map[string]float64)
	colors := []string{"#FF6384", "#36A2EB", "#FFCE56", "#4BC0C0", "#9966FF", "#FF9F40"}

	for _, pos := range longPositions {
		if pos.Closed == nil { // Only include open positions
			tickerAmounts[pos.Symbol] += pos.CalculateAmount()
		}
	}

	// Sort tickers alphabetically for consistent legend colors
	var tickers []string
	for ticker := range tickerAmounts {
		tickers = append(tickers, ticker)
	}
	sort.Strings(tickers)

	var chartData []ChartData
	for i, ticker := range tickers {
		chartData = append(chartData, ChartData{
			Label: ticker,
			Value: tickerAmounts[ticker],
			Color: colors[i%len(colors)],
		})
	}

	return chartData
}

func (s *Server) buildPutsByTickerChart(options []*models.Option) []ChartData {
	putExposure := make(map[string]float64)
	colors := []string{"#FF6384", "#36A2EB", "#FFCE56", "#4BC0C0", "#9966FF", "#FF9F40"}

	for _, opt := range options {
		if opt.Type == "Put" && opt.Closed == nil { // Only include open puts
			putExposure[opt.Symbol] += opt.Strike * math.Abs(float64(opt.Contracts)) * 100
		}
	}

	// Sort tickers alphabetically for consistent legend colors
	var tickers []string
	for ticker := range putExposure {
		tickers = append(tickers, ticker)
	}
	sort.Strings(tickers)

	var chartData []ChartData
	for i, ticker := range tickers {
		chartData = append(chartData, ChartData{
			Label: ticker,
			Value: putExposure[ticker],
			Color: colors[i%len(colors)],
		})
	}

	return chartData
}

func (s *Server) buildTotalAllocationChart(longPositions []*models.LongPosition, options []*models.Option, treasuries []*models.Treasury) []ChartData {
	var totalLong, totalPuts, totalTreasuries float64

	// Only count open long positions for current allocation
	for _, pos := range longPositions {
		if pos.Closed == nil {
			totalLong += pos.CalculateAmount()
		}
	}

	// Only count open put options for current exposure
	for _, opt := range options {
		if opt.Type == "Put" && opt.Closed == nil {
			totalPuts += opt.Strike * math.Abs(float64(opt.Contracts)) * 100
		}
	}

	// Count all treasury holdings (bonds are typically held to maturity)
	for _, treasury := range treasuries {
		totalTreasuries += treasury.Amount
	}

	return []ChartData{
		{Label: "Long Stock", Value: totalLong, Color: "#36A2EB"},
		{Label: "Put Exposure", Value: totalPuts, Color: "#FF6384"},
		{Label: "Treasuries", Value: totalTreasuries, Color: "#FFCE56"},
	}
}

func (s *Server) calculateDashboardTotals(symbolSummaries []SymbolSummary, treasuries []*models.Treasury) DashboardTotals {
	var totalLong, totalPuts, totalPutPremiums, totalCallPremiums, totalCapGains, totalDividends, totalTreasuries, totalOptionable float64

	// Sum from symbol summaries
	for _, summary := range symbolSummaries {
		totalLong += summary.LongAmount
		totalPuts += summary.PutExposed
		totalPutPremiums += summary.Puts
		totalCallPremiums += summary.Calls
		totalCapGains += summary.CapGains
		totalDividends += summary.Dividends
		totalOptionable += summary.Optionable
	}

	// Sum treasuries
	for _, treasury := range treasuries {
		totalTreasuries += treasury.Amount
	}

	totalNet := totalPutPremiums + totalCallPremiums + totalCapGains + totalDividends
	overallCashOnCash := 0.0
	if totalLong > 0 {
		overallCashOnCash = (totalNet / totalLong) * 100
	}

	// Calculate Put ROI: sum premium of all open Puts divided by Put exposure
	putROI := 0.0
	if totalPuts > 0 {
		putROI = (totalPutPremiums / totalPuts) * 100
	}

	// Calculate Long ROI: sum premiums of all open Calls divided by Total Long
	longROI := 0.0
	if totalLong > 0 {
		longROI = (totalCallPremiums / totalLong) * 100
	}

	return DashboardTotals{
		TotalLong:         totalLong,
		TotalPuts:         totalPuts,
		TotalTreasuries:   totalTreasuries,
		TotalPutPremiums:  totalPutPremiums,
		TotalCallPremiums: totalCallPremiums,
		TotalCapGains:     totalCapGains,
		TotalDividends:    totalDividends,
		TotalNet:          totalNet,
		OverallCashOnCash: overallCashOnCash,
		PutROI:            putROI,
		LongROI:           longROI,
		GrandTotal:        totalLong + totalPuts + totalTreasuries,
		TotalOptionable:   totalOptionable,
	}
}

// premiumDataHandler returns premium data for charts
func (s *Server) premiumDataHandler(w http.ResponseWriter, r *http.Request) {
	options, err := s.optionService.GetAll()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	var putPremium, callPremium float64
	for _, option := range options {
		totalPremium := option.Premium * math.Abs(float64(option.Contracts)) * 100

		if option.Type == "Put" {
			putPremium += totalPremium
		} else if option.Type == "Call" {
			callPremium += totalPremium
		}
	}

	data := PremiumData{
		PutPremium:  putPremium,
		CallPremium: callPremium,
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(data)
}

// allocationDataHandler returns allocation data for charts
func (s *Server) allocationDataHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Get open treasuries (no exit price)
	treasuries, err := s.treasuryService.GetAll()
	if err != nil {
		log.Printf("[ALLOCATION API] Error getting treasuries: %v", err)
		http.Error(w, "Failed to get treasuries", http.StatusInternalServerError)
		return
	}

	var totalTreasuries float64
	for _, treasury := range treasuries {
		// Only count open positions (no exit price)
		if treasury.ExitPrice == nil {
			totalTreasuries += treasury.Amount
		}
	}

	// Get open long positions (no exit price)
	longPositions, err := s.longPositionService.GetAll()
	if err != nil {
		log.Printf("[ALLOCATION API] Error getting long positions: %v", err)
		http.Error(w, "Failed to get long positions", http.StatusInternalServerError)
		return
	}

	var totalLong float64
	longByTicker := make(map[string]float64)
	for _, pos := range longPositions {
		if pos.Closed == nil { // Only open positions
			amount := pos.CalculateAmount()
			totalLong += amount
			longByTicker[pos.Symbol] += amount
		}
	}

	// Get open put options
	options, err := s.optionService.GetAll()
	if err != nil {
		log.Printf("[ALLOCATION API] Error getting options: %v", err)
		http.Error(w, "Failed to get options", http.StatusInternalServerError)
		return
	}

	var totalPuts, totalPutPremiums, totalCallPremiums float64
	putsByTicker := make(map[string]float64)
	callCoverage := make(map[string]bool)
	
	for _, opt := range options {
		if opt.Closed == nil { // Only open options
			if opt.Type == "Put" {
				exposure := opt.Strike * math.Abs(float64(opt.Contracts)) * 100
				totalPuts += exposure
				putsByTicker[opt.Symbol] += exposure
				totalPutPremiums += opt.Premium * math.Abs(float64(opt.Contracts)) * 100
			} else if opt.Type == "Call" {
				totalCallPremiums += opt.Premium * math.Abs(float64(opt.Contracts)) * 100
				callCoverage[opt.Symbol] = true
			}
		}
	}

	// Calculate optionable positions (long positions without call coverage and with 100+ shares)
	var totalOptionable, totalCallCovered float64
	for _, pos := range longPositions {
		if pos.Closed == nil { // Only open positions
			amount := pos.CalculateAmount()
			if callCoverage[pos.Symbol] {
				totalCallCovered += amount
			} else if pos.Shares >= 100 { // Only count positions with enough shares for options
				totalOptionable += amount
			}
		}
	}

	log.Printf("[ALLOCATION API] Calculated totals - Long: $%.2f, Puts: $%.2f, Treasuries: $%.2f", totalLong, totalPuts, totalTreasuries)

	// Build response data
	var longByTickerChart []ChartData
	var putsByTickerChart []ChartData
	colors := []string{"#FF6384", "#36A2EB", "#FFCE56", "#4BC0C0", "#9966FF", "#FF9F40"}

	// Sort tickers for consistent colors
	longTickers := make([]string, 0, len(longByTicker))
	for ticker := range longByTicker {
		longTickers = append(longTickers, ticker)
	}
	sort.Strings(longTickers)

	putTickers := make([]string, 0, len(putsByTicker))
	for ticker := range putsByTicker {
		putTickers = append(putTickers, ticker)
	}
	sort.Strings(putTickers)

	// Build chart data
	for i, ticker := range longTickers {
		longByTickerChart = append(longByTickerChart, ChartData{
			Label: ticker,
			Value: longByTicker[ticker],
			Color: colors[i%len(colors)],
		})
	}

	for i, ticker := range putTickers {
		putsByTickerChart = append(putsByTickerChart, ChartData{
			Label: ticker,
			Value: putsByTicker[ticker],
			Color: colors[i%len(colors)],
		})
	}

	totalAllocation := []ChartData{
		{Label: "Long Stock", Value: totalLong, Color: "#36A2EB"},
		{Label: "Put Exposure", Value: totalPuts, Color: "#FF6384"},
		{Label: "Treasuries", Value: totalTreasuries, Color: "#FFCE56"},
	}

	callsToLongs := []ChartData{
		{Label: "Call Covered", Value: totalCallCovered, Color: "#36A2EB"},
		{Label: "Optionable", Value: totalOptionable, Color: "#FFCE56"},
	}

	// Calculate ROI values
	putROI := 0.0
	if totalPuts > 0 {
		putROI = (totalPutPremiums / totalPuts) * 100
	}

	longROI := 0.0
	if totalLong > 0 {
		longROI = (totalCallPremiums / totalLong) * 100
	}

	response := AllocationData{
		LongByTicker:      longByTickerChart,
		PutsByTicker:      putsByTickerChart,
		CallsToLongs:      callsToLongs,
		TotalAllocation:   totalAllocation,
		PutROI:            putROI,
		LongROI:           longROI,
		TotalPutPremiums:  totalPutPremiums,
		TotalCallPremiums: totalCallPremiums,
		TotalCallCovered:  totalCallCovered,
		TotalOptionable:   totalOptionable,
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(response); err != nil {
		log.Printf("[ALLOCATION API] Error encoding response: %v", err)
		http.Error(w, "Failed to encode response", http.StatusInternalServerError)
	}
}

// optionablePositionsHandler returns long positions that have no open call coverage
func (s *Server) optionablePositionsHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Get all open long positions
	longPositions, err := s.longPositionService.GetAll()
	if err != nil {
		log.Printf("[OPTIONABLE API] Error getting long positions: %v", err)
		http.Error(w, "Failed to get long positions", http.StatusInternalServerError)
		return
	}

	// Get all open call options
	options, err := s.optionService.GetAll()
	if err != nil {
		log.Printf("[OPTIONABLE API] Error getting options: %v", err)
		http.Error(w, "Failed to get options", http.StatusInternalServerError)
		return
	}

	// Build map of symbols with open call coverage
	callCoverage := make(map[string]bool)
	for _, opt := range options {
		if opt.Type == "Call" && opt.Closed == nil {
			callCoverage[opt.Symbol] = true
		}
	}

	// Find long positions without call coverage
	type OptionablePosition struct {
		Symbol     string  `json:"symbol"`
		Shares     int     `json:"shares"`
		Amount     float64 `json:"amount"`
		BuyPrice   float64 `json:"buyPrice"`
		Opened     string  `json:"opened"`
		CurrentValue float64 `json:"currentValue,omitempty"`
	}

	var optionablePositions []OptionablePosition
	totalOptionableValue := 0.0

	for _, pos := range longPositions {
		if pos.Closed == nil { // Only open positions
			if !callCoverage[pos.Symbol] && pos.Shares >= 100 { // No call coverage and enough shares for options
				amount := pos.CalculateAmount()
				
				// Get current price for value calculation
				currentPrice := pos.BuyPrice // fallback to buy price
				if symbolData, err := s.symbolService.GetBySymbol(pos.Symbol); err == nil {
					currentPrice = symbolData.Price
				}
				
				optionablePositions = append(optionablePositions, OptionablePosition{
					Symbol:       pos.Symbol,
					Shares:       pos.Shares,
					Amount:       amount,
					BuyPrice:     pos.BuyPrice,
					Opened:       pos.Opened.Format("2006-01-02"),
					CurrentValue: float64(pos.Shares) * currentPrice,
				})
				totalOptionableValue += amount
			}
		}
	}

	log.Printf("[OPTIONABLE API] Found %d optionable positions worth $%.2f", len(optionablePositions), totalOptionableValue)

	response := map[string]interface{}{
		"positions":   optionablePositions,
		"totalValue":  totalOptionableValue,
		"count":       len(optionablePositions),
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(response); err != nil {
		log.Printf("[OPTIONABLE API] Error encoding response: %v", err)
		http.Error(w, "Failed to encode response", http.StatusInternalServerError)
	}
}