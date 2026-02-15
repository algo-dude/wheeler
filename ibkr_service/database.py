"""Database operations for syncing IBKR positions to Wheeler SQLite."""
import logging
import os
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


def _resolve_data_dir() -> Path:
    """Resolve the Wheeler data directory containing `currentdb` and the DB files.

    Priority:
    1) IBKR_DATA_DIR (or WHEELER_DATA_DIR)
    2) ./data next to the ibkr_service code (Docker image layout: /app/data)
    3) ../data relative to the ibkr_service directory (repo layout)
    """
    env_dir = os.getenv("IBKR_DATA_DIR") or os.getenv("WHEELER_DATA_DIR")
    if env_dir:
        return Path(env_dir)

    here = Path(__file__).resolve().parent

    candidate = here / "data"
    if (candidate / "currentdb").exists() or candidate.exists():
        return candidate

    return here.parent / "data"


def resolve_database_path() -> str:
    """Resolve the SQLite DB path the service should write to.

    If `IBKR_DATABASE_PATH` is set, it is always honored.
    Otherwise, the active DB name is read from `data/currentdb` and opened under `data/`.
    """
    explicit = os.getenv("IBKR_DATABASE_PATH")
    if explicit:
        return explicit

    data_dir = _resolve_data_dir()
    currentdb_file = data_dir / "currentdb"

    try:
        if currentdb_file.exists():
            active_db_name = currentdb_file.read_text(encoding="utf-8").strip()
            if active_db_name:
                # Wheeler writes just a filename; keep this constrained to the data dir.
                active_db_name = Path(active_db_name).name
                return str(data_dir / active_db_name)
    except Exception:
        # Fall through to configured default.
        pass

    return settings.database_path


def get_db_connection():
    """Get a connection to the Wheeler SQLite database."""
    conn = sqlite3.connect(resolve_database_path())
    conn.row_factory = sqlite3.Row
    return conn


def ensure_symbol_exists(conn: sqlite3.Connection, symbol: str) -> None:
    """Ensure a symbol exists in the symbols table."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO symbols (symbol) VALUES (?)",
        (symbol,)
    )
    conn.commit()


def sync_option_position(
    conn: sqlite3.Connection,
    symbol: str,
    option_type: str,  # 'Put' or 'Call'
    strike: float,
    expiration: date,
    contracts: int,
    premium: float,
    opened: Optional[date] = None,
    account: Optional[str] = None
) -> int:
    """
    Sync an option position from IBKR to Wheeler database.
    
    For IBKR-synced positions, we identify by (symbol, type, strike, expiration, account)
    for active (unclosed) positions. Premium and contracts may change when averaging.
    
    Returns the ID of the inserted/updated option.
    """
    ensure_symbol_exists(conn, symbol)
    
    opened_date = opened or date.today()
    cursor = conn.cursor()
    
    # First, check if there's an active (unclosed) option for this account/symbol/strike/expiration/type
    # This handles the case where premium/contracts changed (e.g., averaging into a position)
    cursor.execute(
        """
        SELECT id FROM options 
        WHERE symbol = ? AND type = ? AND strike = ? AND expiration = ? AND closed IS NULL
        AND (account = ? OR (account IS NULL AND ? IS NULL))
        """,
        (symbol, option_type, strike, expiration.isoformat(), account, account)
    )
    existing = cursor.fetchone()
    
    if existing:
        # Update existing active position (premium/contracts may have changed)
        cursor.execute(
            """
            UPDATE options 
            SET contracts = ?, premium = ?, account = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (contracts, premium, account, existing['id'])
        )
        option_id = existing['id']
        logger.info(f"Updated option {option_id}: {symbol} {option_type} {strike} {expiration}")
    else:
        # No active position found - try to insert, but handle unique constraint conflicts
        # The unique index is on (symbol, type, opened, strike, expiration, premium, contracts)
        # If there's a closed position with same key, we should insert a new active one
        # Use a different opened date if needed to avoid conflicts with closed positions
        try:
            cursor.execute(
                """
                INSERT INTO options (symbol, type, opened, strike, expiration, premium, contracts, account)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, option_type, opened_date.isoformat(), strike, expiration.isoformat(), premium, contracts, account)
            )
            option_id = cursor.lastrowid
            logger.info(f"Inserted option {option_id}: {symbol} {option_type} {strike} {expiration}")
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                # There's a conflicting record (likely closed). Find and reopen it or update it.
                cursor.execute(
                    """
                    SELECT id, closed FROM options 
                    WHERE symbol = ? AND type = ? AND opened = ? AND strike = ? 
                    AND expiration = ? AND premium = ? AND contracts = ?
                    """,
                    (symbol, option_type, opened_date.isoformat(), strike, expiration.isoformat(), premium, contracts)
                )
                conflict_row = cursor.fetchone()
                if conflict_row:
                    # Reopen the closed position and update account
                    cursor.execute(
                        """
                        UPDATE options 
                        SET closed = NULL, account = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (account, conflict_row['id'])
                    )
                    option_id = conflict_row['id']
                    logger.info(f"Reopened option {option_id}: {symbol} {option_type} {strike} {expiration}")
                else:
                    # Shouldn't happen, but re-raise if we can't find the conflict
                    raise
            else:
                raise
    
    conn.commit()
    return option_id


def sync_long_position(
    conn: sqlite3.Connection,
    symbol: str,
    shares: int,
    buy_price: float,
    opened: Optional[date] = None,
    account: Optional[str] = None
) -> int:
    """
    Sync a stock position from IBKR to Wheeler database.
    
    Returns the ID of the inserted/updated position.
    """
    ensure_symbol_exists(conn, symbol)
    
    opened_date = opened or date.today()
    cursor = conn.cursor()
    
    # Check if there's an open position for this symbol AND account
    cursor.execute(
        """
        SELECT id, shares, buy_price FROM long_positions 
        WHERE symbol = ? AND closed IS NULL
        AND (account = ? OR (account IS NULL AND ? IS NULL))
        ORDER BY opened DESC LIMIT 1
        """,
        (symbol, account, account)
    )
    existing = cursor.fetchone()
    
    if existing:
        # Update existing position
        cursor.execute(
            """
            UPDATE long_positions 
            SET shares = ?, buy_price = ?, account = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (shares, buy_price, account, existing['id'])
        )
        position_id = existing['id']
        logger.info(f"Updated long position {position_id}: {symbol} {shares} shares @ ${buy_price:.2f}")
    else:
        # Insert new position
        cursor.execute(
            """
            INSERT INTO long_positions (symbol, opened, shares, buy_price, account)
            VALUES (?, ?, ?, ?, ?)
            """,
            (symbol, opened_date.isoformat(), shares, buy_price, account)
        )
        position_id = cursor.lastrowid
        logger.info(f"Inserted long position {position_id}: {symbol} {shares} shares @ ${buy_price:.2f}")
    
    conn.commit()
    return position_id


def close_missing_options(conn: sqlite3.Connection, current_option_ids: list[int]) -> int:
    """
    Close options that are no longer in IBKR (expired/exercised/closed).
    
    Returns the number of options closed.
    """
    if not current_option_ids:
        # Close all open options if none from IBKR
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE options 
            SET closed = ?, updated_at = CURRENT_TIMESTAMP
            WHERE closed IS NULL
            """,
            (date.today().isoformat(),)
        )
        count = cursor.rowcount
        conn.commit()
        return count
    
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(current_option_ids))
    cursor.execute(
        f"""
        UPDATE options 
        SET closed = ?, updated_at = CURRENT_TIMESTAMP
        WHERE closed IS NULL AND id NOT IN ({placeholders})
        """,
        [date.today().isoformat()] + current_option_ids
    )
    count = cursor.rowcount
    conn.commit()
    logger.info(f"Closed {count} options no longer in IBKR")
    return count


def close_missing_long_positions(conn: sqlite3.Connection, current_symbols: list[str]) -> int:
    """
    Close long positions that are no longer in IBKR.
    
    Returns the number of positions closed.
    """
    if not current_symbols:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE long_positions 
            SET closed = ?, updated_at = CURRENT_TIMESTAMP
            WHERE closed IS NULL
            """,
            (date.today().isoformat(),)
        )
        count = cursor.rowcount
        conn.commit()
        return count
    
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(current_symbols))
    cursor.execute(
        f"""
        UPDATE long_positions 
        SET closed = ?, updated_at = CURRENT_TIMESTAMP
        WHERE closed IS NULL AND symbol NOT IN ({placeholders})
        """,
        [date.today().isoformat()] + current_symbols
    )
    count = cursor.rowcount
    conn.commit()
    logger.info(f"Closed {count} long positions no longer in IBKR")
    return count


def get_sync_status(conn: sqlite3.Connection) -> dict:
    """Get current sync status from database."""
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM options WHERE closed IS NULL")
    open_options = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM long_positions WHERE closed IS NULL")
    open_positions = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(DISTINCT symbol) FROM symbols")
    total_symbols = cursor.fetchone()[0]
    
    return {
        "open_options": open_options,
        "open_positions": open_positions,
        "total_symbols": total_symbols
    }


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table_name,),
    )
    return cursor.fetchone() is not None


def upsert_ibkr_execution(conn: sqlite3.Connection, row: dict, source: str = "API") -> None:
    """Upsert an IBKR execution row keyed by (account, exec_id).
    
    Args:
        conn: Database connection
        row: Dict with execution fields
        source: Data source - 'API' (live) or 'FLEX' (historical)
    """
    conn.execute(
        """
        INSERT INTO ibkr_executions (
          account, exec_id,
          order_id, perm_id, client_id,
          conid, symbol, sec_type, currency, exchange,
          trade_time, side, quantity, price, liquidation,
          multiplier, right, strike, expiration,
          raw_json, source,
          updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(account, exec_id) DO UPDATE SET
          order_id=excluded.order_id,
          perm_id=excluded.perm_id,
          client_id=excluded.client_id,
          conid=excluded.conid,
          symbol=excluded.symbol,
          sec_type=excluded.sec_type,
          currency=excluded.currency,
          exchange=excluded.exchange,
          trade_time=excluded.trade_time,
          side=excluded.side,
          quantity=excluded.quantity,
          price=excluded.price,
          liquidation=excluded.liquidation,
          multiplier=excluded.multiplier,
          right=excluded.right,
          strike=excluded.strike,
          expiration=excluded.expiration,
          raw_json=excluded.raw_json,
          source=excluded.source,
          updated_at=CURRENT_TIMESTAMP;
        """,
        (
            row.get("account"),
            row.get("exec_id"),
            row.get("order_id"),
            row.get("perm_id"),
            row.get("client_id"),
            row.get("conid"),
            row.get("symbol"),
            row.get("sec_type"),
            row.get("currency"),
            row.get("exchange"),
            row.get("trade_time"),
            row.get("side"),
            row.get("quantity"),
            row.get("price"),
            row.get("liquidation"),
            row.get("multiplier"),
            row.get("right"),
            row.get("strike"),
            row.get("expiration"),
            row.get("raw_json"),
            source,
        ),
    )


def upsert_ibkr_commission_report(conn: sqlite3.Connection, row: dict, source: str = "API") -> None:
    """Upsert a commission report row keyed by (account, exec_id).
    
    Args:
        conn: Database connection
        row: Dict with commission report fields
        source: Data source - 'API' (live) or 'FLEX' (historical)
    """
    # Support both 'yield' and 'yield_value' keys for backward compatibility
    yield_val = row.get("yield_value") or row.get("yield")
    conn.execute(
        """
        INSERT INTO ibkr_commission_reports (
          account, exec_id,
          commission, currency, realized_pnl, yield_value, yield_redemption_date,
          raw_json, source,
          updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(account, exec_id) DO UPDATE SET
          commission=excluded.commission,
          currency=excluded.currency,
          realized_pnl=excluded.realized_pnl,
          yield_value=excluded.yield_value,
          yield_redemption_date=excluded.yield_redemption_date,
          raw_json=excluded.raw_json,
          source=excluded.source,
          updated_at=CURRENT_TIMESTAMP;
        """,
        (
            row.get("account"),
            row.get("exec_id"),
            row.get("commission"),
            row.get("currency"),
            row.get("realized_pnl"),
            yield_val,
            row.get("yield_redemption_date"),
            row.get("raw_json"),
            source,
        ),
    )


def get_transactions_db_status(conn: sqlite3.Connection) -> dict:
    """Return record counts for transaction ingestion tables.

    If tables are missing (older schema), returns a helpful error rather than raising.
    """
    status: dict = {
        "path": resolve_database_path(),
    }

    if not _table_exists(conn, "ibkr_executions"):
        status["error"] = "Missing table ibkr_executions (schema not yet applied)"
        return status

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM ibkr_executions")
        rows = cursor.fetchone()[0]
        cursor.execute("SELECT MAX(trade_time) FROM ibkr_executions")
        max_trade_time = cursor.fetchone()[0]
        status["ibkr_executions"] = {"rows": rows, "max_trade_time": max_trade_time}
    except Exception as e:
        status["ibkr_executions"] = {"error": str(e)}

    if _table_exists(conn, "ibkr_commission_reports"):
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM ibkr_commission_reports")
            status["ibkr_commission_reports"] = {"rows": cursor.fetchone()[0]}
        except Exception as e:
            status["ibkr_commission_reports"] = {"error": str(e)}
    else:
        status["ibkr_commission_reports"] = {"error": "Missing table ibkr_commission_reports (schema not yet applied)"}

    # Optional table (not created in MVP) - report 0 if missing.
    if _table_exists(conn, "ibkr_activities"):
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM ibkr_activities")
            status["ibkr_activities"] = {"rows": cursor.fetchone()[0]}
        except Exception as e:
            status["ibkr_activities"] = {"error": str(e)}
    else:
        status["ibkr_activities"] = {"rows": 0}

    return status


def _safe_json(obj) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return json.dumps({"repr": repr(obj)}, default=str)


# -------------------------------------------------------------------------
# Activity and Cost Basis Helpers
# -------------------------------------------------------------------------

def upsert_ibkr_activity(conn: sqlite3.Connection, row: dict, source: str = "API") -> Optional[int]:
    """Upsert an activity record into ibkr_activities.
    
    Args:
        conn: Database connection
        row: Dict with activity fields
        source: Data source - 'API' (live) or 'FLEX' (historical)
        
    Returns:
        Row ID if successful, None otherwise
    """
    try:
        activity_id = row.get('activity_id')
        if not activity_id:
            # Generate a unique activity_id from the event details
            activity_id = f"{row.get('activity_type', 'UNKNOWN')}_{row.get('symbol', '')}_{row.get('event_time', '')}"
        
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ibkr_activities (
                account, activity_id, activity_type, conid, symbol, event_time,
                quantity, amount, currency, description, raw_text, raw_json,
                related_exec_id, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account, activity_id) DO UPDATE SET
                activity_type = excluded.activity_type,
                conid = excluded.conid,
                symbol = excluded.symbol,
                event_time = excluded.event_time,
                quantity = excluded.quantity,
                amount = excluded.amount,
                currency = excluded.currency,
                description = excluded.description,
                raw_text = excluded.raw_text,
                raw_json = excluded.raw_json,
                related_exec_id = excluded.related_exec_id,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
        """, (
            row.get('account'),
            activity_id,
            row.get('activity_type'),
            row.get('conid'),
            row.get('symbol'),
            row.get('event_time'),
            row.get('quantity'),
            row.get('amount'),
            row.get('currency'),
            row.get('description'),
            row.get('raw_text'),
            row.get('raw_json'),
            row.get('related_exec_id'),
            source,
        ))
        return cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to upsert activity: {e}")
        return None


def get_activities_by_type(
    conn: sqlite3.Connection,
    activity_type: str,
    account: Optional[str] = None,
    symbol: Optional[str] = None,
) -> list[dict]:
    """Get activities of a specific type.
    
    Args:
        conn: Database connection
        activity_type: Type of activity (ASSIGNMENT, EXERCISE, EXPIRATION, etc.)
        account: Optional account filter
        symbol: Optional symbol filter
        
    Returns:
        List of activity dicts
    """
    query = """
        SELECT * FROM ibkr_activities
        WHERE activity_type = ?
    """
    params: list = [activity_type]
    
    if account:
        query += " AND account = ?"
        params.append(account)
    
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    
    query += " ORDER BY event_time DESC"
    
    cursor = conn.cursor()
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def get_option_executions_for_contract(
    conn: sqlite3.Connection,
    symbol: str,
    strike: float,
    expiration: str,
    right: str,
    account: Optional[str] = None,
) -> list[dict]:
    """Get all executions for a specific option contract.
    
    Args:
        conn: Database connection
        symbol: Underlying symbol
        strike: Option strike price
        expiration: Expiration date (YYYY-MM-DD format)
        right: Option right ('C' or 'P')
        account: Optional account filter
        
    Returns:
        List of execution dicts with commission data
    """
    query = """
        SELECT 
            e.*,
            c.commission,
            c.realized_pnl
        FROM ibkr_executions e
        LEFT JOIN ibkr_commission_reports c 
            ON e.account = c.account AND e.exec_id = c.exec_id
        WHERE e.sec_type = 'OPT'
          AND e.symbol = ?
          AND e.strike = ?
          AND e.expiration = ?
          AND e.right = ?
    """
    params: list = [symbol, strike, expiration, right]
    
    if account:
        query += " AND e.account = ?"
        params.append(account)
    
    query += " ORDER BY e.trade_time"
    
    cursor = conn.cursor()
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def sum_commissions_for_option(
    conn: sqlite3.Connection,
    symbol: str,
    strike: float,
    expiration: str,
    right: str,
    account: Optional[str] = None,
) -> float:
    """Sum all commissions for a specific option contract.
    
    Args:
        conn: Database connection
        symbol: Underlying symbol
        strike: Option strike price
        expiration: Expiration date (YYYY-MM-DD format)
        right: Option right ('C' or 'P')
        account: Optional account filter
        
    Returns:
        Total commission amount
    """
    query = """
        SELECT COALESCE(SUM(c.commission), 0) as total
        FROM ibkr_executions e
        JOIN ibkr_commission_reports c 
            ON e.account = c.account AND e.exec_id = c.exec_id
        WHERE e.sec_type = 'OPT'
          AND e.symbol = ?
          AND e.strike = ?
          AND e.expiration = ?
          AND e.right = ?
    """
    params: list = [symbol, strike, expiration, right]
    
    if account:
        query += " AND e.account = ?"
        params.append(account)
    
    cursor = conn.cursor()
    cursor.execute(query, params)
    result = cursor.fetchone()
    return result['total'] if result else 0.0


def get_stock_executions_around_date(
    conn: sqlite3.Connection,
    symbol: str,
    target_date: str,
    days_window: int = 7,
    account: Optional[str] = None,
) -> list[dict]:
    """Get stock executions around a specific date.
    
    Useful for finding stock trades that might be related to option assignments.
    
    Args:
        conn: Database connection
        symbol: Stock symbol
        target_date: Center date (YYYY-MM-DD format)
        days_window: Number of days before and after target_date
        account: Optional account filter
        
    Returns:
        List of execution dicts
    """
    query = """
        SELECT e.*, c.commission
        FROM ibkr_executions e
        LEFT JOIN ibkr_commission_reports c 
            ON e.account = c.account AND e.exec_id = c.exec_id
        WHERE e.sec_type = 'STK'
          AND e.symbol = ?
          AND date(e.trade_time) >= date(?, ?)
          AND date(e.trade_time) <= date(?, ?)
    """
    params: list = [
        symbol,
        target_date, f'-{days_window} days',
        target_date, f'+{days_window} days',
    ]
    
    if account:
        query += " AND e.account = ?"
        params.append(account)
    
    query += " ORDER BY e.trade_time"
    
    cursor = conn.cursor()
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def mark_execution_reconciled(
    conn: sqlite3.Connection,
    exec_id: str,
    account: str,
    option_id: Optional[int] = None,
    position_id: Optional[int] = None,
) -> bool:
    """Mark an execution as reconciled with an option or position.
    
    Args:
        conn: Database connection
        exec_id: Execution ID
        account: Account
        option_id: Optional linked option ID
        position_id: Optional linked position ID
        
    Returns:
        True if updated, False otherwise
    """
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ibkr_executions
            SET reconciled = 1,
                reconciled_option_id = ?,
                reconciled_position_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE exec_id = ? AND account = ?
        """, (option_id, position_id, exec_id, account))
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to mark execution reconciled: {e}")
        return False

