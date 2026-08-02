"""Application settings.

Configuration is read once, validated once, and then trusted. Reading
os.environ scattered through the code means a typo in a variable name shows
up as a mysterious None deep inside a request instead of a startup failure.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore unrelated variables in the environment rather than failing:
        # the shell always contains PATH, HOME and dozens of others.
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "info"

    api_host: str = "127.0.0.1"
    api_port: int = 4020

    database_url: str

    # Salt used to pseudonymise entity identifiers. Raw card numbers are
    # never stored; a salted hash makes the same card recognisable across
    # transactions without the original value being recoverable.
    # min_length is enforced so a blank or trivially short salt cannot
    # silently make every hash guessable.
    entity_hash_salt: str = Field(min_length=16)

    # A decision that misses this budget is a defect: the payment gateway
    # times out and a good sale is lost.
    decision_latency_budget_ms: int = 250

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so the .env file is parsed once per process, not per request."""
    return Settings()
