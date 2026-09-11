from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SourcePathMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_prefix: str = Field(min_length=3, max_length=2048)
    local_root: Path

    @field_validator("source_prefix")
    @classmethod
    def unc_prefix(cls, value: str) -> str:
        normalized = value.strip().rstrip("\\/")
        if not normalized.startswith("\\\\") or "\x00" in normalized:
            raise ValueError("Source prefix must be an absolute UNC prefix")
        return normalized

    @field_validator("local_root")
    @classmethod
    def absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Mapped source root must be absolute")
        return value


class RuntimeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heartbeat_interval: float = Field(gt=0)
    poll_interval: float = Field(gt=0)
    retry_initial_seconds: float = Field(gt=0)
    retry_max_seconds: float = Field(ge=1)
    storage_retry_limit: int = Field(ge=0)
    file_not_found_retry_limit: int = Field(ge=0)
    health_interval: float = Field(gt=0)
    health_max_age: float = Field(gt=0)
    wake_timeout_seconds: float | None = Field(default=None, gt=0)
    path_mappings: list[SourcePathMapping] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def related_values(self):
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("Retry max must not be below retry initial")
        if self.health_max_age <= self.health_interval:
            raise ValueError("Health max age must exceed health interval")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    proof_core_url: str
    proof_worker_token: SecretStr
    proof_worker_name: str = Field(min_length=1, max_length=160)
    worker_data_path: Path = Path("/data")
    worker_output_path: Path = Path("/output")
    worker_source_roots: list[Path] = Field(default_factory=lambda: [Path("/sources/main")])
    log_level: str = "INFO"
    heartbeat_interval: float = Field(default=30, gt=0)
    poll_interval: float = Field(default=5, gt=0)
    http_timeout: float = Field(default=30, gt=0)
    retry_initial_seconds: float = Field(default=1, gt=0)
    retry_max_seconds: float = Field(default=30, ge=1)
    storage_retry_limit: int = Field(default=5, ge=0)
    file_not_found_retry_limit: int = Field(default=0, ge=0)
    health_interval: float = Field(default=5, gt=0)
    health_max_age: float = Field(default=90, gt=0)
    vips_concurrency: int = Field(default=2, ge=1)
    vips_cache_memory_mb: int = Field(default=128, ge=0)
    worker_listen_host: str = "0.0.0.0"
    worker_listen_port: int = Field(default=8090, ge=1, le=65535)

    @field_validator("proof_core_url")
    @classmethod
    def safe_url(cls, value):
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Core URL must be HTTP(S), without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("worker_source_roots")
    @classmethod
    def source_roots(cls, roots):
        if not roots or any(not path.is_absolute() for path in roots):
            raise ValueError("Source roots must be absolute local mount paths")
        return roots

    @field_validator("proof_worker_token")
    @classmethod
    def token_not_empty(cls, token):
        if not token.get_secret_value().strip():
            raise ValueError("Worker token is required")
        return token
