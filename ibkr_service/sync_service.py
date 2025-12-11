"""Sync service for synchronizing IBKR positions to Wheeler database."""
from datetime import date, datetime
from typing import Optional
import logging

from ibkr_client import ibkr_client
from database import (
    get_db_connection,
    sync_option_position,
    sync_long_position,
    close_missing_options,
    close_missing_long_positions,
    get_sync_status
)

logger = logging.getLogger(__name__)


class SyncService:
    """Service for syncing IBKR positions to Wheeler database."""
    
    def __init__(self):
        self.last_sync: Optional[datetime] = None
        self.last_sync_result: Optional[dict] = None
    
    async def sync_positions(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None
    ) -> dict:
        """
        Sync all positions from IBKR to Wheeler database.
        
        Args:
            host: TWS/Gateway hostname
            port: TWS/Gateway port
            client_id: Client ID for connection
            
        Returns:
            Sync result with counts and any errors
        """
        result = {
            "success": False,
            "synced_at": None,
            "options_synced": 0,
            "positions_synced": 0,
            "options_closed": 0,
            "positions_closed": 0,
            "errors": []
        }
        
        try:
            # Connect if not already connected
            if not ibkr_client.is_connected:
                connected = await ibkr_client.connect(host, port, client_id)
                if not connected:
                    result["errors"].append("Failed to connect to IBKR")
                    return result
            
            # Get positions from IBKR
            positions = await ibkr_client.get_positions()
            
            # Connect to database
            conn = get_db_connection()
            
            option_ids = []
            stock_symbols = []
            
            try:
                for pos in positions:
                    try:
                        if pos["sec_type"] == "OPT":
                            # Sync option position
                            option_type = "Call" if pos["right"] == "C" else "Put"
                            
                            # Parse expiration date (YYYYMMDD format)
                            exp_str = pos["expiration"]
                            expiration = date(
                                int(exp_str[:4]),
                                int(exp_str[4:6]),
                                int(exp_str[6:8])
                            )
                            
                            # Calculate premium (avg_cost / multiplier per contract)
                            multiplier = pos.get("multiplier", 100)
                            premium_per_contract = pos["avg_cost"] / multiplier
                            
                            option_id = sync_option_position(
                                conn=conn,
                                symbol=pos["symbol"],
                                option_type=option_type,
                                strike=pos["strike"],
                                expiration=expiration,
                                contracts=abs(int(pos["position"])),
                                premium=premium_per_contract
                            )
                            option_ids.append(option_id)
                            result["options_synced"] += 1
                            
                        elif pos["sec_type"] == "STK":
                            # Sync stock position
                            position_id = sync_long_position(
                                conn=conn,
                                symbol=pos["symbol"],
                                shares=int(pos["position"]),
                                buy_price=pos["avg_cost"]
                            )
                            stock_symbols.append(pos["symbol"])
                            result["positions_synced"] += 1
                            
                    except Exception as e:
                        error_msg = f"Error syncing position {pos.get('symbol', 'unknown')}: {e}"
                        logger.error(error_msg)
                        result["errors"].append(error_msg)
                
                # Close positions no longer in IBKR
                result["options_closed"] = close_missing_options(conn, option_ids)
                result["positions_closed"] = close_missing_long_positions(conn, stock_symbols)
                
                result["success"] = True
                result["synced_at"] = datetime.now().isoformat()
                
            finally:
                conn.close()
            
        except Exception as e:
            error_msg = f"Sync failed: {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)
        
        self.last_sync = datetime.now()
        self.last_sync_result = result
        
        return result
    
    def get_status(self) -> dict:
        """Get current sync status."""
        try:
            conn = get_db_connection()
            db_status = get_sync_status(conn)
            conn.close()
        except Exception as e:
            logger.error(f"Error getting sync status: {e}")
            db_status = {"error": str(e)}
        
        return {
            "connected": ibkr_client.is_connected,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "last_sync_result": self.last_sync_result,
            "database": db_status
        }


# Global sync service instance
sync_service = SyncService()
