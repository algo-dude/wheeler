"""Reconciliation service for matching IBKR executions to Wheeler positions."""
import logging
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def reconcile_executions(conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, Any]:
    """
    Reconcile unreconciled ibkr_executions to options/long_positions.
    
    Args:
        conn: SQLite connection
        dry_run: If True, don't write changes
        
    Returns:
        Dict with counts: options_matched, positions_matched, errors
    """
    result = {
        "options_matched": 0,
        "positions_matched": 0,
        "already_reconciled": 0,
        "errors": [],
    }
    
    cursor = conn.cursor()
    
    # Get all unreconciled executions
    cursor.execute("""
        SELECT id, account, exec_id, conid, symbol, sec_type, trade_time, side,
               quantity, price, right, strike, expiration
        FROM ibkr_executions
        WHERE reconciled = 0
        ORDER BY trade_time
    """)
    
    unreconciled = cursor.fetchall()
    
    for row in unreconciled:
        try:
            exec_id = row["id"]
            account = row["account"]
            symbol = row["symbol"]
            sec_type = row["sec_type"]
            trade_time_str = row["trade_time"]
            right_val = row["right"]
            strike = row["strike"]
            expiration = row["expiration"]
            
            # Parse trade_time
            trade_time = _parse_datetime(trade_time_str)
            if trade_time is None:
                result["errors"].append(f"Invalid trade_time for execution {exec_id}")
                continue
            
            if sec_type == "OPT":
                # Try to match to an option
                option_type = "Call" if right_val == "C" else "Put"
                exp_date = _parse_date(expiration)
                
                if exp_date is None or strike is None:
                    result["errors"].append(f"Missing expiration or strike for option execution {exec_id}")
                    continue
                
                option_id = match_execution_to_option(
                    conn, symbol, option_type, strike, exp_date, trade_time, account
                )
                
                if option_id:
                    if not dry_run:
                        update_execution_reconciliation(
                            conn, row["exec_id"], account, option_id=option_id
                        )
                    result["options_matched"] += 1
                    logger.info(f"Matched execution {exec_id} to option {option_id}")
                    
            elif sec_type == "STK":
                # Try to match to a long position
                position_id = match_execution_to_position(
                    conn, symbol, trade_time, account
                )
                
                if position_id:
                    if not dry_run:
                        update_execution_reconciliation(
                            conn, row["exec_id"], account, position_id=position_id
                        )
                    result["positions_matched"] += 1
                    logger.info(f"Matched execution {exec_id} to position {position_id}")
                    
        except Exception as e:
            result["errors"].append(f"Error reconciling execution {row['id']}: {e}")
            logger.error(f"Reconciliation error for execution {row['id']}: {e}")
    
    if not dry_run:
        conn.commit()
    
    return result


def apply_commissions_to_options(conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, Any]:
    """
    Update options.commission from ibkr_commission_reports for reconciled executions.
    
    Args:
        conn: SQLite connection
        dry_run: If True, don't write changes
        
    Returns:
        Dict with counts: options_updated, total_commission_applied
    """
    result = {
        "options_updated": 0,
        "total_commission_applied": 0.0,
        "errors": [],
    }
    
    cursor = conn.cursor()
    
    # Group commission by option_id for reconciled option executions
    cursor.execute("""
        SELECT e.reconciled_option_id, SUM(ABS(COALESCE(c.commission, 0))) as total_commission
        FROM ibkr_executions e
        LEFT JOIN ibkr_commission_reports c ON e.account = c.account AND e.exec_id = c.exec_id
        WHERE e.reconciled = 1 AND e.reconciled_option_id IS NOT NULL
        GROUP BY e.reconciled_option_id
    """)
    
    commission_by_option = cursor.fetchall()
    
    for row in commission_by_option:
        option_id = row["reconciled_option_id"]
        commission = row["total_commission"] or 0.0
        
        if commission > 0:
            try:
                if not dry_run:
                    cursor.execute("""
                        UPDATE options SET commission = ? WHERE id = ?
                    """, (commission, option_id))
                
                result["options_updated"] += 1
                result["total_commission_applied"] += commission
                logger.info(f"Applied commission ${commission:.2f} to option {option_id}")
                
            except Exception as e:
                result["errors"].append(f"Error updating commission for option {option_id}: {e}")
    
    if not dry_run:
        conn.commit()
    
    return result


def apply_fees_to_positions(conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, Any]:
    """
    Update long_positions.fees from ibkr_commission_reports for reconciled executions.
    
    Args:
        conn: SQLite connection
        dry_run: If True, don't write changes
        
    Returns:
        Dict with counts: positions_updated, total_fees_applied
    """
    result = {
        "positions_updated": 0,
        "total_fees_applied": 0.0,
        "errors": [],
    }
    
    cursor = conn.cursor()
    
    # Check if fees column exists
    cursor.execute("PRAGMA table_info(long_positions)")
    columns = [col["name"] for col in cursor.fetchall()]
    if "fees" not in columns:
        result["errors"].append("fees column does not exist in long_positions table")
        return result
    
    # Group commission by position_id for reconciled stock executions
    cursor.execute("""
        SELECT e.reconciled_position_id, SUM(ABS(COALESCE(c.commission, 0))) as total_fees
        FROM ibkr_executions e
        LEFT JOIN ibkr_commission_reports c ON e.account = c.account AND e.exec_id = c.exec_id
        WHERE e.reconciled = 1 AND e.reconciled_position_id IS NOT NULL
        GROUP BY e.reconciled_position_id
    """)
    
    fees_by_position = cursor.fetchall()
    
    for row in fees_by_position:
        position_id = row["reconciled_position_id"]
        fees = row["total_fees"] or 0.0
        
        if fees > 0:
            try:
                if not dry_run:
                    cursor.execute("""
                        UPDATE long_positions SET fees = ? WHERE id = ?
                    """, (fees, position_id))
                
                result["positions_updated"] += 1
                result["total_fees_applied"] += fees
                logger.info(f"Applied fees ${fees:.2f} to position {position_id}")
                
            except Exception as e:
                result["errors"].append(f"Error updating fees for position {position_id}: {e}")
    
    if not dry_run:
        conn.commit()
    
    return result


def detect_assignments(
    conn: sqlite3.Connection,
    lookback_days: int = 7,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Detect assignment/exercise events by analyzing execution patterns.
    
    Heuristics:
    - Option disappears (closed) on same day as stock execution
    - Stock trade at option strike price (within tolerance)
    - PUT assignment: stock BUY at strike (shares assigned to holder)
    - CALL assignment: stock SELL at strike (shares called away)
    
    Args:
        conn: SQLite connection
        lookback_days: How far back to look for patterns
        dry_run: If True, don't write activities
        
    Returns:
        Dict with counts: assignments_detected, exercises_detected, activities_created
    """
    result = {
        "assignments_detected": 0,
        "exercises_detected": 0,
        "activities_created": 0,
        "errors": [],
    }
    
    cursor = conn.cursor()
    
    # Check if ibkr_activities table exists
    cursor.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='ibkr_activities' LIMIT 1
    """)
    if not cursor.fetchone():
        result["errors"].append("ibkr_activities table does not exist")
        return result
    
    cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    
    # Find recently closed options that might have been assigned
    cursor.execute("""
        SELECT id, symbol, type, strike, expiration, closed, contracts, account
        FROM options
        WHERE closed IS NOT NULL AND closed >= ?
        ORDER BY closed
    """, (cutoff_date,))
    
    closed_options = cursor.fetchall()
    
    for opt in closed_options:
        try:
            option_id = opt["id"]
            symbol = opt["symbol"]
            option_type = opt["type"]  # 'Put' or 'Call'
            strike = opt["strike"]
            expiration_str = opt["expiration"]
            closed_str = opt["closed"]
            contracts = abs(opt["contracts"] or 1)
            account = opt["account"]
            
            closed_date = _parse_date(closed_str)
            if closed_date is None:
                continue
            
            expiration_date = _parse_date(expiration_str)
            
            # Look for stock executions around the close date
            window_start = (closed_date - timedelta(days=1)).strftime("%Y-%m-%d")
            window_end = (closed_date + timedelta(days=1)).strftime("%Y-%m-%d")
            
            # Determine expected execution side based on option type
            # PUT assignment -> trader BUYS stock at strike
            # CALL assignment -> trader SELLS stock at strike
            expected_side = "BUY" if option_type == "Put" else "SELL"
            
            cursor.execute("""
                SELECT id, account, exec_id, trade_time, side, quantity, price
                FROM ibkr_executions
                WHERE symbol = ? AND sec_type = 'STK'
                AND side = ?
                AND DATE(trade_time) BETWEEN ? AND ?
                AND (account = ? OR ? IS NULL)
            """, (symbol, expected_side, window_start, window_end, account, account))
            
            stock_executions = cursor.fetchall()
            
            for stock_exec in stock_executions:
                exec_price = stock_exec["price"]
                exec_quantity = abs(stock_exec["quantity"] or 0)
                expected_shares = contracts * 100
                
                # Check if price is at or near strike (within 2% tolerance)
                price_tolerance = strike * 0.02
                if abs(exec_price - strike) <= price_tolerance:
                    # Check if quantity matches (within reason)
                    if exec_quantity >= expected_shares * 0.9:
                        # Looks like an assignment!
                        trade_time = _parse_datetime(stock_exec["trade_time"])
                        
                        # Determine if this is an assignment or exercise
                        # Short options get assigned, long options get exercised
                        # Contracts < 0 means short (sold) option
                        is_short_option = (opt["contracts"] or 0) < 0
                        activity_type = "ASSIGNMENT" if is_short_option else "EXERCISE"
                        
                        # Check if we already recorded this activity
                        activity_id = f"{activity_type}_{option_id}_{closed_str}"
                        cursor.execute("""
                            SELECT 1 FROM ibkr_activities 
                            WHERE activity_id = ? LIMIT 1
                        """, (activity_id,))
                        
                        if cursor.fetchone():
                            # Already recorded
                            continue
                        
                        if not dry_run:
                            create_assignment_activity(
                                conn,
                                account=account or stock_exec["account"],
                                symbol=symbol,
                                option_type=option_type,
                                strike=strike,
                                expiration=expiration_date,
                                quantity=contracts,
                                event_time=trade_time or datetime.now(),
                                option_id=option_id,
                                position_id=None,  # Could link to position if needed
                                dry_run=False
                            )
                            result["activities_created"] += 1
                        
                        if activity_type == "ASSIGNMENT":
                            result["assignments_detected"] += 1
                        else:
                            result["exercises_detected"] += 1
                        
                        logger.info(
                            f"Detected {activity_type}: {symbol} {option_type} ${strike} "
                            f"closed on {closed_str}, stock {expected_side} @ ${exec_price:.2f}"
                        )
                        break  # One assignment per closed option
                        
        except Exception as e:
            result["errors"].append(f"Error detecting assignment for option {opt['id']}: {e}")
            logger.error(f"Assignment detection error for option {opt['id']}: {e}")
    
    if not dry_run:
        conn.commit()
    
    return result


def create_assignment_activity(
    conn: sqlite3.Connection,
    account: str,
    symbol: str,
    option_type: str,  # 'Put' or 'Call'
    strike: float,
    expiration: Optional[date],
    quantity: int,
    event_time: datetime,
    option_id: Optional[int] = None,
    position_id: Optional[int] = None,
    dry_run: bool = False
) -> Optional[int]:
    """
    Create an ibkr_activities record for an assignment event.
    
    Args:
        conn: SQLite connection
        account: IBKR account ID
        symbol: Underlying symbol
        option_type: 'Put' or 'Call'
        strike: Option strike price
        expiration: Option expiration date
        quantity: Number of contracts assigned
        event_time: When the assignment occurred
        option_id: Link to options.id if known
        position_id: Link to long_positions.id if known
        dry_run: If True, don't write
        
    Returns:
        Activity ID if created, None if dry_run
    """
    if dry_run:
        return None
    
    cursor = conn.cursor()
    
    # Determine activity type based on option type
    # Short PUT assignment = shares assigned TO holder
    # Short CALL assignment = shares called AWAY from holder
    activity_type = "ASSIGNMENT"
    
    exp_str = expiration.isoformat() if expiration else "unknown"
    activity_id = f"ASSIGNMENT_{option_id or 'manual'}_{event_time.strftime('%Y%m%d')}"
    
    description = (
        f"{option_type.upper()} assignment: {symbol} ${strike} exp {exp_str}, "
        f"{quantity} contract(s)"
    )
    
    # Calculate shares amount
    shares = quantity * 100
    amount = shares * strike  # Notional value
    
    try:
        cursor.execute("""
            INSERT INTO ibkr_activities (
                account, activity_id, activity_type, conid, symbol,
                event_time, quantity, amount, currency, description,
                source, linked_option_id, linked_position_id
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 'USD', ?, 'INFERRED', ?, ?)
        """, (
            account,
            activity_id,
            activity_type,
            symbol,
            event_time.strftime("%Y-%m-%d %H:%M:%S"),
            shares,
            amount,
            description,
            option_id,
            position_id,
        ))
        
        conn.commit()
        return cursor.lastrowid
        
    except sqlite3.IntegrityError:
        # Activity already exists
        logger.debug(f"Activity {activity_id} already exists")
        return None


def get_reconciliation_status(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Get counts of reconciled vs unreconciled executions.
    
    Returns:
        Dict with: total_executions, reconciled_count, unreconciled_count,
                   assignments_detected, total_commissions
    """
    cursor = conn.cursor()
    
    result = {
        "total_executions": 0,
        "reconciled_count": 0,
        "unreconciled_count": 0,
        "assignments_detected": 0,
        "total_commissions": 0.0,
    }
    
    # Check if tables exist
    cursor.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='ibkr_executions' LIMIT 1
    """)
    if not cursor.fetchone():
        return result
    
    # Total executions
    cursor.execute("SELECT COUNT(*) FROM ibkr_executions")
    result["total_executions"] = cursor.fetchone()[0]
    
    # Reconciled vs unreconciled
    cursor.execute("SELECT COUNT(*) FROM ibkr_executions WHERE reconciled = 1")
    result["reconciled_count"] = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM ibkr_executions WHERE reconciled = 0")
    result["unreconciled_count"] = cursor.fetchone()[0]
    
    # Assignments detected
    cursor.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='ibkr_activities' LIMIT 1
    """)
    if cursor.fetchone():
        cursor.execute("""
            SELECT COUNT(*) FROM ibkr_activities 
            WHERE activity_type IN ('ASSIGNMENT', 'EXERCISE')
        """)
        result["assignments_detected"] = cursor.fetchone()[0]
    
    # Total commissions
    cursor.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='ibkr_commission_reports' LIMIT 1
    """)
    if cursor.fetchone():
        cursor.execute("""
            SELECT COALESCE(SUM(ABS(commission)), 0) FROM ibkr_commission_reports
        """)
        result["total_commissions"] = cursor.fetchone()[0] or 0.0
    
    return result


def recompute_adjusted_cost_basis(conn: sqlite3.Connection, symbol: str) -> Dict[str, Any]:
    """
    Recompute adjusted cost basis for a symbol considering assignments.
    
    This is a placeholder - the actual cost basis logic is in Go.
    This function could be extended to update specific fields.
    
    Args:
        conn: SQLite connection
        symbol: Symbol to recompute
        
    Returns:
        Dict with status info
    """
    result = {
        "symbol": symbol,
        "status": "not_implemented",
        "message": "Cost basis recomputation is handled by the Go service"
    }
    
    # For now, just return status - the Go side handles cost basis
    # This could be extended to set flags or trigger recalculation
    
    return result


# Helper functions

def match_execution_to_option(
    conn: sqlite3.Connection,
    symbol: str,
    option_type: str,  # 'Call' or 'Put'
    strike: float,
    expiration: date,
    trade_time: datetime,
    account: Optional[str] = None,
    tolerance_days: int = 1
) -> Optional[int]:
    """
    Find matching option ID for an execution.
    
    Matching criteria:
    - Same symbol, type, strike, expiration
    - opened date within tolerance_days of trade_time
    - Same account (if specified)
    
    Returns:
        options.id if found, None otherwise
    """
    cursor = conn.cursor()
    
    trade_date = trade_time.date()
    date_min = (trade_date - timedelta(days=tolerance_days)).isoformat()
    date_max = (trade_date + timedelta(days=tolerance_days)).isoformat()
    exp_str = expiration.isoformat()
    
    # Try to find matching option
    cursor.execute("""
        SELECT id FROM options
        WHERE symbol = ? AND type = ? AND strike = ? AND expiration = ?
        AND opened BETWEEN ? AND ?
        AND (account = ? OR account IS NULL OR ? IS NULL)
        ORDER BY 
            CASE WHEN account = ? THEN 0 ELSE 1 END,
            ABS(JULIANDAY(opened) - JULIANDAY(?))
        LIMIT 1
    """, (symbol, option_type, strike, exp_str, date_min, date_max, account, account, account, trade_date.isoformat()))
    
    row = cursor.fetchone()
    if row:
        return row["id"]
    
    # Try with wider tolerance (any matching option)
    cursor.execute("""
        SELECT id FROM options
        WHERE symbol = ? AND type = ? AND strike = ? AND expiration = ?
        AND (account = ? OR account IS NULL OR ? IS NULL)
        ORDER BY 
            CASE WHEN account = ? THEN 0 ELSE 1 END,
            ABS(JULIANDAY(opened) - JULIANDAY(?))
        LIMIT 1
    """, (symbol, option_type, strike, exp_str, account, account, account, trade_date.isoformat()))
    
    row = cursor.fetchone()
    return row["id"] if row else None


def match_execution_to_position(
    conn: sqlite3.Connection,
    symbol: str,
    trade_time: datetime,
    account: Optional[str] = None,
    tolerance_days: int = 7
) -> Optional[int]:
    """
    Find matching long_position ID for a stock execution.
    
    Matching criteria:
    - Same symbol
    - opened date within tolerance_days of trade_time
    - Same account (if specified)
    
    Returns:
        long_positions.id if found, None otherwise
    """
    cursor = conn.cursor()
    
    trade_date = trade_time.date()
    date_min = (trade_date - timedelta(days=tolerance_days)).isoformat()
    date_max = (trade_date + timedelta(days=tolerance_days)).isoformat()
    
    # Try to find matching position
    cursor.execute("""
        SELECT id FROM long_positions
        WHERE symbol = ?
        AND opened BETWEEN ? AND ?
        AND (account = ? OR account IS NULL OR ? IS NULL)
        ORDER BY 
            CASE WHEN account = ? THEN 0 ELSE 1 END,
            ABS(JULIANDAY(opened) - JULIANDAY(?))
        LIMIT 1
    """, (symbol, date_min, date_max, account, account, account, trade_date.isoformat()))
    
    row = cursor.fetchone()
    if row:
        return row["id"]
    
    # Try with any open position for this symbol
    cursor.execute("""
        SELECT id FROM long_positions
        WHERE symbol = ? AND closed IS NULL
        AND (account = ? OR account IS NULL OR ? IS NULL)
        ORDER BY 
            CASE WHEN account = ? THEN 0 ELSE 1 END,
            opened DESC
        LIMIT 1
    """, (symbol, account, account, account))
    
    row = cursor.fetchone()
    return row["id"] if row else None


def update_execution_reconciliation(
    conn: sqlite3.Connection,
    exec_id: str,
    account: str,
    option_id: Optional[int] = None,
    position_id: Optional[int] = None
) -> None:
    """
    Mark an execution as reconciled and link to position.
    """
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE ibkr_executions
        SET reconciled = 1,
            reconciled_option_id = COALESCE(?, reconciled_option_id),
            reconciled_position_id = COALESCE(?, reconciled_position_id),
            updated_at = CURRENT_TIMESTAMP
        WHERE exec_id = ? AND account = ?
    """, (option_id, position_id, exec_id, account))
    conn.commit()


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse datetime from various formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    
    s = str(value).strip()
    if not s:
        return None
    
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s[:len(fmt.replace('%', '').replace('Y', 'YYYY').replace('m', 'MM').replace('d', 'DD').replace('H', 'HH').replace('M', 'MM').replace('S', 'SS'))], fmt)
        except ValueError:
            continue
    
    # Try ISO format
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").split("+")[0])
    except ValueError:
        return None


def _parse_date(value: Optional[str]) -> Optional[date]:
    """Parse date from various formats."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    
    s = str(value).strip()
    if not s:
        return None
    
    # Take just the date part if datetime
    s = s.split("T")[0].split(" ")[0]
    
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    
    # Try YYYYMMDD format
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            pass
    
    return None
