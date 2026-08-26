from pydantic_settings import BaseSettings
class ControlPlaneSettings(BaseSettings):
    tenant_id: str = "default"
    database_url: str = "postgresql+asyncpg://open_agent_os:secret@localhost:5432/open_agent_os"
    redis_url: str = "redis://localhost:6379/0"
    hermes_base_url: str = "http://localhost:8001"
    log_level: str = "INFO"
    model_config = {"env_prefix": "OAOS_CP_"}
settings = ControlPlaneSettings()
