"""IBKR client module using ib_async for Interactive Brokers connection."""
import asyncio
import json
from datetime import date, datetime, timezone
from typing import Optional, List, Dict, Any
import logging

from ib_async import IB, Contract, Option, Stock

try:
    from ib_async import ExecutionFilter  # type: ignore
except Exception:  # pragma: no cover
    ExecutionFilter = None

from config import settings

logger = logging.getLogger(__name__)


class IBKRClient:
    """Client for connecting to Interactive Brokers TWS/Gateway using ib_async."""
    
    def __init__(self):
        self.ib = IB()
        self._connected = False
    
    async def connect(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None
    ) -> bool:
        """
        Connect to TWS/Gateway.
        
        Args:
            host: TWS/Gateway hostname (default from settings)
            port: TWS/Gateway port (default from settings)
            client_id: Client ID for connection (default from settings)
            
        Returns:
            True if connection successful, False otherwise
        """
        host = host or settings.tws_host
        port = port or settings.tws_port
        client_id = client_id or settings.client_id
        
        try:
            await self.ib.connectAsync(host, port, clientId=client_id)
            self._connected = True
            logger.info(f"Connected to IBKR at {host}:{port} with client ID {client_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to IBKR: {e}")
            self._connected = False
            return False
    
    def disconnect(self) -> None:
        """Disconnect from TWS/Gateway."""
        if self.ib.isConnected():
            self.ib.disconnect()
            self._connected = False
            logger.info("Disconnected from IBKR")
    
    @property
    def is_connected(self) -> bool:
        """Check if connected to IBKR."""
        return self.ib.isConnected()

    async def set_market_data_type(self, market_data_type: int = 4) -> None:
        """
        Set the market data type for subsequent requests.
        
        Args:
            market_data_type: 1=Live, 2=Frozen, 3=Delayed, 4=Delayed-frozen
        """
        if self.ib and self.ib.isConnected():
            self.ib.reqMarketDataType(market_data_type)
            logger.info(f"Set market data type to {market_data_type}")
    
    async def test_connection(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None
    ) -> dict:
        """
        Test connection to TWS/Gateway.
        
        Returns connection status and account info if successful.
        """
        try:
            if not self.is_connected:
                success = await self.connect(host, port, client_id)
                if not success:
                    return {
                        "connected": False,
                        "error": "Failed to connect to TWS/Gateway"
                    }
            
            # Get managed accounts to verify connection
            accounts = self.ib.managedAccounts()
            
            return {
                "connected": True,
                "accounts": accounts,
                "server_version": self.ib.client.serverVersion() if self.ib.client else None
            }
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return {
                "connected": False,
                "error": str(e)
            }
    
    async def get_positions(self) -> list[dict]:
        """
        Get all positions from IBKR account.
        
        Returns list of position dictionaries with contract and position info.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")
        
        positions = self.ib.positions()
        result = []
        
        for pos in positions:
            contract = pos.contract
            
            position_data = {
                "account": pos.account,
                "conid": int(getattr(contract, "conId", 0) or 0),
                "symbol": contract.symbol,
                "sec_type": contract.secType,
                "position": pos.position,
                "avg_cost": pos.avgCost,
                "exchange": getattr(contract, "exchange", None),
                "currency": getattr(contract, "currency", None),
                "local_symbol": getattr(contract, "localSymbol", None),
                "trading_class": getattr(contract, "tradingClass", None),
            }
            
            # Add option-specific fields
            if contract.secType == "OPT":
                position_data.update({
                    "strike": contract.strike,
                    "expiration": contract.lastTradeDateOrContractMonth,
                    "right": contract.right,  # 'C' for Call, 'P' for Put
                    "multiplier": int(contract.multiplier) if contract.multiplier else 100,
                })
            
            result.append(position_data)
        
        logger.info(f"Retrieved {len(result)} positions from IBKR")
        return result

    async def get_option_greeks(self, options: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Get real-time option Greeks for the provided option positions.

        Returns:
            {"options": [...], "errors": [...]}

        Notes:
        - Uses conId when available to avoid ib_async hashing errors.
        - Qualifies contracts missing conId before requesting market data.
        - Skips any option that cannot be qualified and reports the error.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")

        if not options:
            return {"options": [], "errors": []}

        errors: List[str] = []

        contracts = []
        normalized = []

        for opt in options:
            try:
                expiration = normalize_expiration(opt.get("expiration"))
                if expiration is None:
                    logger.warning(f"Skipping option with invalid expiration: {opt.get('expiration')}")
                    continue

                right = normalize_option_right(opt.get("right") or opt.get("type"))

                conid_raw = opt.get("conid")
                conid: Optional[int] = None
                try:
                    if conid_raw is not None and str(conid_raw).strip() != "":
                        conid = int(conid_raw)
                        if conid <= 0:
                            conid = None
                except Exception:
                    conid = None

                exchange = opt.get("exchange") or "SMART"
                currency = opt.get("currency") or "USD"
                multiplier = opt.get("multiplier")
                multiplier_str = str(int(multiplier)) if multiplier else None

                contract = Option(
                    conId=conid or 0,
                    symbol=opt["symbol"],
                    lastTradeDateOrContractMonth=expiration,
                    strike=float(opt["strike"]),
                    right=right,
                    exchange=exchange,
                    currency=currency,
                    multiplier=multiplier_str,
                )
                contracts.append(contract)
                normalized.append(
                    {
                        "symbol": opt.get("symbol"),
                        "strike": float(opt.get("strike")),
                        "expiration": expiration,
                        "right": right,
                        "position": opt.get("position"),
                        "multiplier": opt.get("multiplier"),
                    }
                )
            except Exception as e:
                msg = f"Failed to prepare option contract for Greeks: {e}"
                logger.error(msg)
                errors.append(msg)
                continue

        if not contracts:
            return {"options": [], "errors": errors}

        # Qualify any contracts missing conId before requesting market data.
        to_qualify = [c for c in contracts if not int(getattr(c, "conId", 0) or 0)]
        if to_qualify:
            try:
                if hasattr(self.ib, "qualifyContractsAsync"):
                    await self.ib.qualifyContractsAsync(*to_qualify)
                else:
                    self.ib.qualifyContracts(*to_qualify)
            except Exception as e:
                msg = f"Failed to qualify {len(to_qualify)} option contracts: {e}"
                logger.error(msg)
                errors.append(msg)

        # Only request tickers for contracts that have a conId (ib_async may hash contracts internally).
        request_contracts: List[Contract] = []
        request_normalized: List[Dict[str, Any]] = []
        for idx, contract in enumerate(contracts):
            conid_val = int(getattr(contract, "conId", 0) or 0)
            if conid_val <= 0:
                base = normalized[idx]
                errors.append(
                    f"Missing conId for {base.get('symbol')} {base.get('expiration')} {base.get('right')} {base.get('strike')}; skipping"
                )
                continue
            request_contracts.append(contract)
            request_normalized.append(normalized[idx])

        if not request_contracts:
            return {"options": [], "errors": errors}

        try:
            tickers = await self.ib.reqTickersAsync(*request_contracts)
        except Exception as e:
            msg = f"Failed to request Greeks from IBKR for {len(request_contracts)} contracts: {e}"
            logger.error(msg)
            errors.append(msg)
            return {"options": [], "errors": errors}

        results: List[Dict[str, Any]] = []
        for idx, ticker in enumerate(tickers):
            base = request_normalized[idx]
            greeks_obj = getattr(ticker, "modelGreeks", None)

            greek_values = None
            implied_vol = None

            if greeks_obj:
                greek_values = {
                    "delta": getattr(greeks_obj, "delta", None),
                    "gamma": getattr(greeks_obj, "gamma", None),
                    "theta": getattr(greeks_obj, "theta", None),
                    "vega": getattr(greeks_obj, "vega", None),
                    "rho": getattr(greeks_obj, "rho", None),
                }
                implied_vol = getattr(greeks_obj, "impliedVol", None)

            results.append(
                {
                    **base,
                    "greeks": greek_values,
                    "implied_volatility": implied_vol,
                    "data_source": "IBKR",
                }
            )

        return {"options": results, "errors": errors}

    async def get_account_summary(self) -> dict:
        """Get account summary information per account.
        
        Returns a dict keyed by account ID, each containing the account's
        financial metrics (NetLiquidation, MaintMarginReq, etc.).
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")

        # Request account values
        account_values = self.ib.accountValues()

        # Group by account for per-account margin display
        by_account = {}
        for av in account_values:
            if av.tag in ['NetLiquidation', 'TotalCashValue', 'GrossPositionValue',
                          'AvailableFunds', 'BuyingPower', 'MaintMarginReq']:
                account = av.account or "default"
                if account not in by_account:
                    by_account[account] = {}
                by_account[account][av.tag] = {
                    "value": float(av.value) if av.value else 0.0,
                    "currency": av.currency
                }

        return by_account

    async def get_executions(
        self,
        start_dt: datetime,
        end_dt: datetime,
        account: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch executions in a time window and normalize them for DB ingestion.

        Notes:
        - The IB API execution query often supports a lower bound but not a strict upper bound.
          We request "since start" and then filter client-side by end_dt.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")

        start_dt_utc = _as_utc_naive(start_dt)
        end_dt_utc = _as_utc_naive(end_dt)

        filt = None
        if ExecutionFilter is not None:
            filt = ExecutionFilter()
            filt.time = start_dt_utc.strftime("%Y%m%d %H:%M:%S")
            if account:
                filt.acctCode = account

        try:
            if hasattr(self.ib, "reqExecutionsAsync"):
                # Some ib_async builds accept an optional filter arg.
                fills = await self.ib.reqExecutionsAsync(filt) if filt is not None else await self.ib.reqExecutionsAsync()
            else:
                fills = self.ib.reqExecutions(filt) if filt is not None else self.ib.reqExecutions()
        except Exception as e:
            logger.error(f"Failed to request executions from IBKR: {e}")
            raise

        normalized: List[Dict[str, Any]] = []
        for fill in fills or []:
            try:
                execution = getattr(fill, "execution", None)
                contract = getattr(fill, "contract", None)
                commission_report = getattr(fill, "commissionReport", None)

                if execution is None or contract is None:
                    continue

                trade_dt = _parse_ibkr_time(getattr(execution, "time", None))
                if trade_dt is None:
                    continue

                trade_dt_utc = _as_utc_naive(trade_dt)
                if trade_dt_utc < start_dt_utc or trade_dt_utc > end_dt_utc:
                    continue

                side = (getattr(execution, "side", "") or "").strip().upper()
                if side not in ("BUY", "SELL"):
                    # Avoid violating DB CHECK constraint; keep but normalize common variants.
                    if side.startswith("B"):
                        side = "BUY"
                    elif side.startswith("S"):
                        side = "SELL"

                expiration_iso = _parse_contract_expiration(getattr(contract, "lastTradeDateOrContractMonth", None))
                multiplier_val = getattr(contract, "multiplier", None)
                try:
                    multiplier = int(multiplier_val) if multiplier_val not in (None, "") else None
                except Exception:
                    multiplier = None

                row = {
                    "account": getattr(execution, "acctNumber", None) or account,
                    "exec_id": getattr(execution, "execId", None),
                    "order_id": getattr(execution, "orderId", None),
                    "perm_id": getattr(execution, "permId", None),
                    "client_id": getattr(execution, "clientId", None),
                    "conid": getattr(contract, "conId", None),
                    "symbol": getattr(contract, "symbol", None),
                    "sec_type": getattr(contract, "secType", None),
                    "currency": getattr(contract, "currency", None),
                    "exchange": getattr(contract, "exchange", None),
                    "trade_time": trade_dt_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                    "side": side,
                    "quantity": getattr(execution, "shares", None),
                    "price": getattr(execution, "price", None),
                    "liquidation": getattr(execution, "liquidation", None),
                    "multiplier": multiplier,
                    "right": getattr(contract, "right", None),
                    "strike": getattr(contract, "strike", None),
                    "expiration": expiration_iso,
                }

                row["raw_json"] = json.dumps(
                    {
                        "execution": {k: getattr(execution, k, None) for k in ("execId", "orderId", "permId", "clientId", "acctNumber", "time", "side", "shares", "price", "liquidation")},
                        "contract": {k: getattr(contract, k, None) for k in ("conId", "symbol", "secType", "currency", "exchange", "strike", "right", "multiplier", "lastTradeDateOrContractMonth")},
                        "commissionReport": _commission_report_dict(commission_report),
                    },
                    default=str,
                )

                row["commission_report"] = _commission_report_dict(commission_report)
                normalized.append(row)
            except Exception as e:
                logger.error(f"Failed to normalize execution fill: {e}")
                continue

        return normalized


def normalize_expiration(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.replace("-", ""), "%Y%m%d").strftime("%Y%m%d")
    except ValueError:
        return None


def normalize_option_right(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    if not v:
        return None
    if v[0] in ("C", "P"):
        return v[0]
    return None


def _as_utc_naive(dt: datetime) -> datetime:
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_ibkr_time(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None

        # Common formats:
        # - "YYYYMMDD  HH:MM:SS" (sometimes double-space)
        # - "YYYYMMDD HH:MM:SS" with optional trailing timezone text
        parts = s.split()
        if len(parts) >= 2 and len(parts[0]) == 8 and ":" in parts[1]:
            candidate = f"{parts[0]} {parts[1]}"
            for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d  %H:%M:%S"):
                try:
                    return datetime.strptime(candidate, fmt)
                except ValueError:
                    pass

        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s[:19], fmt)
            except ValueError:
                pass

    return None


def _parse_contract_expiration(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) >= 8 and s[:8].isdigit():
        try:
            d = datetime.strptime(s[:8], "%Y%m%d").date()
            return d.isoformat()
        except ValueError:
            return None
    return None


def _commission_report_dict(cr) -> Optional[Dict[str, Any]]:
    if cr is None:
        return None
    try:
        yield_val = getattr(cr, "yield_", None)
        if yield_val is None:
            yield_val = getattr(cr, "yield", None)

        yrd = getattr(cr, "yieldRedemptionDate", None)
        if isinstance(yrd, (datetime, date)):
            yrd_val = yrd.isoformat()
        elif isinstance(yrd, str):
            yrd_val = yrd.strip() or None
        else:
            yrd_val = None

        return {
            "commission": getattr(cr, "commission", None),
            "currency": getattr(cr, "currency", None),
            "realized_pnl": getattr(cr, "realizedPNL", None),
            "yield": yield_val,
            "yield_redemption_date": yrd_val,
            "raw_json": json.dumps({k: getattr(cr, k, None) for k in ("commission", "currency", "realizedPNL", "yield_", "yieldRedemptionDate")}, default=str),
        }
    except Exception:
        return {
            "raw_json": json.dumps({"repr": repr(cr)}, default=str),
        }


# Global client instance
ibkr_client = IBKRClient()
