"""Configuration loader and validator for ATLAS.

Loads environment variables from a local `.env` file (or the process env)
and validates them at startup. Fails fast with a clear, actionable error
message rather than letting the app crash later with a cryptic stack trace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_API_KEY_PREFIX = "sk-ant-"
_PLACEHOLDER_MARKER = "REPLACE_WITH_YOUR_KEY"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Immutable application configuration.

    Use `Config.load()` to build an instance. The frozen dataclass prevents
    accidental mutation of config values after startup.
    """

    anthropic_api_key: str
    log_level: str
    bot_app_id: str
    bot_app_password: str
    bot_tenant_id: str
    azure_storage_connection_string: str

    @classmethod
    def load(cls) -> "Config":
        """Load and validate configuration from `.env` and the environment.

        Returns:
            A fully validated `Config` instance.

        Raises:
            ConfigError: If any required value is missing or malformed.
        """
        load_dotenv()

        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        cls._validate_api_key(api_key)

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got: {log_level!r}"
            )

        # Bot Framework credentials. Optional for local Emulator dev;
        # required for real Microsoft Teams. `bot_tenant_id` is only
        # needed when the bot is registered as Single Tenant in Azure AD.
        bot_app_id = os.getenv("BOT_APP_ID", "").strip()
        bot_app_password = os.getenv("BOT_APP_PASSWORD", "").strip()
        bot_tenant_id = os.getenv("BOT_TENANT_ID", "").strip()

        # Data lake credentials. Optional at config load time so scripts
        # that don't need data access (e.g. hello.py) still work, but the
        # data layer raises a clear error if it's empty when accessed.
        azure_conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()

        return cls(
            anthropic_api_key=api_key,
            log_level=log_level,
            bot_app_id=bot_app_id,
            bot_app_password=bot_app_password,
            bot_tenant_id=bot_tenant_id,
            azure_storage_connection_string=azure_conn,
        )

    @staticmethod
    def _validate_api_key(api_key: str) -> None:
        """Validate that the API key is present and well-formed.

        We catch the placeholder explicitly because it's the #1 source of
        confusing "auth failed" errors during initial setup.
        """
        if not api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is missing. "
                "Add it to your .env file or set it in your environment."
            )
        if _PLACEHOLDER_MARKER in api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is still set to the placeholder value. "
                "Replace it with your real key from https://console.anthropic.com"
            )
        if not api_key.startswith(_API_KEY_PREFIX):
            raise ConfigError(
                f"ANTHROPIC_API_KEY looks malformed. "
                f"It should start with {_API_KEY_PREFIX!r}."
            )
