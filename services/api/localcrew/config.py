from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOCALCREW_", env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localcrew:localcrew-dev-only@127.0.0.1:54329/localcrew"
    data_dir: Path = ROOT / ".local" / "data"
    # Key file lives outside the database. Back it up separately (docs/OPERATIONS.md).
    secret_key_file: Path = ROOT / ".local" / "keys" / "secrets.key"
    bind_host: str = "127.0.0.1"
    public_origin: str = "http://127.0.0.1:5173"
    secure_cookies: bool = False  # set true behind TLS
    session_hours: int = 24 * 7
    default_timezone: str = "Asia/Karachi"

    ollama_url: str = "http://127.0.0.1:11434"
    default_model: str = "qwen2.5:1.5b"  # measured choice, docs/MODELS.md
    model_timeout_s: float = 600
    allow_scripted_model: bool = False  # test-only MOCK provider
    remote_providers_enabled: bool = False

    computer_url: str = "http://127.0.0.1:8765"
    computer_token: str = "dev-internal-token-change-me"
    searxng_url: str = ""
    # Hosts the browser/fetch tools may reach even though they resolve to private IPs (fixtures).
    private_host_allowlist: str = "127.0.0.1:8899,localhost:8899"

    worker_concurrency: int = 2
    model_concurrency: int = 1
    lease_seconds: int = 30
    heartbeat_seconds: int = 5
    max_upload_mb: int = 20
    max_handoff_depth: int = 3
    default_tool_budget: int = 20
    default_task_timeout_s: int = 1800

    @property
    def workspaces(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def artifacts(self) -> Path:
        return self.data_dir / "artifacts"


@lru_cache
def settings() -> Settings:
    return Settings()
