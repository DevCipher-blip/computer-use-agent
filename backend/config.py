from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str  # read from the DATABASE_URL environment variable
    docker_network: str = "computeagent_network"
    session_agent_image: str = "computeagent/session-agent:latest"
    session_agent_port: int = 8765
    # How session containers reach the backend for the completion callback.
    # "backend" is the Compose service name on the shared network.
    backend_internal_url: str = "http://backend:8000"
    vnc_base_port: int = 5900
    novnc_base_port: int = 6080
    max_concurrent_sessions: int = 20
    container_ready_timeout: int = 90

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
