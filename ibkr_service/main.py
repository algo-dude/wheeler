"""FastAPI application for IBKR integration microservice."""
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from ibkr_client import ibkr_client
from sync_service import sync_service

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


@app.post("/api/ibkr/disconnect")
async def disconnect():
    """Disconnect from IBKR TWS/Gateway."""
    try:
        ibkr_client.disconnect()
        return {"success": True, "message": "Disconnected from IBKR"}
    except Exception as e:
        logger.error(f"Disconnect error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.service_host,
        port=settings.service_port,
        reload=True
    )
