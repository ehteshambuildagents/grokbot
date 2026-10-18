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
    default_model: str = "qwen2.5:3b"  # measured choice (docs/MODELS.md); non-commercial license, see THIRD_PARTY_NOTICES
    model_timeout_s: float = 600
    allow_scripted_model: bool = False  # test-only MOCK provider
    # Optional hosted model API (OpenAI-compatible Chat Completions: OpenAI, OpenRouter, Groq, Together, DeepSeek,
    # Mistral, LM Studio, vLLM, ...). Off by default: prompts, memories and tool results leave this machine.
    # Select per bot or as default with model names "remote:<provider-model-id>".
    remote_providers_enabled: bool = False
    remote_base_url: str = ""          # e.g. https://api.openai.com/v1 or https://openrouter.ai/api/v1
    remote_api_key: str = ""           # keep in .env only; never stored in the DB or shown to models
    remote_models: str = ""            # comma-separated ids offered in the UI, e.g. "gpt-4o-mini,gpt-4.1"
    remote_context_length: int = 128000
    remote_json_mode: bool = True      # send response_format=json_object for planning (disable if the API rejects it)

    computer_url: str = "http://127.0.0.1:8765"
    # Shared secret between API/worker and the computer service. Empty → a random per-install token is created
    # next to the secret key file (see internal_token()).
    computer_token: str = ""
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

    def internal_token(self) -> str:
        if self.computer_token:
            return self.computer_token
        f = self.secret_key_file.parent / "computer.token"
        if not f.exists():
            import secrets
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(secrets.token_urlsafe(32))
        return f.read_text().strip()

    @property
    def workspaces(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def artifacts(self) -> Path:
        return self.data_dir / "artifacts"


@lru_cache
def settings() -> Settings:
    return Settings()
