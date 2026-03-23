from __future__ import annotations

"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the API service.

    Values are loaded from `.env` and process environment variables.
    Both current `AZURE_*` names and legacy aliases are supported.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    azure_speech_key: str = Field(
        ...,
        validation_alias=AliasChoices("AZURE_SPEECH_KEY", "SS_KEY"),
    )
    azure_speech_region: str = Field(
        ...,
        validation_alias=AliasChoices("AZURE_SPEECH_REGION", "SS_LOCATION"),
    )
    azure_openai_endpoint: HttpUrl = Field(
        ...,
        validation_alias=AliasChoices("AZURE_OPENAI_ENDPOINT", "GPT_ENDPOINT"),
    )
    azure_openai_api_key: str = Field(
        ...,
        validation_alias=AliasChoices("AZURE_OPENAI_API_KEY", "GPT_KEY"),
    )
    azure_openai_deployment: str = Field(
        ...,
        validation_alias=AliasChoices("AZURE_OPENAI_DEPLOYMENT", "GPT_DEPLOYMENT_NAME"),
    )
    azure_openai_api_version: str = Field(
        default="2024-02-15-preview",
        validation_alias=AliasChoices("AZURE_OPENAI_API_VERSION", "GPT_API_VERSION"),
    )
    api_bearer_token: str = Field(..., validation_alias=AliasChoices("API_BEARER_TOKEN"))
    max_upload_mb: int = Field(default=25, validation_alias=AliasChoices("MAX_UPLOAD_MB"))
    max_duration_minutes: int = Field(default=20, validation_alias=AliasChoices("MAX_DURATION_MINUTES"))
    prompt_version: str = Field(default="1", validation_alias=AliasChoices("PROMPT_VERSION"))
    taxonomy_path: Path = Field(
        default=Path("categories.yaml"),
        validation_alias=AliasChoices("TAXONOMY_PATH"),
    )
    ffmpeg_binary: str = Field(default="ffmpeg", validation_alias=AliasChoices("FFMPEG_BINARY"))
    enable_ffmpeg: bool = Field(default=True, validation_alias=AliasChoices("ENABLE_FFMPEG"))
    log_transcripts: bool = Field(default=False, validation_alias=AliasChoices("LOG_TRANSCRIPTS"))
    log_level: str = Field(default="DEBUG", validation_alias=AliasChoices("LOG_LEVEL"))
    log_file: Path = Field(default=Path("logs/calls_category_api.log"), validation_alias=AliasChoices("LOG_FILE"))
    log_max_bytes: int = Field(default=10_485_760, validation_alias=AliasChoices("LOG_MAX_BYTES"))
    log_backup_count: int = Field(default=5, validation_alias=AliasChoices("LOG_BACKUP_COUNT"))
    verbose_ai_logs: bool = Field(default=False, validation_alias=AliasChoices("VERBOSE_AI_LOGS"))
    stt_languages_raw: str = Field(default="uk-UA", validation_alias=AliasChoices("STT_LANGUAGES"))
    max_concurrent_calls: int = Field(default=2, ge=1, validation_alias=AliasChoices("MAX_CONCURRENT_CALLS"))
    openai_timeout_seconds: int = Field(default=60, ge=1, validation_alias=AliasChoices("OPENAI_TIMEOUT_SECONDS"))
    openai_max_attempts: int = Field(default=3, ge=1, validation_alias=AliasChoices("OPENAI_MAX_ATTEMPTS"))
    openai_retry_base_delay_ms: int = Field(
        default=500,
        ge=50,
        validation_alias=AliasChoices("OPENAI_RETRY_BASE_DELAY_MS"),
    )
    speech_timeout_seconds: int = Field(default=3600, ge=1, validation_alias=AliasChoices("SPEECH_TIMEOUT_SECONDS"))
    speech_max_attempts: int = Field(default=2, ge=1, validation_alias=AliasChoices("SPEECH_MAX_ATTEMPTS"))
    speech_retry_base_delay_ms: int = Field(
        default=500,
        ge=50,
        validation_alias=AliasChoices("SPEECH_RETRY_BASE_DELAY_MS"),
    )

    @property
    def project_root(self) -> Path:
        """Return absolute project root path."""
        return Path(__file__).resolve().parent.parent

    @property
    def taxonomy_file(self) -> Path:
        """Return absolute path to taxonomy YAML file."""
        if self.taxonomy_path.is_absolute():
            return self.taxonomy_path
        return self.project_root / self.taxonomy_path

    @property
    def max_upload_bytes(self) -> int:
        """Convert max upload size from MB to bytes."""
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_duration_seconds(self) -> int:
        """Convert max audio duration from minutes to seconds."""
        return self.max_duration_minutes * 60

    @property
    def log_file_path(self) -> Path:
        """Return absolute path to application log file."""
        if self.log_file.is_absolute():
            return self.log_file
        return self.project_root / self.log_file

    @property
    def stt_languages(self) -> list[str]:
        """Parse comma-separated STT language list with safe default."""
        parts = [lang.strip() for lang in self.stt_languages_raw.split(",")]
        languages = [lang for lang in parts if lang]
        if not languages:
            return ["uk-UA"]
        return languages

    def redacted_dict(self) -> dict[str, Any]:
        """Return settings dictionary with sensitive values masked."""
        model_data = self.model_dump()
        for key in ("azure_speech_key", "azure_openai_api_key", "api_bearer_token"):
            if key in model_data:
                model_data[key] = "***"
        model_data["stt_languages"] = self.stt_languages
        return model_data


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance for dependency injection."""
    return Settings()
