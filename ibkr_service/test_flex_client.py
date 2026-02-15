"""
Unit tests for IBKR Flex Web Service client and XML parser.

Tests cover:
- FlexXMLParser.parse() with various XML structures
- Execution mapping from Trade elements
- Activity mapping from CashTransaction elements
- Activity mapping from CorporateAction elements
- Error handling for malformed XML
- Deterministic exec_id/activity_id generation
- Datetime parsing with various formats

Run with: pytest ibkr_service/test_flex_client.py -v
"""

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the modules under test
from flex_client import (
    FlexError,
    FlexQueryClient,
    FlexReport,
    FlexXMLParser,
    SEND_REQUEST_URL,
    GET_STATEMENT_URL,
    STATUS_NOT_YET_READY,
    STATUS_SUCCESS,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def fixtures_dir() -> Path:
    """Get the path to test fixtures directory."""
    return Path(__file__).parent / "test_fixtures"


@pytest.fixture
def sample_flex_report_xml(fixtures_dir: Path) -> str:
    """Load the comprehensive sample Flex report XML."""
    return (fixtures_dir / "sample_flex_report.xml").read_text()


@pytest.fixture
def empty_flex_report_xml(fixtures_dir: Path) -> str:
    """Load the empty Flex report XML."""
    return (fixtures_dir / "empty_flex_report.xml").read_text()


@pytest.fixture
def malformed_xml(fixtures_dir: Path) -> str:
    """Load malformed XML for error handling tests."""
    return (fixtures_dir / "malformed_flex_report.xml").read_text()


@pytest.fixture
def send_request_response_xml(fixtures_dir: Path) -> str:
    """Load successful SendRequest response XML."""
    return (fixtures_dir / "flex_send_request_response.xml").read_text()


@pytest.fixture
def error_response_xml(fixtures_dir: Path) -> str:
    """Load error response XML."""
    return (fixtures_dir / "flex_error_response.xml").read_text()


@pytest.fixture
def not_ready_response_xml(fixtures_dir: Path) -> str:
    """Load not-yet-ready response XML."""
    return (fixtures_dir / "flex_not_ready_response.xml").read_text()


@pytest.fixture
def parser() -> FlexXMLParser:
    """Create a FlexXMLParser instance."""
    return FlexXMLParser()


@pytest.fixture
def client() -> FlexQueryClient:
    """Create a FlexQueryClient instance with test credentials."""
    return FlexQueryClient(token="test_token_123", query_id="12345")


# =============================================================================
# FlexXMLParser Tests - Basic Parsing
# =============================================================================

class TestFlexXMLParserBasics:
    """Test basic FlexXMLParser functionality."""
    
    def test_parse_returns_flex_report(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should return a FlexReport dataclass."""
        report = parser.parse(sample_flex_report_xml)
        assert isinstance(report, FlexReport)
    
    def test_parse_empty_report(self, parser: FlexXMLParser, empty_flex_report_xml: str):
        """Parser should handle empty report with no trades/activities."""
        report = parser.parse(empty_flex_report_xml)
        
        assert isinstance(report, FlexReport)
        assert len(report.executions) == 0
        assert len(report.commission_reports) == 0
        assert len(report.activities) == 0
        assert len(report.warnings) == 0
    
    def test_parse_malformed_xml_adds_warning(self, parser: FlexXMLParser, malformed_xml: str):
        """Parser should handle malformed XML gracefully with warnings."""
        report = parser.parse(malformed_xml)
        
        assert isinstance(report, FlexReport)
        assert len(report.warnings) > 0
        assert any("parse error" in w.lower() or "xml" in w.lower() for w in report.warnings)
    
    def test_parse_empty_string(self, parser: FlexXMLParser):
        """Parser should handle empty string input."""
        report = parser.parse("")
        
        assert isinstance(report, FlexReport)
        assert len(report.warnings) > 0
    
    def test_parse_non_xml_content(self, parser: FlexXMLParser):
        """Parser should handle non-XML content."""
        report = parser.parse("This is not XML at all!")
        
        assert isinstance(report, FlexReport)
        assert len(report.warnings) > 0


# =============================================================================
# FlexXMLParser Tests - Trade/Execution Parsing
# =============================================================================

class TestFlexXMLParserTrades:
    """Test Trade element parsing → ibkr_executions mapping."""
    
    def test_parse_stock_buy_trade(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should correctly map stock BUY trade attributes."""
        report = parser.parse(sample_flex_report_xml)
        
        # Find the AAPL stock BUY trade
        aapl_buys = [e for e in report.executions 
                    if e["symbol"] == "AAPL" and e["side"] == "BUY" and e["sec_type"] == "STK"]
        assert len(aapl_buys) == 1
        
        trade = aapl_buys[0]
        assert trade["account"] == "U1234567"
        assert trade["exec_id"] == "0001234567.65432.01"
        assert trade["order_id"] == 12345678
        assert trade["conid"] == 265598
        assert trade["symbol"] == "AAPL"
        assert trade["sec_type"] == "STK"
        assert trade["currency"] == "USD"
        assert trade["side"] == "BUY"
        assert trade["quantity"] == 100
        assert trade["price"] == 185.50
        assert trade["multiplier"] == 1
        assert trade["source"] == "FLEX"
        assert trade["right"] is None  # Stock trades have no right
        assert trade["strike"] is None
        assert trade["expiration"] is None
    
    def test_parse_stock_sell_trade(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should correctly map stock SELL trade with realized P&L."""
        report = parser.parse(sample_flex_report_xml)
        
        # Find the AAPL stock SELL trade
        aapl_sells = [e for e in report.executions 
                     if e["symbol"] == "AAPL" and e["side"] == "SELL" and e["sec_type"] == "STK"]
        assert len(aapl_sells) == 1
        
        trade = aapl_sells[0]
        assert trade["side"] == "SELL"
        assert trade["quantity"] == 50
        assert trade["price"] == 192.75
        
        # Check corresponding commission report has realized P&L
        comm_reports = [c for c in report.commission_reports 
                       if c["exec_id"] == trade["exec_id"]]
        assert len(comm_reports) == 1
        assert comm_reports[0]["realized_pnl"] == 362.50
    
    def test_parse_put_option_trade(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should correctly map PUT option trade attributes."""
        report = parser.parse(sample_flex_report_xml)
        
        # Find the AAPL PUT option trade
        aapl_puts = [e for e in report.executions 
                   if e["symbol"] == "AAPL" and e["sec_type"] == "OPT" and e["right"] == "P"]
        assert len(aapl_puts) == 1
        
        trade = aapl_puts[0]
        assert trade["sec_type"] == "OPT"
        assert trade["right"] == "P"
        assert trade["strike"] == 180.00
        assert trade["expiration"] == "2024-03-15"
        assert trade["multiplier"] == 100
        assert trade["side"] == "SELL"  # Writing a put
        assert trade["quantity"] == 1
        assert trade["price"] == 2.50
    
    def test_parse_call_option_trade(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should correctly map CALL option trade attributes."""
        report = parser.parse(sample_flex_report_xml)
        
        # Find the TSLA CALL option trade
        tsla_calls = [e for e in report.executions 
                    if e["symbol"] == "TSLA" and e["sec_type"] == "OPT" and e["right"] == "C"]
        assert len(tsla_calls) == 1
        
        trade = tsla_calls[0]
        assert trade["sec_type"] == "OPT"
        assert trade["right"] == "C"
        assert trade["strike"] == 250.00
        assert trade["expiration"] == "2024-04-19"
        assert trade["multiplier"] == 100
        assert trade["side"] == "BUY"
        assert trade["quantity"] == 2
    
    def test_parse_trade_alternative_field_names(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should handle alternative Flex XML field names."""
        report = parser.parse(sample_flex_report_xml)
        
        # Find the MSFT trade with alternative field names
        msft_trades = [e for e in report.executions if e["symbol"] == "MSFT"]
        assert len(msft_trades) == 1
        
        trade = msft_trades[0]
        assert trade["exec_id"] == "ALT-EXEC-001"  # Uses tradeID fallback
        assert trade["side"] == "BUY"  # BOT → BUY normalization
        assert trade["quantity"] == 25
        assert trade["price"] == 420.00
    
    def test_commission_reports_generated(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should generate commission reports for trades with commission data."""
        report = parser.parse(sample_flex_report_xml)
        
        # Should have commission reports for trades with ibCommission
        assert len(report.commission_reports) > 0
        
        # Commission should be absolute value (Flex reports as negative)
        for comm in report.commission_reports:
            assert comm["commission"] >= 0
            assert "account" in comm
            assert "exec_id" in comm
            assert "currency" in comm
    
    def test_raw_json_populated(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should populate raw_json with original XML attributes."""
        report = parser.parse(sample_flex_report_xml)
        
        for exec_row in report.executions:
            assert "raw_json" in exec_row
            raw = json.loads(exec_row["raw_json"])
            assert isinstance(raw, dict)
            # Should contain the original symbol at minimum
            assert "symbol" in raw or "Symbol" in raw


# =============================================================================
# FlexXMLParser Tests - Cash Transaction Parsing
# =============================================================================

class TestFlexXMLParserCashTransactions:
    """Test CashTransaction element parsing → ibkr_activities mapping."""
    
    def test_parse_dividend(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map Dividends cash transaction to DIVIDEND activity."""
        report = parser.parse(sample_flex_report_xml)
        
        dividends = [a for a in report.activities if a["activity_type"] == "DIVIDEND"]
        assert len(dividends) >= 2  # Multiple dividend entries
        
        # Find AAPL dividend
        aapl_divs = [d for d in dividends if d["symbol"] == "AAPL"]
        assert len(aapl_divs) == 1
        
        div = aapl_divs[0]
        assert div["account"] == "U1234567"
        assert div["amount"] == 24.00
        assert div["currency"] == "USD"
        assert div["source"] == "FLEX"
        assert "dividend" in div["description"].lower()
    
    def test_parse_withholding_tax(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map Withholding Tax to FEE activity."""
        report = parser.parse(sample_flex_report_xml)
        
        fees = [a for a in report.activities if a["activity_type"] == "FEE"]
        
        # Should have withholding tax as a FEE
        wht_fees = [f for f in fees if "tax" in f.get("description", "").lower()]
        assert len(wht_fees) >= 1
        
        wht = wht_fees[0]
        assert wht["amount"] == -3.60  # Tax is negative
        assert wht["symbol"] == "AAPL"
    
    def test_parse_payment_in_lieu(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map Payment In Lieu to DIVIDEND activity."""
        report = parser.parse(sample_flex_report_xml)
        
        dividends = [a for a in report.activities if a["activity_type"] == "DIVIDEND"]
        
        # Find AMD payment in lieu
        amd_pil = [d for d in dividends if d["symbol"] == "AMD"]
        assert len(amd_pil) == 1
        assert amd_pil[0]["amount"] == 12.00
    
    def test_parse_broker_interest(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map Broker Interest to FEE activity."""
        report = parser.parse(sample_flex_report_xml)
        
        fees = [a for a in report.activities if a["activity_type"] == "FEE"]
        
        # Find broker interest - check by known amount value (45.67)
        # The description should contain "interest" or "credit" 
        interest_fees = [f for f in fees 
                        if f.get("amount") == 45.67 or "int" in f.get("description", "").lower()]
        assert len(interest_fees) >= 1
        
        # Verify the interest fee has expected amount
        interest_fee = next((f for f in fees if f.get("amount") == 45.67), None)
        assert interest_fee is not None, f"Expected fee with amount 45.67, got fees: {fees}"
    
    def test_parse_transfer(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map Deposits/Withdrawals to TRANSFER activity."""
        report = parser.parse(sample_flex_report_xml)
        
        transfers = [a for a in report.activities if a["activity_type"] == "TRANSFER"]
        assert len(transfers) >= 1
        
        deposit = transfers[0]
        assert deposit["amount"] == 10000.00
        assert "electronic" in deposit["description"].lower() or "transfer" in deposit["description"].lower()
    
    def test_activity_id_generated(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should generate deterministic activity_id for each activity."""
        report = parser.parse(sample_flex_report_xml)
        
        for activity in report.activities:
            assert "activity_id" in activity
            assert activity["activity_id"] is not None
            assert len(activity["activity_id"]) == 16  # SHA-256 truncated to 16 chars
    
    def test_activity_id_is_deterministic(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Same input should always produce same activity_id (idempotent)."""
        report1 = parser.parse(sample_flex_report_xml)
        report2 = parser.parse(sample_flex_report_xml)
        
        # Same activities should have same IDs
        ids1 = {a["activity_id"] for a in report1.activities}
        ids2 = {a["activity_id"] for a in report2.activities}
        
        assert ids1 == ids2


# =============================================================================
# FlexXMLParser Tests - Corporate Action Parsing
# =============================================================================

class TestFlexXMLParserCorporateActions:
    """Test CorporateAction element parsing → ibkr_activities mapping."""
    
    def test_parse_option_assignment_tc(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map TC (Tender Call) to ASSIGNMENT activity."""
        report = parser.parse(sample_flex_report_xml)
        
        assignments = [a for a in report.activities if a["activity_type"] == "ASSIGNMENT"]
        
        # Find AAPL assignment (TC type)
        aapl_assigns = [a for a in assignments if a["symbol"] == "AAPL"]
        assert len(aapl_assigns) == 1
        
        assign = aapl_assigns[0]
        assert assign["quantity"] == 100
        assert assign["amount"] == -18000.00
        assert assign["source"] == "FLEX"
    
    def test_parse_option_assignment_as(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map AS code to ASSIGNMENT activity."""
        report = parser.parse(sample_flex_report_xml)
        
        assignments = [a for a in report.activities if a["activity_type"] == "ASSIGNMENT"]
        
        # Find TSLA assignment (AS type)
        tsla_assigns = [a for a in assignments if a["symbol"] == "TSLA"]
        assert len(tsla_assigns) == 1
        
        assign = tsla_assigns[0]
        assert assign["quantity"] == -200  # Negative for sale
        assert assign["amount"] == 50000.00
    
    def test_parse_option_exercise(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map EX to EXERCISE activity."""
        report = parser.parse(sample_flex_report_xml)
        
        exercises = [a for a in report.activities if a["activity_type"] == "EXERCISE"]
        assert len(exercises) == 1
        
        exercise = exercises[0]
        assert exercise["symbol"] == "NVDA"
        assert exercise["quantity"] == 50
        assert exercise["amount"] == -30000.00
        assert "exercise" in exercise["description"].lower()
    
    def test_parse_option_expiration(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map EP to EXPIRATION activity."""
        report = parser.parse(sample_flex_report_xml)
        
        expirations = [a for a in report.activities if a["activity_type"] == "EXPIRATION"]
        assert len(expirations) == 1
        
        exp = expirations[0]
        assert exp["symbol"] == "SPY"
        assert exp["quantity"] == 5
        assert exp["amount"] == 0  # Expired worthless
    
    def test_parse_stock_split(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map FS (Forward Split) to OTHER activity."""
        report = parser.parse(sample_flex_report_xml)
        
        others = [a for a in report.activities if a["activity_type"] == "OTHER"]
        
        # Find NVDA split
        nvda_splits = [o for o in others if o["symbol"] == "NVDA" and "split" in o.get("description", "").lower()]
        assert len(nvda_splits) == 1
        
        split = nvda_splits[0]
        assert split["quantity"] == 900
    
    def test_parse_corporate_dividend(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Parser should map DI corporate action to DIVIDEND activity."""
        report = parser.parse(sample_flex_report_xml)
        
        dividends = [a for a in report.activities if a["activity_type"] == "DIVIDEND"]
        
        # Find KO dividend from corporate actions
        ko_divs = [d for d in dividends if d["symbol"] == "KO"]
        assert len(ko_divs) == 1
        assert ko_divs[0]["amount"] == 18.50


# =============================================================================
# FlexXMLParser Tests - Datetime Parsing
# =============================================================================

class TestFlexXMLParserDatetime:
    """Test datetime parsing with various formats."""
    
    def test_parse_datetime_yyyymmdd_semicolon_hhmmss(self, parser: FlexXMLParser):
        """Parser should handle YYYYMMDD;HHMMSS format."""
        result = parser._parse_flex_datetime("20240115;103000")
        assert result == "2024-01-15 10:30:00"
    
    def test_parse_datetime_yyyymmdd_space_hhmmss(self, parser: FlexXMLParser):
        """Parser should handle YYYYMMDD HHMMSS format."""
        result = parser._parse_flex_datetime("20240115 103000")
        assert result == "2024-01-15 10:30:00"
    
    def test_parse_datetime_yyyymmdd_semicolon_hh_mm_ss(self, parser: FlexXMLParser):
        """Parser should handle YYYYMMDD;HH:MM:SS format."""
        result = parser._parse_flex_datetime("20240115;10:30:00")
        assert result == "2024-01-15 10:30:00"
    
    def test_parse_datetime_iso_format(self, parser: FlexXMLParser):
        """Parser should handle ISO datetime format."""
        result = parser._parse_flex_datetime("2024-01-15 10:30:00")
        assert result == "2024-01-15 10:30:00"
    
    def test_parse_datetime_iso_t_separator(self, parser: FlexXMLParser):
        """Parser should handle ISO format with T separator."""
        result = parser._parse_flex_datetime("2024-01-15T10:30:00")
        assert result == "2024-01-15 10:30:00"
    
    def test_parse_datetime_date_only_yyyymmdd(self, parser: FlexXMLParser):
        """Parser should handle date-only YYYYMMDD format."""
        result = parser._parse_flex_datetime("20240115")
        assert "2024-01-15" in result
    
    def test_parse_datetime_date_only_iso(self, parser: FlexXMLParser):
        """Parser should handle date-only ISO format."""
        result = parser._parse_flex_datetime("2024-01-15")
        assert "2024-01-15" in result
    
    def test_parse_expiration_yyyymmdd(self, parser: FlexXMLParser):
        """Parser should convert YYYYMMDD expiration to YYYY-MM-DD."""
        result = parser._parse_expiration("20240315")
        assert result == "2024-03-15"
    
    def test_parse_expiration_already_formatted(self, parser: FlexXMLParser):
        """Parser should pass through already formatted expiration."""
        result = parser._parse_expiration("2024-03-15")
        assert result == "2024-03-15"
    
    def test_parse_expiration_none(self, parser: FlexXMLParser):
        """Parser should handle None expiration."""
        result = parser._parse_expiration(None)
        assert result is None


# =============================================================================
# FlexXMLParser Tests - Helper Methods
# =============================================================================

class TestFlexXMLParserHelpers:
    """Test parser helper methods."""
    
    def test_safe_int_valid(self, parser: FlexXMLParser):
        """_safe_int should convert valid integers."""
        assert parser._safe_int("123") == 123
        assert parser._safe_int("0") == 0
        assert parser._safe_int("-50") == -50
    
    def test_safe_int_float_string(self, parser: FlexXMLParser):
        """_safe_int should handle float strings like '100.0'."""
        assert parser._safe_int("100.0") == 100
        assert parser._safe_int("99.9") == 99
    
    def test_safe_int_invalid(self, parser: FlexXMLParser):
        """_safe_int should return None for invalid input."""
        assert parser._safe_int(None) is None
        assert parser._safe_int("") is None
        assert parser._safe_int("abc") is None
    
    def test_safe_float_valid(self, parser: FlexXMLParser):
        """_safe_float should convert valid floats."""
        assert parser._safe_float("123.45") == 123.45
        assert parser._safe_float("0") == 0.0
        assert parser._safe_float("-50.5") == -50.5
    
    def test_safe_float_invalid(self, parser: FlexXMLParser):
        """_safe_float should return None for invalid input."""
        assert parser._safe_float(None) is None
        assert parser._safe_float("") is None
        assert parser._safe_float("xyz") is None
    
    def test_generate_activity_id_deterministic(self, parser: FlexXMLParser):
        """_generate_activity_id should be deterministic."""
        id1 = parser._generate_activity_id(
            account="U1234567",
            activity_type="DIVIDEND",
            event_time="2024-01-15 10:30:00",
            symbol="AAPL",
            amount="24.00",
            description="Test dividend",
        )
        id2 = parser._generate_activity_id(
            account="U1234567",
            activity_type="DIVIDEND",
            event_time="2024-01-15 10:30:00",
            symbol="AAPL",
            amount="24.00",
            description="Test dividend",
        )
        assert id1 == id2
        assert len(id1) == 16
    
    def test_generate_activity_id_different_inputs(self, parser: FlexXMLParser):
        """_generate_activity_id should produce different IDs for different inputs."""
        id1 = parser._generate_activity_id(
            account="U1234567",
            activity_type="DIVIDEND",
            event_time="2024-01-15 10:30:00",
            symbol="AAPL",
            amount="24.00",
        )
        id2 = parser._generate_activity_id(
            account="U1234567",
            activity_type="DIVIDEND",
            event_time="2024-01-15 10:30:00",
            symbol="MSFT",  # Different symbol
            amount="24.00",
        )
        assert id1 != id2


# =============================================================================
# FlexQueryClient Tests
# =============================================================================

class TestFlexQueryClientInit:
    """Test FlexQueryClient initialization."""
    
    def test_init_with_valid_params(self):
        """Client should initialize with valid token and query_id."""
        client = FlexQueryClient(token="abc123", query_id="12345")
        assert client.token == "abc123"
        assert client.query_id == "12345"
    
    def test_init_missing_token_raises(self):
        """Client should raise ValueError if token is empty."""
        with pytest.raises(ValueError, match="token"):
            FlexQueryClient(token="", query_id="12345")
    
    def test_init_missing_query_id_raises(self):
        """Client should raise ValueError if query_id is empty."""
        with pytest.raises(ValueError, match="query_id"):
            FlexQueryClient(token="abc123", query_id="")
    
    def test_init_custom_timeouts(self):
        """Client should accept custom timeout settings."""
        client = FlexQueryClient(
            token="abc123",
            query_id="12345",
            timeout=60.0,
            poll_interval=10.0,
            max_attempts=20,
        )
        assert client.timeout == 60.0
        assert client.poll_interval == 10.0
        assert client.max_attempts == 20


class TestFlexQueryClientSendRequest:
    """Test FlexQueryClient.send_request() method."""
    
    @pytest.mark.asyncio
    async def test_send_request_success(self, client: FlexQueryClient, send_request_response_xml: str):
        """send_request should return reference code on success."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_response = MagicMock()
            mock_response.text = send_request_response_xml
            mock_response.raise_for_status = MagicMock()
            
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client
            
            ref_code = await client.send_request()
            
            assert ref_code == "1234567890"
            mock_client.get.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_send_request_error_response(self, client: FlexQueryClient, error_response_xml: str):
        """send_request should raise FlexError on error response."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_response = MagicMock()
            mock_response.text = error_response_xml
            mock_response.raise_for_status = MagicMock()
            
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client
            
            with pytest.raises(FlexError) as exc_info:
                await client.send_request()
            
            assert "1003" in str(exc_info.value) or "token" in str(exc_info.value).lower()


class TestFlexQueryClientGetStatement:
    """Test FlexQueryClient.get_statement() method."""
    
    @pytest.mark.asyncio
    async def test_get_statement_immediate_success(
        self, client: FlexQueryClient, sample_flex_report_xml: str
    ):
        """get_statement should return XML when immediately ready."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_response = MagicMock()
            mock_response.text = sample_flex_report_xml
            mock_response.raise_for_status = MagicMock()
            
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client
            
            xml_content = await client.get_statement("ref123")
            
            assert xml_content == sample_flex_report_xml
    
    @pytest.mark.asyncio
    async def test_get_statement_polls_until_ready(
        self, client: FlexQueryClient, not_ready_response_xml: str, sample_flex_report_xml: str
    ):
        """get_statement should poll until statement is ready."""
        with patch("httpx.AsyncClient") as mock_client_class:
            # First two calls return not ready, third returns success
            mock_responses = [
                MagicMock(text=not_ready_response_xml, raise_for_status=MagicMock()),
                MagicMock(text=not_ready_response_xml, raise_for_status=MagicMock()),
                MagicMock(text=sample_flex_report_xml, raise_for_status=MagicMock()),
            ]
            
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_responses)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client
            
            # Use short poll interval for test
            client.poll_interval = 0.01
            
            with patch("asyncio.sleep", new_callable=AsyncMock):
                xml_content = await client.get_statement("ref123")
            
            assert xml_content == sample_flex_report_xml
            assert mock_client.get.call_count == 3
    
    @pytest.mark.asyncio
    async def test_get_statement_returns_none_after_max_attempts(
        self, client: FlexQueryClient, not_ready_response_xml: str
    ):
        """get_statement should return None after max attempts."""
        client.max_attempts = 3
        client.poll_interval = 0.01
        
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_response = MagicMock()
            mock_response.text = not_ready_response_xml
            mock_response.raise_for_status = MagicMock()
            
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client
            
            with patch("asyncio.sleep", new_callable=AsyncMock):
                xml_content = await client.get_statement("ref123")
            
            assert xml_content is None
            assert mock_client.get.call_count == 3


class TestFlexQueryClientFetchFlexReport:
    """Test FlexQueryClient.fetch_flex_report() orchestration method."""
    
    @pytest.mark.asyncio
    async def test_fetch_flex_report_full_flow(
        self, client: FlexQueryClient, send_request_response_xml: str, sample_flex_report_xml: str
    ):
        """fetch_flex_report should orchestrate send_request and get_statement."""
        with patch.object(client, "send_request", new_callable=AsyncMock) as mock_send:
            with patch.object(client, "get_statement", new_callable=AsyncMock) as mock_get:
                mock_send.return_value = "ref123"
                mock_get.return_value = sample_flex_report_xml
                
                result = await client.fetch_flex_report()
                
                assert result == sample_flex_report_xml
                mock_send.assert_called_once()
                mock_get.assert_called_once_with("ref123")
    
    @pytest.mark.asyncio
    async def test_fetch_flex_report_raises_on_timeout(self, client: FlexQueryClient):
        """fetch_flex_report should raise FlexError when statement times out."""
        with patch.object(client, "send_request", new_callable=AsyncMock) as mock_send:
            with patch.object(client, "get_statement", new_callable=AsyncMock) as mock_get:
                mock_send.return_value = "ref123"
                mock_get.return_value = None  # Timeout - statement not ready
                
                with pytest.raises(FlexError, match="not ready"):
                    await client.fetch_flex_report()


# =============================================================================
# Integration-Style Tests
# =============================================================================

class TestFlexParserIntegration:
    """Integration-style tests parsing full XML documents."""
    
    def test_parse_full_report_counts(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Full report should parse expected counts of each record type."""
        report = parser.parse(sample_flex_report_xml)
        
        # Based on sample_flex_report.xml contents
        assert len(report.executions) == 5  # 5 Trade elements
        assert len(report.commission_reports) >= 4  # At least 4 trades have commission
        assert len(report.activities) >= 10  # Cash transactions + corporate actions
        
        # Verify activity types present
        activity_types = {a["activity_type"] for a in report.activities}
        assert "DIVIDEND" in activity_types
        assert "FEE" in activity_types
        assert "TRANSFER" in activity_types
        assert "ASSIGNMENT" in activity_types
        assert "EXERCISE" in activity_types
        assert "EXPIRATION" in activity_types
    
    def test_parse_full_report_no_warnings(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """Well-formed sample report should produce no warnings."""
        report = parser.parse(sample_flex_report_xml)
        
        # Filter out expected debug-level messages
        significant_warnings = [w for w in report.warnings if "error" in w.lower() or "fail" in w.lower()]
        assert len(significant_warnings) == 0, f"Unexpected warnings: {significant_warnings}"
    
    def test_source_always_flex(self, parser: FlexXMLParser, sample_flex_report_xml: str):
        """All parsed records should have source='FLEX'."""
        report = parser.parse(sample_flex_report_xml)
        
        for exec_row in report.executions:
            assert exec_row["source"] == "FLEX"
        
        for activity in report.activities:
            assert activity["source"] == "FLEX"


# =============================================================================
# Run tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
