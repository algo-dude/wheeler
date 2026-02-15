"""Sync service for synchronizing IBKR positions to Wheeler database."""
from datetime import date, datetime, timedelta
from typing import Optional
import logging

from ibkr_client import ibkr_client
from database import (
    get_db_connection,
    sync_option_position,
    sync_long_position,
    close_missing_options,
    close_missing_long_positions,
    get_sync_status,
    upsert_ibkr_execution,
    upsert_ibkr_commission_report,
    get_transactions_db_status,
)

logger = logging.getLogger(__name__)


class SyncService:
    """Service for syncing IBKR positions to Wheeler database."""
    
    def __init__(self):
        self.last_sync: Optional[datetime] = None
        self.last_sync_result: Optional[dict] = None

        self.last_transactions_sync: Optional[datetime] = None
        self.last_transactions_sync_result: Optional[dict] = None
        
        # Account summary data (fetched during sync)
        self.account_summary: Optional[dict] = None
    
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
                                contracts=int(pos["position"]),  # Keep sign - IBKR reports short positions as negative
                                premium=premium_per_contract,
                                account=pos.get("account")
                            )
                            option_ids.append(option_id)
                            result["options_synced"] += 1
                            
                        elif pos["sec_type"] == "STK":
                            # Sync stock position
                            position_id = sync_long_position(
                                conn=conn,
                                symbol=pos["symbol"],
                                shares=int(pos["position"]),
                                buy_price=pos["avg_cost"],
                                account=pos.get("account")
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
            
            # Fetch account summary after successful sync
            try:
                self.account_summary = await ibkr_client.get_account_summary()
                logger.info(f"Fetched account summary: {self.account_summary}")
            except Exception as e:
                logger.warning(f"Failed to fetch account summary: {e}")
            
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
            "database": db_status,
            "account_summary": self.account_summary
        }

    async def sync_transactions(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        lookback_days: int = 7,
        account: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        result = {
            "success": False,
            "synced_at": None,
            "executions_upserted": 0,
            "commission_reports_upserted": 0,
            "start": None,
            "end": None,
            "warnings": [],
            "errors": [],
        }

        try:
            if not ibkr_client.is_connected:
                connected = await ibkr_client.connect(host, port, client_id)
                if not connected:
                    result["errors"].append("Failed to connect to IBKR")
                    return result

            start_dt, end_dt = _compute_window(start, end, lookback_days)
            result["start"] = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
            result["end"] = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
            if dry_run:
                result["warnings"].append("dry_run=true: no DB writes will be performed")

            executions = await ibkr_client.get_executions(start_dt, end_dt, account)

            # Connect to database
            conn = get_db_connection()
            try:
                if not dry_run:
                    conn.execute("BEGIN")

                exec_upserts = 0
                comm_upserts = 0

                # Upsert executions first (FK dependency)
                for row in executions:
                    try:
                        if not row.get("account") or not row.get("exec_id"):
                            raise ValueError("Missing account or exec_id")
                        if not row.get("symbol") or not row.get("sec_type"):
                            raise ValueError("Missing symbol or sec_type")
                        if not row.get("trade_time") or not row.get("side"):
                            raise ValueError("Missing trade_time or side")
                        if row.get("quantity") is None or row.get("price") is None:
                            raise ValueError("Missing quantity or price")

                        if not dry_run:
                            upsert_ibkr_execution(conn, row)
                        exec_upserts += 1
                    except Exception as e:
                        result["errors"].append(f"Execution upsert failed for exec_id={row.get('exec_id')}: {e}")

                # Then upsert commission reports (best-effort)
                for row in executions:
                    try:
                        cr = row.get("commission_report")
                        if not cr:
                            continue

                        cr_row = {
                            "account": row.get("account"),
                            "exec_id": row.get("exec_id"),
                            "commission": cr.get("commission"),
                            "currency": cr.get("currency"),
                            "realized_pnl": cr.get("realized_pnl"),
                            "yield": cr.get("yield"),
                            "yield_redemption_date": cr.get("yield_redemption_date"),
                            "raw_json": cr.get("raw_json"),
                        }

                        if not cr_row.get("account") or not cr_row.get("exec_id"):
                            raise ValueError("Missing account or exec_id")

                        if not dry_run:
                            upsert_ibkr_commission_report(conn, cr_row)
                        comm_upserts += 1
                    except Exception as e:
                        result["errors"].append(f"Commission upsert failed for exec_id={row.get('exec_id')}: {e}")

                if exec_upserts > 0 and comm_upserts == 0:
                    result["warnings"].append("No commission reports were available for the fetched executions (they may lag; re-run the rolling window sync later)")

                if not dry_run:
                    conn.commit()

                result["success"] = True
                result["synced_at"] = datetime.now().isoformat()
                result["executions_upserted"] = exec_upserts
                result["commission_reports_upserted"] = comm_upserts

            finally:
                conn.close()

        except Exception as e:
            result["errors"].append(f"Transactions sync failed: {e}")

        self.last_transactions_sync = datetime.now()
        self.last_transactions_sync_result = result
        return result

    def get_transactions_status(self) -> dict:
        try:
            conn = get_db_connection()
            db_status = get_transactions_db_status(conn)
            conn.close()
        except Exception as e:
            logger.error(f"Error getting transactions status: {e}")
            db_status = {"error": str(e)}

        return {
            "connected": ibkr_client.is_connected,
            "last_transactions_sync": self.last_transactions_sync.isoformat() if self.last_transactions_sync else None,
            "last_transactions_sync_result": self.last_transactions_sync_result,
            "database": db_status,
        }


def _parse_iso_datetime(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1]

    # Date-only string
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        return datetime.fromisoformat(v)

    return datetime.fromisoformat(v)


def _compute_window(start: Optional[str], end: Optional[str], lookback_days: int) -> tuple[datetime, datetime]:
    now = datetime.now()
    if not start and not end:
        if lookback_days <= 0:
            lookback_days = 7
        return now - timedelta(days=lookback_days), now

    start_dt = _parse_iso_datetime(start) if start else now - timedelta(days=max(1, lookback_days))
    end_dt = _parse_iso_datetime(end) if end else now

    # If the user provided date-only strings, normalize to inclusive day bounds.
    if start and len(start.strip()) == 10:
        start_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if end and len(end.strip()) == 10:
        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=0)

    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    return start_dt, end_dt


# Global sync service instance
sync_service = SyncService()
