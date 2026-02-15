"""FastAPI application for IBKR integration microservice."""
import logging
from typing import Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import settings
from ibkr_client import ibkr_client
from sync_service import sync_service
from transaction_sync import transaction_sync_service
from flex_sync_service import flex_sync_service

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="Wheeler IBKR Service",
    description="Interactive Brokers integration microservice for Wheeler portfolio tracking",
    version="1.0.0"
)

# Configure CORS for communication with Wheeler
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to Wheeler's origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectionConfig(BaseModel):
    """Connection configuration for IBKR."""
    host: Optional[str] = None
    port: Optional[int] = None
    client_id: Optional[int] = None


class TestConnectionResponse(BaseModel):
    """Response for test connection endpoint."""
    success: bool
    connected: bool
    accounts: Optional[list[str]] = None
    server_version: Optional[int] = None
    error: Optional[str] = None


class SyncResponse(BaseModel):
    """Response for sync endpoint."""
    success: bool
    synced_at: Optional[str] = None
    options_synced: int = 0
    positions_synced: int = 0
    options_closed: int = 0
    positions_closed: int = 0
    errors: list[str] = []


class StatusResponse(BaseModel):
    """Response for status endpoint."""
    connected: bool
    last_sync: Optional[str] = None
    last_sync_result: Optional[dict] = None
    database: dict = {}
    account_summary: Optional[dict] = None


class GreekValues(BaseModel):
    """Greek value container."""
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    rho: Optional[float] = None


class OptionGreek(BaseModel):
    """Greek data for an individual option contract."""
    symbol: str
    right: Optional[str] = None
    strike: float
    expiration: Optional[str] = None
    position: Optional[float] = None
    multiplier: Optional[int] = None
    greeks: Optional[GreekValues] = None
    implied_volatility: Optional[float] = None
    data_source: str = "IBKR"


class GreeksResponse(BaseModel):
    """Response for Greek endpoint."""
    options: list[OptionGreek] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class TransactionsSyncRequest(ConnectionConfig):
    """Request payload for ledger (transactions) sync."""

    start: Optional[str] = None
    end: Optional[str] = None
    lookback_days: int = 7
    account: Optional[str] = None
    dry_run: bool = False


class TransactionsSyncResponse(BaseModel):
    success: bool
    synced_at: Optional[str] = None
    executions_upserted: int = 0
    commission_reports_upserted: int = 0
    start: Optional[str] = None
    end: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class CostBasisRequest(BaseModel):
    """Request payload for cost basis recomputation."""
    account: Optional[str] = None
    symbol: Optional[str] = None


class CostBasisResponse(BaseModel):
    """Response for cost basis recomputation."""
    success: bool
    positions_updated: int = 0
    details: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class DetectActivitiesRequest(BaseModel):
    """Request payload for activity detection."""
    account: Optional[str] = None


class DetectActivitiesResponse(BaseModel):
    """Response for activity detection."""
    success: bool
    activities_detected: int = 0
    activities: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class UpdateFeesRequest(BaseModel):
    """Request payload for fee updates."""
    account: Optional[str] = None


class UpdateFeesResponse(BaseModel):
    """Response for fee updates."""
    success: bool
    options_updated: int = 0
    positions_updated: int = 0
    total_fees_applied: float = 0.0
    details: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class TransactionStatusResponse(BaseModel):
    """Response for transaction status endpoint."""
    execution_count: int = 0
    commission_count: int = 0
    activity_count: int = 0
    last_execution_time: Optional[str] = None
    last_activity_time: Optional[str] = None
    activity_breakdown: dict = Field(default_factory=dict)
    error: Optional[str] = None


class FlexSyncRequest(BaseModel):
    """Request payload for Flex report sync."""
    token: Optional[str] = None  # Falls back to IBKR_FLEX_TOKEN env var
    query_id: Optional[str] = None  # Falls back to IBKR_FLEX_QUERY_ID env var
    dry_run: bool = False


class FlexSyncResponse(BaseModel):
    """Response for Flex sync endpoint."""
    success: bool
    executions_synced: int = 0
    commission_reports_synced: int = 0
    activities_synced: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    synced_at: Optional[str] = None


class FlexStatusResponse(BaseModel):
    """Response for Flex status endpoint."""
    flex_configured: bool
    has_token: bool
    has_query_id: bool
    last_flex_sync: Optional[str] = None
    last_flex_sync_result: Optional[dict] = None


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "service": "Wheeler IBKR Service",
        "status": "running",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    """Health check for container orchestration."""
    return {"status": "healthy"}


@app.post("/api/ibkr/test", response_model=TestConnectionResponse)
async def test_connection(config: Optional[ConnectionConfig] = None):
    """
    Test connection to Interactive Brokers TWS/Gateway.
    
    Uses provided configuration or falls back to environment settings.
    """
    try:
        host = config.host if config else None
        port = config.port if config else None
        client_id = config.client_id if config else None
        
        result = await ibkr_client.test_connection(host, port, client_id)
        
        return TestConnectionResponse(
            success=result.get("connected", False),
            connected=result.get("connected", False),
            accounts=result.get("accounts"),
            server_version=result.get("server_version"),
            error=result.get("error")
        )
    except Exception as e:
        logger.error(f"Test connection error: {e}")
        return TestConnectionResponse(
            success=False,
            connected=False,
            error=str(e)
        )


@app.post("/api/ibkr/sync", response_model=SyncResponse)
async def sync_positions(config: Optional[ConnectionConfig] = None):
    """
    Sync all positions from IBKR to Wheeler database.
    
    This will:
    - Connect to TWS/Gateway
    - Fetch all option and stock positions
    - Update Wheeler's SQLite database
    - Close positions no longer present in IBKR
    """
    try:
        host = config.host if config else None
        port = config.port if config else None
        client_id = config.client_id if config else None
        
        result = await sync_service.sync_positions(host, port, client_id)
        
        return SyncResponse(**result)
    except Exception as e:
        logger.error(f"Sync error: {e}")
        return SyncResponse(
            success=False,
            errors=[str(e)]
        )


@app.get("/api/ibkr/status", response_model=StatusResponse)
async def get_status():
    """Get current IBKR connection and sync status."""
    try:
        status = sync_service.get_status()
        return StatusResponse(**status)
    except Exception as e:
        logger.error(f"Status error: {e}")
        return StatusResponse(
            connected=False,
            database={"error": str(e)}
        )


@app.post("/api/ibkr/transactions/sync", response_model=TransactionsSyncResponse)
async def sync_transactions(config: TransactionsSyncRequest = Body(default_factory=TransactionsSyncRequest)):
    """Sync executions + commission reports into Wheeler DB (idempotent)."""
    try:
        host = config.host
        port = config.port
        client_id = config.client_id

        result = await sync_service.sync_transactions(
            host=host,
            port=port,
            client_id=client_id,
            start=config.start,
            end=config.end,
            lookback_days=config.lookback_days,
            account=config.account,
            dry_run=config.dry_run,
        )

        return TransactionsSyncResponse(**result)
    except Exception as e:
        logger.error(f"Transactions sync error: {e}")
        return TransactionsSyncResponse(success=False, errors=[str(e)])


@app.get("/api/ibkr/transactions/status")
async def get_transactions_status():
    """Get status for ledger (transactions) ingestion."""
    try:
        return sync_service.get_transactions_status()
    except Exception as e:
        logger.error(f"Transactions status error: {e}")
        return {
            "connected": False,
            "last_transactions_sync": None,
            "last_transactions_sync_result": None,
            "database": {"error": str(e)},
        }


@app.post("/api/ibkr/transactions/sync-flex", response_model=FlexSyncResponse)
async def sync_flex_transactions(request: FlexSyncRequest = Body(default_factory=FlexSyncRequest)):
    """
    Sync transactions from IBKR Flex Web Service report.
    
    This endpoint fetches a Flex report using the provided credentials (or env vars)
    and syncs executions, commission reports, and activities to the database.
    
    Request body (all optional - falls back to env vars):
    - token: Flex Web Service token (uses IBKR_FLEX_TOKEN if not provided)
    - query_id: Flex Query ID (uses IBKR_FLEX_QUERY_ID if not provided)
    - dry_run: If true, parse and validate but don't write to database
    """
    try:
        # Resolve credentials: request body > env vars
        token = request.token or settings.flex_token
        query_id = request.query_id or settings.flex_query_id
        
        # Validate credentials are available
        if not token:
            logger.warning("[FLEX] No Flex token provided in request or IBKR_FLEX_TOKEN env var")
            return FlexSyncResponse(
                success=False,
                error="No Flex token provided. Set IBKR_FLEX_TOKEN env var or provide 'token' in request body."
            )
        
        if not query_id:
            logger.warning("[FLEX] No Flex query_id provided in request or IBKR_FLEX_QUERY_ID env var")
            return FlexSyncResponse(
                success=False,
                error="No Flex query_id provided. Set IBKR_FLEX_QUERY_ID env var or provide 'query_id' in request body."
            )
        
        logger.info("[FLEX] Starting Flex sync (query_id=%s, dry_run=%s)", query_id, request.dry_run)
        
        # Call the Flex sync service
        result = await flex_sync_service.sync_flex_report(
            token=token,
            query_id=query_id,
            dry_run=request.dry_run,
        )
        
        return FlexSyncResponse(
            success=result.success,
            executions_synced=result.executions_synced,
            commission_reports_synced=result.commission_reports_synced,
            activities_synced=result.activities_synced,
            warnings=result.warnings,
            error=result.error,
            from_date=result.from_date,
            to_date=result.to_date,
            synced_at=result.synced_at,
        )
        
    except Exception as e:
        logger.error(f"[FLEX] Flex sync error: {e}")
        return FlexSyncResponse(
            success=False,
            error=str(e)
        )


@app.get("/api/ibkr/flex/status", response_model=FlexStatusResponse)
async def get_flex_status():
    """
    Get Flex Web Service configuration and sync status.
    
    Returns whether Flex credentials are configured (via env vars) and
    the last sync timestamp/result if available.
    """
    try:
        has_token = bool(settings.flex_token)
        has_query_id = bool(settings.flex_query_id)
        flex_configured = has_token and has_query_id
        
        # Get last sync status from service
        status = flex_sync_service.get_status()
        
        return FlexStatusResponse(
            flex_configured=flex_configured,
            has_token=has_token,
            has_query_id=has_query_id,
            last_flex_sync=status.get("last_flex_sync"),
            last_flex_sync_result=status.get("last_flex_sync_result"),
        )
        
    except Exception as e:
        logger.error(f"[FLEX] Flex status error: {e}")
        return FlexStatusResponse(
            flex_configured=False,
            has_token=False,
            has_query_id=False,
        )


@app.get("/api/ibkr/transactions/detailed-status", response_model=TransactionStatusResponse)
async def get_detailed_transactions_status():
    """Get detailed transaction status from the transaction sync service.
    
    Returns counts for executions, commissions, and activities along with
    breakdown by activity type.
    """
    try:
        status = transaction_sync_service.get_transaction_status()
        return TransactionStatusResponse(**status)
    except Exception as e:
        logger.error(f"Detailed transactions status error: {e}")
        return TransactionStatusResponse(error=str(e))


@app.post("/api/ibkr/activities/detect", response_model=DetectActivitiesResponse)
async def detect_activities(request: DetectActivitiesRequest = Body(default_factory=DetectActivitiesRequest)):
    """Detect assignment/exercise/expiration events from execution data.
    
    Analyzes ibkr_executions to find patterns indicating:
    - Assignments (PUT option assigned, resulting in stock purchase)
    - Exercises (CALL option exercised)
    - Expirations (option expired worthless)
    
    Detected events are inserted into ibkr_activities table.
    """
    try:
        events = transaction_sync_service.detect_assignments_exercises(
            account=request.account
        )
        return DetectActivitiesResponse(
            success=True,
            activities_detected=len(events),
            activities=events,
        )
    except Exception as e:
        logger.error(f"Activity detection error: {e}")
        return DetectActivitiesResponse(
            success=False,
            errors=[str(e)],
        )


@app.post("/api/ibkr/cost-basis/recompute", response_model=CostBasisResponse)
async def recompute_cost_basis(request: CostBasisRequest = Body(default_factory=CostBasisRequest)):
    """Recompute adjusted cost basis for long positions based on assignments.
    
    For positions acquired via PUT assignment:
    - adjusted_cost_basis_per_share = strike_price - premium_received_per_share
    - adjusted_cost_basis_total = per_share * shares
    
    Request body:
    - account: Optional account filter
    - symbol: Optional symbol filter. If omitted, recomputes all positions.
    """
    try:
        result = transaction_sync_service.recompute_cost_basis(
            account=request.account,
            symbol=request.symbol,
        )
        return CostBasisResponse(
            success=True,
            positions_updated=result.get('positions_updated', 0),
            details=result.get('details', []),
            errors=result.get('errors', []),
        )
    except Exception as e:
        logger.error(f"Cost basis recomputation error: {e}")
        return CostBasisResponse(
            success=False,
            errors=[str(e)],
        )


@app.post("/api/ibkr/fees/update", response_model=UpdateFeesResponse)
async def update_fees(request: UpdateFeesRequest = Body(default_factory=UpdateFeesRequest)):
    """Update fees on options and positions from commission reports.
    
    Sums commissions from ibkr_commission_reports and updates:
    - options.commission for option trades
    - long_positions.fees for stock trades
    """
    try:
        option_result = transaction_sync_service.update_option_fees(
            account=request.account
        )
        position_result = transaction_sync_service.update_position_fees(
            account=request.account
        )
        
        return UpdateFeesResponse(
            success=True,
            options_updated=option_result.get('options_updated', 0),
            positions_updated=position_result.get('positions_updated', 0),
            total_fees_applied=(
                option_result.get('total_fees_applied', 0.0) +
                position_result.get('total_fees_applied', 0.0)
            ),
            details=option_result.get('details', []) + position_result.get('details', []),
            errors=option_result.get('errors', []) + position_result.get('errors', []),
        )
    except Exception as e:
        logger.error(f"Fee update error: {e}")
        return UpdateFeesResponse(
            success=False,
            errors=[str(e)],
        )


@app.post("/api/ibkr/disconnect")
async def disconnect():
    """Disconnect from IBKR TWS/Gateway."""
    try:
        ibkr_client.disconnect()
        return {"success": True, "message": "Disconnected from IBKR"}
    except Exception as e:
        logger.error(f"Disconnect error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ibkr/greeks", response_model=GreeksResponse)
async def get_greeks(
    host: Optional[str] = None,
    port: Optional[int] = None,
    client_id: Optional[int] = None,
    market_data_type: Optional[int] = 4,
):
    """
    Return Greek data for currently held option positions.

    market_data_type:
        1 = Live (requires subscription)
        2 = Frozen (requires subscription)
        3 = Delayed (free, 15-20 min delay)
        4 = Delayed-frozen (free, last delayed value - recommended)

    This uses IBKR market data when connected; errors are returned but
    will not raise to keep Wheeler UI responsive.
    """
    response = GreeksResponse(options=[], errors=[])

    try:
        if not ibkr_client.is_connected:
            connected = await ibkr_client.connect(host, port, client_id)
            if not connected:
                response.errors.append("Failed to connect to IBKR for Greeks")
                return response

        # Set market data type before requesting data
        await ibkr_client.set_market_data_type(market_data_type)

        positions = await ibkr_client.get_positions()
        option_positions = [p for p in positions if p.get("sec_type") == "OPT"]

        greeks_result = await ibkr_client.get_option_greeks(option_positions)
        if isinstance(greeks_result, dict):
            greeks = greeks_result.get("options", [])
            response.errors.extend(greeks_result.get("errors", []) or [])
        else:
            # Backward compatibility if the client returns a plain list
            greeks = greeks_result

        for g in greeks:
            try:
                response.options.append(OptionGreek(**g))
            except Exception as e:
                logger.error(f"Failed to parse Greek payload for option {g.get('symbol')}: {e}")
                response.errors.append(str(e))
    except Exception as e:
        logger.error(f"Greek retrieval error: {e}")
        response.errors.append(str(e))

    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.service_host,
        port=settings.service_port,
        reload=True
    )
