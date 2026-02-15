"""
IBKR Flex Web Service client and XML parser.

This module provides:
- FlexQueryClient: HTTP client for IBKR Flex Web Service API
- FlexXMLParser: XML parser for Flex report data
- FlexReport: Dataclass container for parsed data

Usage:
    client = FlexQueryClient(token="your_token", query_id="your_query_id")
    xml_content = await client.fetch_flex_report()
    
    parser = FlexXMLParser()
    report = parser.parse(xml_content)
    
    print(f"Executions: {len(report.executions)}")
    print(f"Activities: {len(report.activities)}")
"""

import asyncio
import hashlib
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any

import httpx

logger = logging.getLogger(__name__)

# IBKR Flex Web Service endpoints
FLEX_BASE_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
SEND_REQUEST_URL = f"{FLEX_BASE_URL}.SendRequest"
GET_STATEMENT_URL = f"{FLEX_BASE_URL}.GetStatement"

# Response status codes
STATUS_SUCCESS = "0"
STATUS_NOT_YET_READY = "1019"


class FlexError(Exception):
    """Exception raised for Flex Web Service errors."""
    
    def __init__(self, message: str, error_code: Optional[str] = None):
        super().__init__(message)
        self.error_code = error_code


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class FlexReport:
    """Container for parsed Flex report data.
    
    Attributes:
        executions: List of execution records (maps to ibkr_executions table)
        commission_reports: List of commission records (maps to ibkr_commission_reports table)
        activities: List of activity records (maps to ibkr_activities table)
        warnings: List of non-fatal parsing warnings
        account_id: Primary account ID from the report
        from_date: Report start date
        to_date: Report end date
    """
    executions: list[dict[str, Any]] = field(default_factory=list)
    commission_reports: list[dict[str, Any]] = field(default_factory=list)
    activities: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    account_id: Optional[str] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None


# =============================================================================
# FlexQueryClient - HTTP Client for Flex Web Service
# =============================================================================

class FlexQueryClient:
    """Client for IBKR Flex Web Service API.
    
    Implements the two-step polling flow:
    1. SendRequest - Submit query and get reference code
    2. GetStatement - Poll until statement is ready
    
    Args:
        token: Flex Web Service token from IBKR Account Management
        query_id: Flex Query ID from IBKR Account Management
        timeout: HTTP request timeout in seconds (default: 30.0)
        poll_interval: Seconds between GetStatement polls (default: 5.0)
        max_attempts: Maximum GetStatement poll attempts (default: 12)
    """
    
    def __init__(
        self,
        token: str,
        query_id: str,
        timeout: float = 30.0,
        poll_interval: float = 5.0,
        max_attempts: int = 12,
    ):
        self.token = token
        self.query_id = query_id
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_attempts = max_attempts
        
        if not token:
            raise ValueError("Flex token is required")
        if not query_id:
            raise ValueError("Flex query_id is required")
    
    async def send_request(self) -> str:
        """Call IBKR Flex SendRequest API to initiate report generation.
        
        Returns:
            Reference code to use with GetStatement API
            
        Raises:
            FlexError: If the request fails or returns an error
            httpx.HTTPError: For network/HTTP errors
        """
        logger.info("[FLEX_CLIENT] Sending Flex query request (query_id=%s)", self.query_id)
        
        params = {
            "t": self.token,
            "q": self.query_id,
            "v": "3",
        }
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(SEND_REQUEST_URL, params=params)
            response.raise_for_status()
        
        xml_text = response.text
        logger.debug("[FLEX_CLIENT] SendRequest response: %s", xml_text[:500])
        
        # Parse response XML
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error("[FLEX_CLIENT] Failed to parse SendRequest response: %s", e)
            raise FlexError(f"Invalid XML in SendRequest response: {e}")
        
        # Check response status
        status = root.find(".//Status")
        if status is not None and status.text not in (STATUS_SUCCESS, None, ""):
            # Check for specific error
            error_code_elem = root.find(".//ErrorCode")
            error_msg_elem = root.find(".//ErrorMessage")
            error_code = error_code_elem.text if error_code_elem is not None else status.text
            error_msg = error_msg_elem.text if error_msg_elem is not None else "Unknown error"
            logger.error("[FLEX_CLIENT] SendRequest failed: %s - %s", error_code, error_msg)
            raise FlexError(f"SendRequest failed: {error_code} - {error_msg}", error_code)
        
        # Check for ErrorCode element (can appear without Status)
        error_code_elem = root.find(".//ErrorCode")
        if error_code_elem is not None and error_code_elem.text:
            error_msg_elem = root.find(".//ErrorMessage")
            error_msg = error_msg_elem.text if error_msg_elem is not None else "Unknown error"
            logger.error("[FLEX_CLIENT] SendRequest error: %s - %s", error_code_elem.text, error_msg)
            raise FlexError(f"SendRequest error: {error_code_elem.text} - {error_msg}", error_code_elem.text)
        
        # Extract reference code
        ref_code_elem = root.find(".//ReferenceCode")
        if ref_code_elem is None or not ref_code_elem.text:
            logger.error("[FLEX_CLIENT] No ReferenceCode in SendRequest response")
            raise FlexError("No ReferenceCode in SendRequest response")
        
        reference_code = ref_code_elem.text.strip()
        logger.info("[FLEX_CLIENT] Got reference code: %s", reference_code)
        return reference_code
    
    async def get_statement(self, reference_code: str) -> Optional[str]:
        """Poll IBKR Flex GetStatement API until report is ready or timeout.
        
        Args:
            reference_code: Reference code from send_request()
            
        Returns:
            XML content of the Flex statement, or None if not ready after max attempts
            
        Raises:
            FlexError: If the request fails or returns an error
            httpx.HTTPError: For network/HTTP errors
        """
        logger.info(
            "[FLEX_CLIENT] Polling for statement (ref=%s, max_attempts=%d, interval=%.1fs)",
            reference_code, self.max_attempts, self.poll_interval
        )
        
        params = {
            "t": self.token,
            "q": reference_code,
            "v": "3",
        }
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, self.max_attempts + 1):
                logger.debug("[FLEX_CLIENT] GetStatement attempt %d/%d", attempt, self.max_attempts)
                
                response = await client.get(GET_STATEMENT_URL, params=params)
                response.raise_for_status()
                
                xml_text = response.text
                
                # Check for "not yet ready" status (1019)
                if f"<Status>{STATUS_NOT_YET_READY}</Status>" in xml_text:
                    logger.info(
                        "[FLEX_CLIENT] Statement not ready (attempt %d/%d), waiting %.1fs...",
                        attempt, self.max_attempts, self.poll_interval
                    )
                    if attempt < self.max_attempts:
                        await asyncio.sleep(self.poll_interval)
                    continue
                
                # Check for error response
                if "<ErrorCode>" in xml_text:
                    try:
                        root = ET.fromstring(xml_text)
                        error_code_elem = root.find(".//ErrorCode")
                        error_msg_elem = root.find(".//ErrorMessage")
                        error_code = error_code_elem.text if error_code_elem is not None else "Unknown"
                        error_msg = error_msg_elem.text if error_msg_elem is not None else "Unknown error"
                        
                        # Status 1019 can also appear in ErrorCode
                        if error_code == STATUS_NOT_YET_READY:
                            logger.info(
                                "[FLEX_CLIENT] Statement not ready via ErrorCode (attempt %d/%d)",
                                attempt, self.max_attempts
                            )
                            if attempt < self.max_attempts:
                                await asyncio.sleep(self.poll_interval)
                            continue
                        
                        logger.error("[FLEX_CLIENT] GetStatement error: %s - %s", error_code, error_msg)
                        raise FlexError(f"GetStatement error: {error_code} - {error_msg}", error_code)
                    except ET.ParseError:
                        pass  # Will treat as success and return the content
                
                # Check for valid FlexQueryResponse or FlexStatements
                if "<FlexQueryResponse" in xml_text or "<FlexStatements" in xml_text:
                    logger.info("[FLEX_CLIENT] Statement retrieved successfully (attempt %d)", attempt)
                    return xml_text
                
                # Unknown response - log warning but return it
                logger.warning("[FLEX_CLIENT] Unexpected response format, returning as-is")
                return xml_text
        
        logger.error(
            "[FLEX_CLIENT] Statement not ready after %d attempts (%.1fs total)",
            self.max_attempts, self.max_attempts * self.poll_interval
        )
        return None
    
    async def fetch_flex_report(self) -> str:
        """Orchestrate the full Flex report fetch flow.
        
        This is the main entry point that:
        1. Sends the query request
        2. Polls until the statement is ready
        3. Returns the XML content
        
        Returns:
            XML content of the Flex statement
            
        Raises:
            FlexError: If any step fails or times out
        """
        logger.info("[FLEX_CLIENT] Starting Flex report fetch")
        
        # Step 1: Send request to get reference code
        reference_code = await self.send_request()
        
        # Step 2: Poll for statement
        xml_content = await self.get_statement(reference_code)
        
        if xml_content is None:
            raise FlexError(
                f"Flex statement not ready after {self.max_attempts} attempts "
                f"(waited {self.max_attempts * self.poll_interval:.1f}s)"
            )
        
        logger.info("[FLEX_CLIENT] Flex report fetch complete (%d bytes)", len(xml_content))
        return xml_content


# =============================================================================
# FlexXMLParser - XML Parser for Flex Reports
# =============================================================================

class FlexXMLParser:
    """Parser for IBKR Flex XML reports.
    
    Parses Flex XML elements and maps them to Wheeler's ledger table structures:
    - <Trade> elements → ibkr_executions + ibkr_commission_reports
    - <CashTransaction> elements → ibkr_activities  
    - <CorporateAction> elements → ibkr_activities
    """
    
    # Cash transaction type mapping
    CASH_TX_TYPE_MAP = {
        "DIVIDENDS": "DIVIDEND",
        "DIVIDEND": "DIVIDEND",
        "PAYMENT IN LIEU OF DIVIDENDS": "DIVIDEND",
        "WITHHOLDING TAX": "FEE",
        "BROKER INTEREST PAID": "FEE",
        "BROKER INTEREST RECEIVED": "FEE",
        "COMMISSION ADJUSTMENTS": "FEE",
        "OTHER FEES": "FEE",
        "TRANSFERS": "TRANSFER",
        "DEPOSITS/WITHDRAWALS": "TRANSFER",
        "DEPOSITS": "TRANSFER",
        "WITHDRAWALS": "TRANSFER",
    }
    
    # Corporate action type mapping
    CORP_ACTION_TYPE_MAP = {
        "TC": "ASSIGNMENT",   # Option assignment (Tender Call)
        "AS": "ASSIGNMENT",   # Assignment
        "EX": "EXERCISE",     # Option exercise
        "EP": "EXPIRATION",   # Option expiration
        "CA": "OTHER",        # Generic corporate action
        "DI": "DIVIDEND",     # Dividend
        "FS": "OTHER",        # Forward split
        "RS": "OTHER",        # Reverse split
        "SO": "OTHER",        # Spin-off
        "TO": "OTHER",        # Tender offer
    }
    
    def parse(self, xml_content: str) -> FlexReport:
        """Parse Flex XML content into a structured FlexReport.
        
        Args:
            xml_content: Raw XML string from Flex Web Service
            
        Returns:
            FlexReport containing parsed executions, activities, and warnings
        """
        logger.info("[FLEX_CLIENT] Parsing Flex XML (%d bytes)", len(xml_content))
        
        report = FlexReport()
        
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            logger.error("[FLEX_CLIENT] XML parse error: %s", e)
            report.warnings.append(f"XML parse error: {e}")
            return report
        
        # Find FlexStatements container
        flex_statements = root.find(".//FlexStatements")
        if flex_statements is not None:
            # Extract report metadata
            report.account_id = flex_statements.get("accountId")
            report.from_date = flex_statements.get("fromDate")
            report.to_date = flex_statements.get("toDate")
        
        # Process each FlexStatement (could be multiple accounts)
        statements = root.findall(".//FlexStatement")
        if not statements:
            # Try alternative structure
            statements = root.findall(".//FlexStatements/FlexStatement")
        
        if not statements:
            logger.warning("[FLEX_CLIENT] No FlexStatement elements found in XML")
            report.warnings.append("No FlexStatement elements found")
            return report
        
        for statement in statements:
            account = statement.get("accountId", report.account_id or "")
            logger.debug("[FLEX_CLIENT] Processing statement for account: %s", account)
            
            # Parse Trades → executions + commission_reports
            trades_section = statement.find(".//Trades")
            if trades_section is not None:
                for trade in trades_section.findall("Trade"):
                    self._parse_trade(trade, account, report)
            else:
                # Try finding Trade elements directly
                for trade in statement.findall(".//Trade"):
                    self._parse_trade(trade, account, report)
            
            # Parse CashTransactions → activities
            cash_section = statement.find(".//CashTransactions")
            if cash_section is not None:
                for cash_tx in cash_section.findall("CashTransaction"):
                    self._parse_cash_transaction(cash_tx, account, report)
            else:
                for cash_tx in statement.findall(".//CashTransaction"):
                    self._parse_cash_transaction(cash_tx, account, report)
            
            # Parse CorporateActions → activities
            corp_section = statement.find(".//CorporateActions")
            if corp_section is not None:
                for corp_action in corp_section.findall("CorporateAction"):
                    self._parse_corporate_action(corp_action, account, report)
            else:
                for corp_action in statement.findall(".//CorporateAction"):
                    self._parse_corporate_action(corp_action, account, report)
        
        logger.info(
            "[FLEX_CLIENT] Parsed %d executions, %d commission reports, %d activities, %d warnings",
            len(report.executions), len(report.commission_reports), 
            len(report.activities), len(report.warnings)
        )
        
        return report
    
    def _parse_trade(self, trade: ET.Element, account: str, report: FlexReport) -> None:
        """Parse a <Trade> element into execution and commission records."""
        try:
            exec_id = trade.get("ibExecID") or trade.get("tradeID")
            if not exec_id:
                report.warnings.append(f"Trade missing exec_id: {trade.attrib}")
                return
            
            # Parse trade datetime
            trade_datetime = self._parse_trade_datetime(trade)
            
            # Normalize side (BUY/SELL)
            buy_sell = (trade.get("buySell") or trade.get("side") or "").upper()
            if buy_sell in ("BUY", "B", "BOT"):
                side = "BUY"
            elif buy_sell in ("SELL", "S", "SLD"):
                side = "SELL"
            else:
                side = buy_sell
            
            # Normalize sec_type
            sec_type = (trade.get("assetCategory") or trade.get("secType") or "").upper()
            if sec_type in ("STOCK", "STK"):
                sec_type = "STK"
            elif sec_type in ("OPTION", "OPT"):
                sec_type = "OPT"
            
            # Normalize right (put/call) for options
            put_call = (trade.get("putCall") or "").upper()
            right = None
            if put_call in ("P", "PUT"):
                right = "P"
            elif put_call in ("C", "CALL"):
                right = "C"
            
            # Parse expiration date
            expiration = self._parse_expiration(trade.get("expiry"))
            
            # Build execution record
            exec_row = {
                "account": account,
                "exec_id": exec_id,
                "order_id": self._safe_int(trade.get("ibOrderID") or trade.get("orderID")),
                "perm_id": self._safe_int(trade.get("permID")),
                "conid": self._safe_int(trade.get("conid")),
                "symbol": trade.get("symbol") or trade.get("underlyingSymbol") or "",
                "sec_type": sec_type if sec_type in ("STK", "OPT") else "STK",
                "currency": trade.get("currency") or "USD",
                "trade_time": trade_datetime,
                "side": side if side in ("BUY", "SELL") else "BUY",
                "quantity": abs(self._safe_float(trade.get("quantity")) or 0),
                "price": self._safe_float(trade.get("tradePrice") or trade.get("price")) or 0,
                "multiplier": self._safe_int(trade.get("multiplier")),
                "right": right,
                "strike": self._safe_float(trade.get("strike")),
                "expiration": expiration,
                "exchange": trade.get("exchange") or "",
                "liquidation": 1 if trade.get("isAPIOrder") == "N" else 0,
                "raw_json": json.dumps(dict(trade.attrib)),
                "source": "FLEX",
            }
            report.executions.append(exec_row)
            
            # Build commission report if commission data present
            commission = self._safe_float(trade.get("ibCommission") or trade.get("commission"))
            if commission is not None:
                comm_row = {
                    "account": account,
                    "exec_id": exec_id,
                    "commission": abs(commission),  # Commission is usually negative in Flex
                    "realized_pnl": self._safe_float(
                        trade.get("realizedPnL") or 
                        trade.get("fifoPnlRealized") or 
                        trade.get("mtmPnl")
                    ),
                    "currency": trade.get("ibCommissionCurrency") or trade.get("currency") or "USD",
                }
                report.commission_reports.append(comm_row)
                
        except Exception as e:
            logger.warning("[FLEX_CLIENT] Failed to parse trade: %s", e)
            report.warnings.append(f"Failed to parse trade: {e}")
    
    def _parse_cash_transaction(self, cash_tx: ET.Element, account: str, report: FlexReport) -> None:
        """Parse a <CashTransaction> element into an activity record."""
        try:
            tx_type = (cash_tx.get("type") or "").upper()
            
            # Map to Wheeler activity type
            activity_type = self.CASH_TX_TYPE_MAP.get(tx_type)
            if not activity_type:
                # Log but skip unmapped types
                logger.debug("[FLEX_CLIENT] Skipping unmapped cash transaction type: %s", tx_type)
                return
            
            # Parse datetime
            event_time = self._parse_flex_datetime(
                cash_tx.get("dateTime") or cash_tx.get("reportDate") or ""
            )
            
            # Generate deterministic activity_id for deduplication
            activity_id = self._generate_activity_id(
                account=account,
                activity_type=activity_type,
                event_time=event_time,
                symbol=cash_tx.get("symbol") or "",
                amount=cash_tx.get("amount") or "",
                description=cash_tx.get("description") or "",
            )
            
            activity_row = {
                "account": account,
                "activity_id": activity_id,
                "activity_type": activity_type,
                "conid": self._safe_int(cash_tx.get("conid")),
                "symbol": cash_tx.get("symbol") or "",
                "event_time": event_time,
                "quantity": self._safe_float(cash_tx.get("quantity")),
                "amount": self._safe_float(cash_tx.get("amount")),
                "currency": cash_tx.get("currency") or "USD",
                "description": cash_tx.get("description") or "",
                "raw_json": json.dumps(dict(cash_tx.attrib)),
                "source": "FLEX",
            }
            report.activities.append(activity_row)
            
        except Exception as e:
            logger.warning("[FLEX_CLIENT] Failed to parse cash transaction: %s", e)
            report.warnings.append(f"Failed to parse cash transaction: {e}")
    
    def _parse_corporate_action(self, corp_action: ET.Element, account: str, report: FlexReport) -> None:
        """Parse a <CorporateAction> element into an activity record."""
        try:
            action_type = (corp_action.get("type") or "").upper()
            
            # Map to Wheeler activity type
            activity_type = self.CORP_ACTION_TYPE_MAP.get(action_type, "OTHER")
            
            # Parse datetime
            event_time = self._parse_flex_datetime(
                corp_action.get("dateTime") or corp_action.get("reportDate") or ""
            )
            
            # Get amount from proceeds or value
            amount = self._safe_float(
                corp_action.get("proceeds") or 
                corp_action.get("value") or 
                corp_action.get("amount")
            )
            
            # Generate deterministic activity_id
            activity_id = self._generate_activity_id(
                account=account,
                activity_type=activity_type,
                event_time=event_time,
                symbol=corp_action.get("symbol") or "",
                amount=str(amount) if amount else "",
                description=corp_action.get("actionDescription") or corp_action.get("description") or "",
            )
            
            activity_row = {
                "account": account,
                "activity_id": activity_id,
                "activity_type": activity_type,
                "conid": self._safe_int(corp_action.get("conid")),
                "symbol": corp_action.get("symbol") or "",
                "event_time": event_time,
                "quantity": self._safe_float(corp_action.get("quantity")),
                "amount": amount,
                "currency": corp_action.get("currency") or "USD",
                "description": corp_action.get("actionDescription") or corp_action.get("description") or "",
                "raw_json": json.dumps(dict(corp_action.attrib)),
                "source": "FLEX",
            }
            report.activities.append(activity_row)
            
        except Exception as e:
            logger.warning("[FLEX_CLIENT] Failed to parse corporate action: %s", e)
            report.warnings.append(f"Failed to parse corporate action: {e}")
    
    # =========================================================================
    # Helper Methods
    # =========================================================================
    
    def _parse_trade_datetime(self, trade: ET.Element) -> str:
        """Parse trade date and time into ISO format string."""
        trade_date = trade.get("tradeDate") or trade.get("dateTime") or ""
        trade_time = trade.get("tradeTime") or ""
        
        if trade_date and trade_time:
            # Combine date and time
            dt_str = f"{trade_date} {trade_time}"
        else:
            dt_str = trade_date
        
        return self._parse_flex_datetime(dt_str)
    
    def _parse_flex_datetime(self, dt_str: str) -> str:
        """Parse Flex datetime format to ISO string."""
        if not dt_str:
            return datetime.now().isoformat()
        
        # Clean up the string
        dt_str = dt_str.strip()
        
        # Try various Flex datetime formats
        formats = [
            "%Y%m%d;%H%M%S",       # 20240115;103000
            "%Y%m%d %H%M%S",       # 20240115 103000
            "%Y%m%d;%H:%M:%S",     # 20240115;10:30:00
            "%Y%m%d %H:%M:%S",     # 20240115 10:30:00
            "%Y-%m-%d;%H:%M:%S",   # 2024-01-15;10:30:00
            "%Y-%m-%d %H:%M:%S",   # 2024-01-15 10:30:00
            "%Y-%m-%dT%H:%M:%S",   # 2024-01-15T10:30:00
            "%Y%m%d",              # 20240115
            "%Y-%m-%d",            # 2024-01-15
        ]
        
        for fmt in formats:
            try:
                dt = datetime.strptime(dt_str, fmt)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        
        # If no format matched, return as-is
        logger.debug("[FLEX_CLIENT] Could not parse datetime: %s", dt_str)
        return dt_str
    
    def _parse_expiration(self, expiry: Optional[str]) -> Optional[str]:
        """Parse expiration date to YYYY-MM-DD format."""
        if not expiry:
            return None
        
        expiry = expiry.strip()
        
        # Handle YYYYMMDD format
        if len(expiry) == 8 and expiry.isdigit():
            return f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:8]}"
        
        # Already in YYYY-MM-DD format
        if len(expiry) == 10 and expiry[4] == "-" and expiry[7] == "-":
            return expiry
        
        return expiry
    
    def _generate_activity_id(
        self, 
        account: str, 
        activity_type: str, 
        event_time: str, 
        symbol: str, 
        amount: str,
        description: str = "",
    ) -> str:
        """Generate deterministic activity_id for deduplication.
        
        Uses SHA-256 hash of key fields to ensure idempotent upserts.
        """
        components = [
            account,
            activity_type,
            event_time,
            symbol,
            amount,
            description[:50],  # Truncate description to avoid hash variance from trailing spaces
        ]
        data = "|".join(str(c) for c in components)
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    
    @staticmethod
    def _safe_int(value: Optional[str]) -> Optional[int]:
        """Safely convert string to int, returning None on failure."""
        if value is None or value == "":
            return None
        try:
            # Handle floats like "100.0"
            return int(float(value))
        except (ValueError, TypeError):
            return None
    
    @staticmethod
    def _safe_float(value: Optional[str]) -> Optional[float]:
        """Safely convert string to float, returning None on failure."""
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
