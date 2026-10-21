import base64
import binascii
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    dashscope_api_key: SecretStr = SecretStr("")
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = Field(default="qwen3.8-flash", min_length=1, max_length=200)
    model_request_timeout: float = Field(default=60.0, gt=0, le=300)
    max_upload_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=25_000_000, gt=0)
    max_image_edge: int = Field(default=1600, ge=32)
    wechat_app_id: str = ""
    wechat_app_secret: SecretStr = SecretStr("")
    wechat_callback_token: SecretStr = SecretStr("")
    wechat_encoding_aes_key: SecretStr = SecretStr("")
    public_base_url: str = ""
    wechat_request_timeout: float = Field(default=10.0, gt=0, le=300)
    moderation_job_ttl_seconds: int = Field(default=2100, gt=0)
    moderation_max_jobs: int = Field(default=100, gt=0)
    moderation_max_pending: int = Field(default=20, gt=0)
    moderation_max_pending_per_user: int = Field(default=2, gt=0)

    @field_validator("wechat_encoding_aes_key")
    @classmethod
    def validate_aes_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw:
            try:
                key = base64.b64decode(raw + "=", validate=True)
            except (ValueError, binascii.Error):
                raise ValueError(
                    "WECHAT_ENCODING_AES_KEY must encode 32 bytes"
                ) from None
            if len(raw) != 43 or len(key) != 32:
                raise ValueError("WECHAT_ENCODING_AES_KEY must encode 32 bytes")
        return value

    @field_validator("public_base_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        if not value:
            return value
        parts = urlsplit(value)
        # Accessing port validates malformed/out-of-range port numbers as well.
        _ = parts.port
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or any(char.isspace() for char in value)
        ):
            raise ValueError(
                "PUBLIC_BASE_URL must be an HTTP(S) URL without credentials"
            )
        return value.rstrip("/")

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

    @property
    def security_configured(self) -> bool:
        credentials = (
            self.wechat_app_id,
            self.wechat_app_secret.get_secret_value(),
            self.wechat_callback_token.get_secret_value(),
            self.wechat_encoding_aes_key.get_secret_value(),
        )
        return all(
            value
            and len(value) <= 512
            and value.isascii()
            and all(33 <= ord(char) <= 126 for char in value)
            for value in credentials
        ) and self.public_base_url.startswith("https://")
