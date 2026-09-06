"""Application settings.

One place where environment becomes typed configuration, so nothing else in the
app reads os.environ. The production guards live here too: a misconfigured
deployment should refuse to boot rather than run insecurely and look fine.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value shipped in .env.example. If this reaches production it is a complete
# auth bypass -- anyone can mint a token for any account -- so it is named here
# and refused below rather than merely discouraged in a comment.
DEV_JWT_SECRET = "dev-only-do-not-use-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"

    database_url: str = "postgresql+psycopg://commonground:commonground@localhost:5434/commonground"

    # Unset means the in-process broadcaster, which is correct for a single API
    # instance and is how the free-tier deployment runs. See docs/architecture.md.
    redis_url: str | None = None

    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    # Short-lived, because it cannot be revoked. Revocation happens by refusing
    # to mint the next one.
    access_token_minutes: int = 30
    refresh_token_days: int = 30

    demo_user_password: str = "demo-read-only"

    # MetaBrainz asks for a real contact address on automated requests.
    user_agent: str = "CommonGround/0.1 (set USER_AGENT before running ingest)"

    cors_origins: str = "http://localhost:5173"

    # An upload larger than this is rejected before it is read into memory.
    max_import_bytes: int = 25 * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _refuse_insecure_production(self) -> Settings:
        if self.environment != "production":
            return self
        if self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < 32:
            raise ValueError(
                "JWT_SECRET is unset, the example value, or too short. A predictable "
                "signing key is a complete auth bypass, so the API refuses to start "
                "in production without a real one."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached so settings are read once, and overridable in tests by clearing it."""
    return Settings()
