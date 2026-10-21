from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dashscope_api_key: SecretStr = SecretStr("")
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = Field(default="qwen3.8-flash", min_length=1, max_length=200)
    model_request_timeout: float = Field(default=60.0, gt=0, le=300)
    max_upload_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=25_000_000, gt=0)
    max_image_edge: int = Field(default=1600, ge=32)

    @field_validator("qwen_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "QWEN_BASE_URL must be an HTTP(S) endpoint without credentials"
            )
        return value.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.dashscope_api_key.get_secret_value().strip())
