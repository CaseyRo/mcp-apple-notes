"""
Settings management using pydantic-settings.

Provides type-safe, validated configuration with automatic .env file loading.
Settings are loaded once and cached for the lifetime of the application.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with validation and .env support.

    Settings are loaded from (in order of priority):
    1. Environment variables
    2. .env file in the project root
    3. Default values defined here

    Environment variables use the same names as the fields (case-insensitive).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Server authentication (inbound from MCP clients)
    apple_notes_mcp_api_key: str = Field(
        default="",
        description="API key for authenticating MCP clients.",
    )

    # NoteStore database path (for read/search tools)
    apple_notes_db_path: str = Field(
        default="~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite",
        description="Path to the NoteStore.sqlite database",
    )

    @property
    def db_path_resolved(self) -> Path:
        """Expand ~ and return the resolved NoteStore database path."""
        return Path(self.apple_notes_db_path).expanduser()

    # Server configuration
    apple_notes_mcp_host: str = Field(
        default="0.0.0.0",
        description="Host address for the MCP server to bind to",
    )
    apple_notes_mcp_port: int = Field(
        default=8010,
        ge=1,
        le=65535,
        description="Port for the MCP server to listen on",
    )

    @property
    def has_api_key(self) -> bool:
        """Check if a server API key is configured."""
        return bool(self.apple_notes_mcp_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get the application settings (cached singleton)."""
    return Settings()
