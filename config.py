"""
Configuration for LinkedIn Analyzer.
Loads environment variables from .env file.
"""
from pydantic_settings import BaseSettings
from typing import Optional
import os


class Settings(BaseSettings):
    # Server Settings
    HOST: str = "127.0.0.1"
    PORT: int = 8766

    # LinkedIn Settings
    LINKEDIN_URL: str = "https://www.linkedin.com"
    LINKEDIN_LOGIN_URL: str = "https://www.linkedin.com/login"

    # Session Storage
    SESSION_DIR: str = os.path.join(os.path.dirname(__file__), "browser_sessions")

    # Session Encryption
    # If SESSION_ENCRYPTION_KEY is empty, the key is read from SESSION_ENCRYPTION_KEY_FILE.
    # If the file does not exist, a new key is created automatically.
    SESSION_ENCRYPTION_KEY: Optional[str] = None
    SESSION_ENCRYPTION_KEY_FILE: str = os.path.expanduser("~/.linkedin-analyzer/session.key")

    # Browser Settings
    BROWSER_HEADLESS: bool = False  # Set to True for automated runs
    BROWSER_TIMEOUT: int = 60000  # milliseconds

    # MCP Settings
    MCP_SERVER_NAME: str = "linkedin-analyzer"

    class Config:
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        env_file_encoding = "utf-8"


settings = Settings()

# Ensure session directory exists
os.makedirs(settings.SESSION_DIR, exist_ok=True)
