"""Configuration settings for the IBKR microservice."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # TWS/Gateway connection settings
    tws_host: str = "127.0.0.1"
    tws_port: int = 7497  # 7497 for TWS paper, 7496 for TWS live, 4002 for Gateway
    client_id: int = 1
    
    # Database path (shared with Wheeler)
    database_path: str = "/app/data/wheeler.db"
    
    # Service settings
    service_host: str = "0.0.0.0"
    service_port: int = 8081
    
    class Config:
        env_prefix = "IBKR_"
        case_sensitive = False


settings = Settings()
