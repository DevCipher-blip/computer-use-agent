from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://cambio:cambio@postgres:5432/cambio"
    docker_network: str = "cambioml_network"
    session_agent_image: str = "cambioml/session-agent:latest"
    session_agent_port: int = 8765
    vnc_base_port: int = 5900
    novnc_base_port: int = 6080
    max_concurrent_sessions: int = 20
    container_ready_timeout: int = 90

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
