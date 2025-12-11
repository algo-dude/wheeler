"""Database operations for syncing IBKR positions to Wheeler SQLite."""
import sqlite3
from datetime import date, datetime
from typing import Optional
import logging

from config import settings

logger = logging.getLogger(__name__)


def get_db_connection():
    """Get a connection to the Wheeler SQLite database."""
    conn = sqlite3.connect(settings.database_path)
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
    opened: Optional[date] = None
) -> int:
    """
    Sync an option position from IBKR to Wheeler database.
    
    Returns the ID of the inserted/updated option.
    """
    ensure_symbol_exists(conn, symbol)
    
    opened_date = opened or date.today()
    cursor = conn.cursor()
    
    # Check if option already exists
    cursor.execute(
        """
        SELECT id FROM options 
        WHERE symbol = ? AND type = ? AND strike = ? AND expiration = ? AND closed IS NULL
        """,
        (symbol, option_type, strike, expiration.isoformat())
    )
    existing = cursor.fetchone()
    
    if existing:
        # Update existing position
        cursor.execute(
            """
            UPDATE options 
            SET contracts = ?, premium = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (contracts, premium, existing['id'])
        )
        option_id = existing['id']
        logger.info(f"Updated option {option_id}: {symbol} {option_type} {strike} {expiration}")
    else:
        # Insert new position
        cursor.execute(
            """
            INSERT INTO options (symbol, type, opened, strike, expiration, premium, contracts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, option_type, opened_date.isoformat(), strike, expiration.isoformat(), premium, contracts)
        )
        option_id = cursor.lastrowid
        logger.info(f"Inserted option {option_id}: {symbol} {option_type} {strike} {expiration}")
    
    conn.commit()
    return option_id


def sync_long_position(
    conn: sqlite3.Connection,
    symbol: str,
    shares: int,
    buy_price: float,
    opened: Optional[date] = None
) -> int:
    """
    Sync a stock position from IBKR to Wheeler database.
    
    Returns the ID of the inserted/updated position.
    """
    ensure_symbol_exists(conn, symbol)
    
    opened_date = opened or date.today()
    cursor = conn.cursor()
    
    # Check if there's an open position for this symbol
    cursor.execute(
        """
        SELECT id, shares, buy_price FROM long_positions 
        WHERE symbol = ? AND closed IS NULL
        ORDER BY opened DESC LIMIT 1
        """,
        (symbol,)
    )
    existing = cursor.fetchone()
    
    if existing:
        # Update existing position
        cursor.execute(
            """
            UPDATE long_positions 
            SET shares = ?, buy_price = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (shares, buy_price, existing['id'])
        )
        position_id = existing['id']
        logger.info(f"Updated long position {position_id}: {symbol} {shares} shares @ ${buy_price:.2f}")
    else:
        # Insert new position
        cursor.execute(
            """
            INSERT INTO long_positions (symbol, opened, shares, buy_price)
            VALUES (?, ?, ?, ?)
            """,
            (symbol, opened_date.isoformat(), shares, buy_price)
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
