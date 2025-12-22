"""IBKR client module using ib_async for Interactive Brokers connection."""
import asyncio
from datetime import date
from typing import Optional, List, Dict, Any
import logging

from ib_async import IB, Contract, Option, Stock

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
                "symbol": contract.symbol,
                "sec_type": contract.secType,
                "position": pos.position,
                "avg_cost": pos.avgCost,
            }
            
            # Add option-specific fields
            if contract.secType == "OPT":
                position_data.update({
                    "strike": contract.strike,
                    "expiration": contract.lastTradeDateOrContractMonth,
                    "right": contract.right,  # 'C' for Call, 'P' for Put
                    "multiplier": int(contract.multiplier) if contract.multiplier else 100
                })
            
            result.append(position_data)
        
        logger.info(f"Retrieved {len(result)} positions from IBKR")
        return result

    async def get_option_greeks(self, options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Get real-time option Greeks for the provided option positions.

        Args:
            options: List of option position dictionaries (symbol, strike, expiration, right)

        Returns:
            List of dictionaries containing option metadata and Greek values.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")

        if not options:
            return []

        contracts = []
        normalized = []

        for opt in options:
            try:
                expiration = opt.get("expiration")
                # Normalize expiration to IBKR expected format YYYYMMDD
                if expiration and "-" in expiration:
                    expiration = expiration.replace("-", "")

                right = opt.get("right") or opt.get("type")
                if right:
                    right = right[0].upper()  # C or P

                contract = Option(
                    symbol=opt["symbol"],
                    lastTradeDateOrContractMonth=expiration,
                    strike=float(opt["strike"]),
                    right=right,
                    exchange="SMART",
                    currency="USD",
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
                logger.error(f"Failed to prepare option contract for Greeks: {e}")
                continue

        if not contracts:
            return []

        try:
            tickers = await self.ib.reqTickersAsync(*contracts)
        except Exception as e:
            logger.error(f"Failed to request Greeks from IBKR: {e}")
            raise

        results: List[Dict[str, Any]] = []
        for idx, ticker in enumerate(tickers):
            base = normalized[idx]
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

        return results
    
    async def get_account_summary(self) -> dict:
        """Get account summary information."""
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")
        
        # Request account values
        account_values = self.ib.accountValues()
        
        summary = {}
        for av in account_values:
            if av.tag in ['NetLiquidation', 'TotalCashValue', 'GrossPositionValue', 
                          'AvailableFunds', 'BuyingPower', 'MaintMarginReq']:
                summary[av.tag] = {
                    "value": float(av.value) if av.value else 0.0,
                    "currency": av.currency
                }
        
        return summary


# Global client instance
ibkr_client = IBKRClient()
