"""
IBKR Flex Sync Service - Orchestrates Flex report fetching and database ingestion.

This module provides FlexSyncService, which:
1. Uses FlexQueryClient to fetch Flex XML reports
2. Uses FlexXMLParser to parse the XML
3. Upserts data to Wheeler's SQLite database using database.py functions

Usage:
    service = FlexSyncService(db_path="/path/to/wheeler.db")
    result = await service.sync_flex_report(token="xxx", query_id="123")
    print(f"Synced {result.executions_synced} executions")
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from flex_client import FlexQueryClient, FlexXMLParser, FlexReport, FlexError
from database import (
    get_db_connection,
    upsert_ibkr_execution,
    upsert_ibkr_commission_report,
    upsert_ibkr_activity,
)

logger = logging.getLogger(__name__)


@dataclass
class FlexSyncResult:
    """Result of a Flex sync operation.
    
    Attributes:
        success: Whether the sync completed without fatal errors
        executions_synced: Number of execution records upserted
        commission_reports_synced: Number of commission report records upserted
        activities_synced: Number of activity records upserted
        warnings: List of non-fatal warnings encountered
        error: Fatal error message if success=False
        from_date: Report start date (from Flex report metadata)
        to_date: Report end date (from Flex report metadata)
    """
    success: bool
    executions_synced: int = 0
    commission_reports_synced: int = 0
    activities_synced: int = 0
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    synced_at: Optional[str] = None


class FlexSyncService:
    """Service for syncing IBKR Flex reports to Wheeler database.
    
    This service orchestrates the complete Flex sync flow:
    1. Fetch Flex XML report via FlexQueryClient
    2. Parse XML into structured data via FlexXMLParser
    3. Upsert all records to database via database.py functions
    
    Args:
        db_path: Path to Wheeler SQLite database (optional, uses default resolution)
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """Initialize the Flex sync service.
        
        Args:
            db_path: Optional database path override. If None, uses database.py
                     default resolution (respects IBKR_DATABASE_PATH env var).
        """
        self.db_path = db_path
        self.parser = FlexXMLParser()
        
        # Track last sync for status reporting
        self.last_sync: Optional[datetime] = None
        self.last_sync_result: Optional[FlexSyncResult] = None
        
        logger.info("[FLEX_SYNC] FlexSyncService initialized (db_path=%s)", db_path or "default")
    
    async def sync_flex_report(
        self,
        token: str,
        query_id: str,
        dry_run: bool = False,
    ) -> FlexSyncResult:
        """Fetch and sync a Flex report to the database.
        
        This is the main entry point for Flex sync operations. It:
        1. Creates a FlexQueryClient and fetches the XML report
        2. Parses the XML into structured FlexReport
        3. Upserts all records to the database in a transaction
        4. Returns statistics about the sync operation
        
        Args:
            token: IBKR Flex Web Service token
            query_id: Flex Query ID to execute
            dry_run: If True, parse and validate but don't write to database
            
        Returns:
            FlexSyncResult with sync statistics and any warnings/errors
        """
        logger.info(
            "[FLEX_SYNC] Starting Flex sync (query_id=%s, dry_run=%s)",
            query_id, dry_run
        )
        
        result = FlexSyncResult(success=False)
        
        try:
            # Step 1: Fetch Flex XML report
            logger.info("[FLEX_SYNC] Fetching Flex report from IBKR...")
            client = FlexQueryClient(token=token, query_id=query_id)
            xml_content = await client.fetch_flex_report()
            logger.info("[FLEX_SYNC] Received %d bytes of XML", len(xml_content))
            
            # Step 2: Parse XML
            logger.info("[FLEX_SYNC] Parsing Flex XML...")
            report = self.parser.parse(xml_content)
            
            # Copy metadata and warnings from parsed report
            result.from_date = report.from_date
            result.to_date = report.to_date
            result.warnings.extend(report.warnings)
            
            logger.info(
                "[FLEX_SYNC] Parsed: %d executions, %d commission reports, %d activities",
                len(report.executions),
                len(report.commission_reports),
                len(report.activities),
            )
            
            if dry_run:
                logger.info("[FLEX_SYNC] Dry run mode - skipping database writes")
                result.warnings.append("dry_run=true: no database writes performed")
                result.executions_synced = len(report.executions)
                result.commission_reports_synced = len(report.commission_reports)
                result.activities_synced = len(report.activities)
                result.success = True
                result.synced_at = datetime.now().isoformat()
                return result
            
            # Step 3: Write to database
            result = await self._sync_to_database(report, result)
            
        except FlexError as e:
            logger.error("[FLEX_SYNC] Flex API error: %s (code=%s)", e, e.error_code)
            result.error = str(e)
            result.success = False
        except Exception as e:
            logger.exception("[FLEX_SYNC] Unexpected error during sync")
            result.error = f"Unexpected error: {e}"
            result.success = False
        
        # Update tracking state
        self.last_sync = datetime.now()
        self.last_sync_result = result
        
        logger.info(
            "[FLEX_SYNC] Sync complete: success=%s, executions=%d, commissions=%d, activities=%d",
            result.success,
            result.executions_synced,
            result.commission_reports_synced,
            result.activities_synced,
        )
        
        return result
    
    async def _sync_to_database(
        self,
        report: FlexReport,
        result: FlexSyncResult,
    ) -> FlexSyncResult:
        """Sync parsed FlexReport data to database.
        
        All database operations are wrapped in a transaction for atomicity.
        
        Args:
            report: Parsed FlexReport with executions, commission_reports, activities
            result: FlexSyncResult to populate with statistics
            
        Returns:
            Updated FlexSyncResult
        """
        logger.info("[FLEX_SYNC] Beginning database sync...")
        
        conn = get_db_connection()
        try:
            # Begin transaction
            conn.execute("BEGIN")
            
            # Sync executions
            exec_count = self._sync_executions(conn, report.executions, result)
            result.executions_synced = exec_count
            
            # Sync commission reports
            comm_count = self._sync_commission_reports(conn, report.commission_reports, result)
            result.commission_reports_synced = comm_count
            
            # Sync activities
            activity_count = self._sync_activities(conn, report.activities, result)
            result.activities_synced = activity_count
            
            # Commit transaction
            conn.commit()
            logger.info("[FLEX_SYNC] Database transaction committed")
            
            result.success = True
            result.synced_at = datetime.now().isoformat()
            
        except Exception as e:
            logger.error("[FLEX_SYNC] Database error, rolling back: %s", e)
            conn.rollback()
            result.error = f"Database error: {e}"
            result.success = False
        finally:
            conn.close()
        
        return result
    
    def _sync_executions(
        self,
        conn,
        executions: list[dict],
        result: FlexSyncResult,
    ) -> int:
        """Sync execution records to ibkr_executions table.
        
        Args:
            conn: Database connection
            executions: List of execution dicts from FlexReport
            result: FlexSyncResult to append warnings to
            
        Returns:
            Number of successfully upserted records
        """
        count = 0
        for row in executions:
            try:
                # Validate required fields
                if not row.get("account") or not row.get("exec_id"):
                    result.warnings.append(
                        f"Execution missing account/exec_id: {row.get('symbol', 'unknown')}"
                    )
                    continue
                
                if not row.get("symbol"):
                    result.warnings.append(
                        f"Execution missing symbol: exec_id={row.get('exec_id')}"
                    )
                    continue
                
                # Use source from row (should be "FLEX") or default to "FLEX"
                source = row.get("source", "FLEX")
                
                upsert_ibkr_execution(conn, row, source=source)
                count += 1
                
            except Exception as e:
                logger.warning(
                    "[FLEX_SYNC] Failed to upsert execution %s: %s",
                    row.get("exec_id"), e
                )
                result.warnings.append(
                    f"Execution upsert failed for exec_id={row.get('exec_id')}: {e}"
                )
        
        logger.info("[FLEX_SYNC] Upserted %d/%d executions", count, len(executions))
        return count
    
    def _sync_commission_reports(
        self,
        conn,
        commission_reports: list[dict],
        result: FlexSyncResult,
    ) -> int:
        """Sync commission report records to ibkr_commission_reports table.
        
        Args:
            conn: Database connection
            commission_reports: List of commission report dicts from FlexReport
            result: FlexSyncResult to append warnings to
            
        Returns:
            Number of successfully upserted records
        """
        count = 0
        for row in commission_reports:
            try:
                # Validate required fields
                if not row.get("account") or not row.get("exec_id"):
                    result.warnings.append(
                        f"Commission report missing account/exec_id"
                    )
                    continue
                
                # Source defaults to FLEX for commission reports from Flex
                source = row.get("source", "FLEX")
                
                upsert_ibkr_commission_report(conn, row, source=source)
                count += 1
                
            except Exception as e:
                logger.warning(
                    "[FLEX_SYNC] Failed to upsert commission report %s: %s",
                    row.get("exec_id"), e
                )
                result.warnings.append(
                    f"Commission upsert failed for exec_id={row.get('exec_id')}: {e}"
                )
        
        logger.info("[FLEX_SYNC] Upserted %d/%d commission reports", count, len(commission_reports))
        return count
    
    def _sync_activities(
        self,
        conn,
        activities: list[dict],
        result: FlexSyncResult,
    ) -> int:
        """Sync activity records to ibkr_activities table.
        
        Args:
            conn: Database connection
            activities: List of activity dicts from FlexReport
            result: FlexSyncResult to append warnings to
            
        Returns:
            Number of successfully upserted records
        """
        count = 0
        for row in activities:
            try:
                # Validate required fields
                if not row.get("account"):
                    result.warnings.append(
                        f"Activity missing account: {row.get('activity_type', 'unknown')}"
                    )
                    continue
                
                if not row.get("activity_type"):
                    result.warnings.append(
                        f"Activity missing activity_type: {row.get('symbol', 'unknown')}"
                    )
                    continue
                
                # Source defaults to FLEX for activities from Flex
                source = row.get("source", "FLEX")
                
                upsert_ibkr_activity(conn, row, source=source)
                count += 1
                
            except Exception as e:
                logger.warning(
                    "[FLEX_SYNC] Failed to upsert activity %s: %s",
                    row.get("activity_id"), e
                )
                result.warnings.append(
                    f"Activity upsert failed for activity_id={row.get('activity_id')}: {e}"
                )
        
        logger.info("[FLEX_SYNC] Upserted %d/%d activities", count, len(activities))
        return count
    
    def get_status(self) -> dict:
        """Get current Flex sync status.
        
        Returns:
            Dict with last sync timestamp and result
        """
        return {
            "last_flex_sync": self.last_sync.isoformat() if self.last_sync else None,
            "last_flex_sync_result": {
                "success": self.last_sync_result.success,
                "executions_synced": self.last_sync_result.executions_synced,
                "commission_reports_synced": self.last_sync_result.commission_reports_synced,
                "activities_synced": self.last_sync_result.activities_synced,
                "from_date": self.last_sync_result.from_date,
                "to_date": self.last_sync_result.to_date,
                "warnings": self.last_sync_result.warnings,
                "error": self.last_sync_result.error,
                "synced_at": self.last_sync_result.synced_at,
            } if self.last_sync_result else None,
        }


# Module-level singleton for use in FastAPI endpoints
flex_sync_service = FlexSyncService()
