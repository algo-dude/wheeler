"""Transaction sync service for IBKR execution and activity ingestion.

This module handles:
- Syncing execution records from IBKR to ibkr_executions table
- Syncing commission reports to ibkr_commission_reports table
- Detecting assignment/exercise/expiration events
- Recomputing cost basis for long positions based on assignments
- Updating fees on options from commission reports
"""
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional, Any

from database import (
    get_db_connection,
    resolve_database_path,
)

logger = logging.getLogger(__name__)


class TransactionSyncService:
    """Service for syncing IBKR transactions and computing derived data."""

    def __init__(self, db_path: Optional[str] = None):
        """Initialize the transaction sync service.
        
        Args:
            db_path: Optional path to SQLite database. If None, uses resolve_database_path().
        """
        self.db_path = db_path or resolve_database_path()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -------------------------------------------------------------------------
    # Activity Detection
    # -------------------------------------------------------------------------

    def detect_assignments_exercises(self, account: Optional[str] = None) -> list[dict]:
        """Detect assignment/exercise patterns from executions and position changes.
        
        This analyzes the ibkr_executions table to detect:
        - Assignments: Short PUT option was assigned, resulting in stock purchase
        - Exercises: Long CALL option was exercised, resulting in stock purchase
        - Expirations: Option expired worthless (position went to 0)
        
        Detection logic:
        1. Find option executions where position closed (sell to close for short, buy to close for long)
        2. Look for stock executions on same day at the option strike price
        3. For ITM options at expiration, infer assignment/exercise
        
        Args:
            account: Optional account filter
            
        Returns:
            List of detected activity events with activity details
        """
        conn = self._get_connection()
        detected_events: list[dict] = []
        
        try:
            cursor = conn.cursor()
            
            # Find potential assignment patterns:
            # A PUT assignment = stock BUY execution at the strike price
            # Look for stock BUYs that might correspond to option assignments
            
            query = """
                SELECT 
                    e.id, e.account, e.exec_id, e.conid, e.symbol, e.sec_type,
                    e.trade_time, e.side, e.quantity, e.price,
                    e.right, e.strike, e.expiration, e.multiplier,
                    e.reconciled, e.raw_json
                FROM ibkr_executions e
                WHERE e.sec_type = 'STK'
                  AND e.reconciled = 0
            """
            params: list[Any] = []
            
            if account:
                query += " AND e.account = ?"
                params.append(account)
            
            query += " ORDER BY e.trade_time DESC"
            
            cursor.execute(query, params)
            stock_executions = cursor.fetchall()
            
            for stock_exec in stock_executions:
                # For each stock execution, check if there's a matching option
                # that could indicate an assignment or exercise
                trade_date = stock_exec['trade_time'][:10] if stock_exec['trade_time'] else None
                if not trade_date:
                    continue
                
                symbol = stock_exec['symbol']
                price = stock_exec['price']
                quantity = stock_exec['quantity']
                side = stock_exec['side']
                
                # Look for options that expired around this time with matching strike
                # An assignment from a short PUT would result in a stock BUY
                # An exercise of a long CALL would result in a stock BUY
                
                if side == 'BUY':
                    # Could be assignment of short PUT or exercise of long CALL
                    # Check for PUT options with expiration near trade_date and strike ~ price
                    option_query = """
                        SELECT o.id, o.symbol, o.type, o.strike, o.expiration, 
                               o.premium, o.contracts, o.account
                        FROM options o
                        WHERE o.symbol = ?
                          AND o.type = 'Put'
                          AND o.strike = ?
                          AND o.expiration <= ?
                          AND o.expiration >= date(?, '-7 days')
                          AND o.closed IS NOT NULL
                    """
                    option_params = [symbol, price, trade_date, trade_date]
                    
                    if account:
                        option_query += " AND (o.account = ? OR o.account IS NULL)"
                        option_params.append(account)
                    
                    cursor.execute(option_query, option_params)
                    matching_options = cursor.fetchall()
                    
                    for opt in matching_options:
                        # Found a potential assignment
                        event = {
                            'account': stock_exec['account'],
                            'activity_type': 'ASSIGNMENT',
                            'symbol': symbol,
                            'event_time': stock_exec['trade_time'],
                            'quantity': quantity,
                            'amount': price * quantity,
                            'currency': 'USD',
                            'description': f"PUT assignment: {symbol} {opt['strike']} @ {opt['expiration']}",
                            'related_exec_id': stock_exec['exec_id'],
                            'option_id': opt['id'],
                            'option_strike': opt['strike'],
                            'option_expiration': opt['expiration'],
                            'option_premium': opt['premium'],
                            'option_contracts': opt['contracts'],
                        }
                        detected_events.append(event)
                
                elif side == 'SELL':
                    # Could be assignment of short CALL or exercise of long PUT
                    # Check for CALL options with expiration near trade_date and strike ~ price
                    option_query = """
                        SELECT o.id, o.symbol, o.type, o.strike, o.expiration,
                               o.premium, o.contracts, o.account
                        FROM options o
                        WHERE o.symbol = ?
                          AND o.type = 'Call'
                          AND o.strike = ?
                          AND o.expiration <= ?
                          AND o.expiration >= date(?, '-7 days')
                          AND o.closed IS NOT NULL
                    """
                    option_params = [symbol, price, trade_date, trade_date]
                    
                    if account:
                        option_query += " AND (o.account = ? OR o.account IS NULL)"
                        option_params.append(account)
                    
                    cursor.execute(option_query, option_params)
                    matching_options = cursor.fetchall()
                    
                    for opt in matching_options:
                        event = {
                            'account': stock_exec['account'],
                            'activity_type': 'ASSIGNMENT',
                            'symbol': symbol,
                            'event_time': stock_exec['trade_time'],
                            'quantity': quantity,
                            'amount': price * quantity,
                            'currency': 'USD',
                            'description': f"CALL assignment: {symbol} {opt['strike']} @ {opt['expiration']}",
                            'related_exec_id': stock_exec['exec_id'],
                            'option_id': opt['id'],
                            'option_strike': opt['strike'],
                            'option_expiration': opt['expiration'],
                            'option_premium': opt['premium'],
                            'option_contracts': opt['contracts'],
                        }
                        detected_events.append(event)
            
            # Insert detected events into ibkr_activities
            for event in detected_events:
                self._insert_activity(conn, event)
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"Error detecting assignments/exercises: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
        
        return detected_events

    def _insert_activity(self, conn: sqlite3.Connection, event: dict) -> Optional[int]:
        """Insert an activity event into ibkr_activities.
        
        Uses INSERT OR IGNORE to avoid duplicates based on (account, activity_id).
        
        Returns:
            The inserted row ID, or None if skipped due to conflict
        """
        cursor = conn.cursor()
        
        # Generate a unique activity_id from the event details
        activity_id = f"{event.get('activity_type', 'UNKNOWN')}_{event.get('symbol', '')}_{event.get('event_time', '')}_{event.get('related_exec_id', '')}"
        
        try:
            cursor.execute("""
                INSERT INTO ibkr_activities (
                    account, activity_id, activity_type, symbol, event_time,
                    quantity, amount, currency, description, related_exec_id,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, activity_id) DO UPDATE SET
                    activity_type = excluded.activity_type,
                    symbol = excluded.symbol,
                    event_time = excluded.event_time,
                    quantity = excluded.quantity,
                    amount = excluded.amount,
                    currency = excluded.currency,
                    description = excluded.description,
                    related_exec_id = excluded.related_exec_id,
                    raw_json = excluded.raw_json
            """, (
                event.get('account'),
                activity_id,
                event.get('activity_type'),
                event.get('symbol'),
                event.get('event_time'),
                event.get('quantity'),
                event.get('amount'),
                event.get('currency'),
                event.get('description'),
                event.get('related_exec_id'),
                None,  # raw_json - can be populated if needed
            ))
            return cursor.lastrowid
        except Exception as e:
            logger.warning(f"Failed to insert activity: {e}")
            return None

    # -------------------------------------------------------------------------
    # Cost Basis Recomputation
    # -------------------------------------------------------------------------

    def recompute_cost_basis(self, account: Optional[str] = None, symbol: Optional[str] = None) -> dict:
        """Recompute adjusted cost basis for long positions based on assignments.
        
        For positions acquired via PUT assignment:
        - adjusted_cost_basis_per_share = strike_price - premium_received_per_share
        - adjusted_cost_basis_total = per_share * shares
        
        Args:
            account: Optional account filter
            symbol: Optional symbol filter. If None, recomputes all positions.
            
        Returns:
            Summary dict with positions_updated count and details
        """
        conn = self._get_connection()
        result = {
            'positions_updated': 0,
            'details': [],
            'errors': [],
        }
        
        try:
            cursor = conn.cursor()
            
            # Find assignment activities
            query = """
                SELECT 
                    a.id, a.account, a.symbol, a.event_time, a.quantity, a.amount,
                    a.description, a.related_exec_id
                FROM ibkr_activities a
                WHERE a.activity_type = 'ASSIGNMENT'
            """
            params: list[Any] = []
            
            if account:
                query += " AND a.account = ?"
                params.append(account)
            
            if symbol:
                query += " AND a.symbol = ?"
                params.append(symbol)
            
            query += " ORDER BY a.event_time DESC"
            
            cursor.execute(query, params)
            assignments = cursor.fetchall()
            
            for assignment in assignments:
                try:
                    sym = assignment['symbol']
                    event_time = assignment['event_time']
                    event_date = event_time[:10] if event_time else None
                    qty = assignment['quantity'] or 0
                    description = assignment['description'] or ''
                    acct = assignment['account']
                    
                    # Parse strike from description or find the matching option
                    # Description format: "PUT assignment: SYMBOL STRIKE @ EXPIRATION"
                    strike = None
                    premium_per_share = 0.0
                    
                    # Try to find the original option to get premium
                    # Look for closed PUT options with matching symbol around the event time
                    opt_query = """
                        SELECT o.id, o.strike, o.premium, o.contracts, o.expiration
                        FROM options o
                        WHERE o.symbol = ?
                          AND o.type = 'Put'
                          AND o.closed IS NOT NULL
                          AND o.expiration <= ?
                          AND o.expiration >= date(?, '-7 days')
                    """
                    opt_params = [sym, event_date, event_date]
                    
                    if acct:
                        opt_query += " AND (o.account = ? OR o.account IS NULL)"
                        opt_params.append(acct)
                    
                    opt_query += " ORDER BY o.expiration DESC LIMIT 1"
                    
                    cursor.execute(opt_query, opt_params)
                    opt_row = cursor.fetchone()
                    
                    if opt_row:
                        strike = opt_row['strike']
                        # Premium is per contract, need to convert to per share
                        # Premium in DB is typically total per contract
                        contracts = opt_row['contracts'] or 1
                        total_premium = opt_row['premium'] * abs(contracts)
                        # For a PUT, 1 contract = 100 shares typically
                        multiplier = 100
                        total_shares = abs(contracts) * multiplier
                        premium_per_share = total_premium / total_shares if total_shares > 0 else 0
                    
                    if strike is None:
                        # Try to extract from amount / quantity
                        if qty and assignment['amount']:
                            strike = assignment['amount'] / qty
                    
                    if strike is None:
                        result['errors'].append(f"Could not determine strike for {sym} assignment on {event_date}")
                        continue
                    
                    # Calculate adjusted cost basis
                    adjusted_per_share = strike - premium_per_share
                    
                    # Find the long position to update
                    # Match by symbol and opened date close to assignment date
                    pos_query = """
                        SELECT id, shares, buy_price, opened
                        FROM long_positions
                        WHERE symbol = ?
                          AND opened <= ?
                          AND opened >= date(?, '-7 days')
                          AND closed IS NULL
                    """
                    pos_params = [sym, event_date, event_date]
                    
                    if acct:
                        pos_query += " AND (account = ? OR account IS NULL)"
                        pos_params.append(acct)
                    
                    pos_query += " ORDER BY opened DESC LIMIT 1"
                    
                    cursor.execute(pos_query, pos_params)
                    pos_row = cursor.fetchone()
                    
                    if pos_row:
                        position_id = pos_row['id']
                        shares = pos_row['shares']
                        adjusted_total = adjusted_per_share * shares
                        
                        # Update the position
                        cursor.execute("""
                            UPDATE long_positions
                            SET adjusted_cost_basis_per_share = ?,
                                adjusted_cost_basis_total = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (adjusted_per_share, adjusted_total, position_id))
                        
                        result['positions_updated'] += 1
                        result['details'].append({
                            'position_id': position_id,
                            'symbol': sym,
                            'strike': strike,
                            'premium_per_share': premium_per_share,
                            'adjusted_per_share': adjusted_per_share,
                            'adjusted_total': adjusted_total,
                            'shares': shares,
                        })
                    else:
                        result['errors'].append(f"No matching long position found for {sym} assignment on {event_date}")
                
                except Exception as e:
                    result['errors'].append(f"Error processing assignment for {assignment.get('symbol')}: {e}")
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"Error recomputing cost basis: {e}")
            conn.rollback()
            result['errors'].append(str(e))
        finally:
            conn.close()
        
        return result

    # -------------------------------------------------------------------------
    # Fee Updates
    # -------------------------------------------------------------------------

    def update_option_fees(self, account: Optional[str] = None) -> dict:
        """Sum commissions from ibkr_commission_reports and update options.commission field.
        
        Links executions to options via symbol, strike, expiration matching.
        
        Args:
            account: Optional account filter
            
        Returns:
            Summary dict with options_updated count
        """
        conn = self._get_connection()
        result = {
            'options_updated': 0,
            'total_fees_applied': 0.0,
            'details': [],
            'errors': [],
        }
        
        try:
            cursor = conn.cursor()
            
            # Get all option executions with their commissions
            query = """
                SELECT 
                    e.account, e.exec_id, e.symbol, e.sec_type,
                    e.right, e.strike, e.expiration, e.trade_time,
                    c.commission
                FROM ibkr_executions e
                JOIN ibkr_commission_reports c 
                    ON e.account = c.account AND e.exec_id = c.exec_id
                WHERE e.sec_type = 'OPT'
                  AND c.commission IS NOT NULL
                  AND c.commission > 0
            """
            params: list[Any] = []
            
            if account:
                query += " AND e.account = ?"
                params.append(account)
            
            cursor.execute(query, params)
            executions = cursor.fetchall()
            
            # Group commissions by option (symbol, type, strike, expiration)
            option_fees: dict[tuple, float] = {}
            
            for exec_row in executions:
                # Convert right to type
                right = exec_row['right']
                opt_type = 'Call' if right == 'C' else 'Put' if right == 'P' else None
                if not opt_type:
                    continue
                
                # Normalize expiration format
                exp = exec_row['expiration']
                if exp and len(exp) == 10:  # YYYY-MM-DD
                    exp_key = exp
                elif exp and len(exp) == 8:  # YYYYMMDD
                    exp_key = f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"
                else:
                    exp_key = exp
                
                key = (exec_row['symbol'], opt_type, exec_row['strike'], exp_key, exec_row['account'])
                commission = exec_row['commission'] or 0.0
                
                if key in option_fees:
                    option_fees[key] += commission
                else:
                    option_fees[key] = commission
            
            # Update options table
            for (symbol, opt_type, strike, expiration, acct), total_commission in option_fees.items():
                try:
                    # Find the matching option
                    opt_query = """
                        SELECT id, commission
                        FROM options
                        WHERE symbol = ?
                          AND type = ?
                          AND strike = ?
                          AND expiration = ?
                    """
                    opt_params: list[Any] = [symbol, opt_type, strike, expiration]
                    
                    if acct:
                        opt_query += " AND (account = ? OR account IS NULL)"
                        opt_params.append(acct)
                    
                    opt_query += " ORDER BY id DESC LIMIT 1"
                    
                    cursor.execute(opt_query, opt_params)
                    opt_row = cursor.fetchone()
                    
                    if opt_row:
                        # Update the commission/fees field
                        cursor.execute("""
                            UPDATE options
                            SET commission = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (total_commission, opt_row['id']))
                        
                        result['options_updated'] += 1
                        result['total_fees_applied'] += total_commission
                        result['details'].append({
                            'option_id': opt_row['id'],
                            'symbol': symbol,
                            'type': opt_type,
                            'strike': strike,
                            'expiration': expiration,
                            'commission': total_commission,
                        })
                    else:
                        result['errors'].append(
                            f"No matching option found for {symbol} {opt_type} {strike} {expiration}"
                        )
                
                except Exception as e:
                    result['errors'].append(f"Error updating fees for {symbol}: {e}")
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"Error updating option fees: {e}")
            conn.rollback()
            result['errors'].append(str(e))
        finally:
            conn.close()
        
        return result

    def update_position_fees(self, account: Optional[str] = None) -> dict:
        """Sum commissions from ibkr_commission_reports and update long_positions.fees field.
        
        Args:
            account: Optional account filter
            
        Returns:
            Summary dict with positions_updated count
        """
        conn = self._get_connection()
        result = {
            'positions_updated': 0,
            'total_fees_applied': 0.0,
            'details': [],
            'errors': [],
        }
        
        try:
            cursor = conn.cursor()
            
            # Get all stock executions with their commissions
            query = """
                SELECT 
                    e.account, e.exec_id, e.symbol, e.trade_time,
                    c.commission
                FROM ibkr_executions e
                JOIN ibkr_commission_reports c 
                    ON e.account = c.account AND e.exec_id = c.exec_id
                WHERE e.sec_type = 'STK'
                  AND c.commission IS NOT NULL
                  AND c.commission > 0
            """
            params: list[Any] = []
            
            if account:
                query += " AND e.account = ?"
                params.append(account)
            
            cursor.execute(query, params)
            executions = cursor.fetchall()
            
            # Group commissions by position (symbol, account)
            position_fees: dict[tuple, float] = {}
            
            for exec_row in executions:
                key = (exec_row['symbol'], exec_row['account'])
                commission = exec_row['commission'] or 0.0
                
                if key in position_fees:
                    position_fees[key] += commission
                else:
                    position_fees[key] = commission
            
            # Update long_positions table
            for (symbol, acct), total_commission in position_fees.items():
                try:
                    # Find the matching position (most recent open one)
                    pos_query = """
                        SELECT id, fees
                        FROM long_positions
                        WHERE symbol = ?
                          AND closed IS NULL
                    """
                    pos_params: list[Any] = [symbol]
                    
                    if acct:
                        pos_query += " AND (account = ? OR account IS NULL)"
                        pos_params.append(acct)
                    
                    pos_query += " ORDER BY opened DESC LIMIT 1"
                    
                    cursor.execute(pos_query, pos_params)
                    pos_row = cursor.fetchone()
                    
                    if pos_row:
                        cursor.execute("""
                            UPDATE long_positions
                            SET fees = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (total_commission, pos_row['id']))
                        
                        result['positions_updated'] += 1
                        result['total_fees_applied'] += total_commission
                        result['details'].append({
                            'position_id': pos_row['id'],
                            'symbol': symbol,
                            'fees': total_commission,
                        })
                
                except Exception as e:
                    result['errors'].append(f"Error updating fees for position {symbol}: {e}")
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"Error updating position fees: {e}")
            conn.rollback()
            result['errors'].append(str(e))
        finally:
            conn.close()
        
        return result

    # -------------------------------------------------------------------------
    # Transaction Status
    # -------------------------------------------------------------------------

    def get_transaction_status(self) -> dict:
        """Get current transaction sync status from database.
        
        Returns:
            Status dict with counts for executions, commissions, activities
        """
        conn = self._get_connection()
        status: dict[str, Any] = {
            'execution_count': 0,
            'commission_count': 0,
            'activity_count': 0,
            'last_execution_time': None,
            'last_activity_time': None,
        }
        
        try:
            cursor = conn.cursor()
            
            # Count executions
            cursor.execute("SELECT COUNT(*) FROM ibkr_executions")
            status['execution_count'] = cursor.fetchone()[0]
            
            # Get latest execution time
            cursor.execute("SELECT MAX(trade_time) FROM ibkr_executions")
            row = cursor.fetchone()
            status['last_execution_time'] = row[0] if row else None
            
            # Count commission reports
            cursor.execute("SELECT COUNT(*) FROM ibkr_commission_reports")
            status['commission_count'] = cursor.fetchone()[0]
            
            # Count activities
            cursor.execute("SELECT COUNT(*) FROM ibkr_activities")
            status['activity_count'] = cursor.fetchone()[0]
            
            # Get latest activity time
            cursor.execute("SELECT MAX(event_time) FROM ibkr_activities")
            row = cursor.fetchone()
            status['last_activity_time'] = row[0] if row else None
            
            # Count by activity type
            cursor.execute("""
                SELECT activity_type, COUNT(*) as count
                FROM ibkr_activities
                GROUP BY activity_type
            """)
            status['activity_breakdown'] = {
                row['activity_type']: row['count'] for row in cursor.fetchall()
            }
            
        except Exception as e:
            logger.error(f"Error getting transaction status: {e}")
            status['error'] = str(e)
        finally:
            conn.close()
        
        return status


# -------------------------------------------------------------------------
# Database Helper Functions
# -------------------------------------------------------------------------

def insert_activity(db_path: str, activity_data: dict) -> Optional[int]:
    """Insert an activity event into ibkr_activities.
    
    Args:
        db_path: Path to SQLite database
        activity_data: Dict with activity fields
        
    Returns:
        Inserted row ID, or None if failed
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    try:
        cursor = conn.cursor()
        
        activity_id = activity_data.get('activity_id') or \
            f"{activity_data.get('activity_type', 'UNKNOWN')}_{activity_data.get('symbol', '')}_{activity_data.get('event_time', '')}"
        
        cursor.execute("""
            INSERT INTO ibkr_activities (
                account, activity_id, activity_type, conid, symbol, event_time,
                quantity, amount, currency, description, raw_text, raw_json,
                related_exec_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                related_exec_id = excluded.related_exec_id
        """, (
            activity_data.get('account'),
            activity_id,
            activity_data.get('activity_type'),
            activity_data.get('conid'),
            activity_data.get('symbol'),
            activity_data.get('event_time'),
            activity_data.get('quantity'),
            activity_data.get('amount'),
            activity_data.get('currency'),
            activity_data.get('description'),
            activity_data.get('raw_text'),
            activity_data.get('raw_json'),
            activity_data.get('related_exec_id'),
        ))
        
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to insert activity: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def get_unlinked_assignments(db_path: str, account: Optional[str] = None) -> list[dict]:
    """Find stock executions that might be assignments but aren't linked to activities.
    
    Args:
        db_path: Path to SQLite database
        account: Optional account filter
        
    Returns:
        List of execution dicts that may be unlinked assignments
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    try:
        cursor = conn.cursor()
        
        # Find stock BUY executions that aren't linked to any activity
        query = """
            SELECT e.*
            FROM ibkr_executions e
            WHERE e.sec_type = 'STK'
              AND e.side = 'BUY'
              AND e.reconciled = 0
              AND NOT EXISTS (
                  SELECT 1 FROM ibkr_activities a
                  WHERE a.related_exec_id = e.exec_id
                    AND a.account = e.account
              )
        """
        params: list[Any] = []
        
        if account:
            query += " AND e.account = ?"
            params.append(account)
        
        query += " ORDER BY e.trade_time DESC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_execution_summary(db_path: str, account: Optional[str] = None, days_back: int = 30) -> dict:
    """Get a summary of recent executions.
    
    Args:
        db_path: Path to SQLite database
        account: Optional account filter
        days_back: Number of days to look back
        
    Returns:
        Summary dict with counts by type, symbol, etc.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    try:
        cursor = conn.cursor()
        cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        
        summary: dict[str, Any] = {
            'total_executions': 0,
            'by_sec_type': {},
            'by_side': {},
            'by_symbol': {},
            'date_range': {'start': None, 'end': None},
        }
        
        # Total count
        query = "SELECT COUNT(*) FROM ibkr_executions WHERE trade_time >= ?"
        params: list[Any] = [cutoff]
        if account:
            query += " AND account = ?"
            params.append(account)
        
        cursor.execute(query, params)
        summary['total_executions'] = cursor.fetchone()[0]
        
        # By sec_type
        query = """
            SELECT sec_type, COUNT(*) as count
            FROM ibkr_executions
            WHERE trade_time >= ?
        """
        params = [cutoff]
        if account:
            query += " AND account = ?"
            params.append(account)
        query += " GROUP BY sec_type"
        
        cursor.execute(query, params)
        summary['by_sec_type'] = {row['sec_type']: row['count'] for row in cursor.fetchall()}
        
        # By side
        query = """
            SELECT side, COUNT(*) as count
            FROM ibkr_executions
            WHERE trade_time >= ?
        """
        params = [cutoff]
        if account:
            query += " AND account = ?"
            params.append(account)
        query += " GROUP BY side"
        
        cursor.execute(query, params)
        summary['by_side'] = {row['side']: row['count'] for row in cursor.fetchall()}
        
        # Date range
        query = """
            SELECT MIN(trade_time) as min_time, MAX(trade_time) as max_time
            FROM ibkr_executions
            WHERE trade_time >= ?
        """
        params = [cutoff]
        if account:
            query += " AND account = ?"
            params.append(account)
        
        cursor.execute(query, params)
        row = cursor.fetchone()
        if row:
            summary['date_range'] = {
                'start': row['min_time'],
                'end': row['max_time'],
            }
        
        return summary
    finally:
        conn.close()


# Global instance for convenience
transaction_sync_service = TransactionSyncService()
