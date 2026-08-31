from pydantic_settings import BaseSettings

class ControlPlaneSettings(BaseSettings):
    tenant_id: str = "default"
    database_url: str = "postgresql+asyncpg://oaos:secret@localhost:5432/oaos"
    redis_url: str = "redis://localhost:6379/0"
    hermes_base_url: str = "http://localhost:8001"
    hermes_api_key: str = ""
    hermes_model: str = "qwen2.5"
    log_level: str = "INFO"
    # Mattermost §16A
    mattermost_bot_token: str = ""
    mattermost_webhook_secret: str = ""
    mattermost_url: str = ""
    # Production credentials are injected by the systemd EnvironmentFile.
    # Do not load production files at import time: tests and local tools must remain isolated.
    model_config = {"env_prefix": "OAOS_CP_", "extra": "ignore"}

settings = ControlPlaneSettings()
