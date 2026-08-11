"""
Session Manager for Playwright browser sessions.
Handles saving and loading of LinkedIn browser sessions (cookies, local storage).
"""
import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken
from playwright.async_api import BrowserContext

from config import settings

ENCRYPTED_FORMAT = "fernet-v1"


class SessionManager:
    """Manages browser session persistence for LinkedIn."""

    def __init__(self, session_name: str = "linkedin_session"):
        self.session_name = session_name
        self.session_file = Path(settings.SESSION_DIR) / f"{session_name}.json"
        self._fernet: Optional[Fernet] = None
        self._fernet_checked = False

    def _resolve_secret(self) -> Optional[str]:
        configured_secret = (settings.SESSION_ENCRYPTION_KEY or "").strip()
        if configured_secret:
            return configured_secret

        key_file = Path(settings.SESSION_ENCRYPTION_KEY_FILE).expanduser()
        try:
            if key_file.exists():
                existing_secret = key_file.read_text(encoding="utf-8").strip()
                if existing_secret:
                    return existing_secret

            key_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(key_file.parent, 0o700)
            except OSError:
                pass

            generated_secret = secrets.token_urlsafe(48)
            key_file.write_text(generated_secret + "\n", encoding="utf-8")
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
            return generated_secret
        except Exception as exc:
            print(f"WARNING: Could not read/create session key file ({key_file}): {exc}")
            return None

    def _get_fernet(self) -> Optional[Fernet]:
        if self._fernet_checked:
            return self._fernet

        self._fernet_checked = True
        secret = self._resolve_secret()
        if not secret:
            self._fernet = None
            return None

        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        self._fernet = Fernet(key)
        return self._fernet

    def _is_encrypted_payload(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        meta = payload.get("_meta")
        if not isinstance(meta, dict):
            return False
        return meta.get("format") == ENCRYPTED_FORMAT and isinstance(payload.get("ciphertext"), str)

    def _encode_payload(self, session_data: Dict[str, Any]) -> Dict[str, Any]:
        fernet = self._get_fernet()
        if not fernet:
            return session_data

        plaintext = json.dumps(session_data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ciphertext = fernet.encrypt(plaintext).decode("utf-8")
        return {
            "_meta": {
                "format": ENCRYPTED_FORMAT,
                "encrypted": True,
            },
            "ciphertext": ciphertext,
        }

    def _decode_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_encrypted_payload(payload):
            return payload

        fernet = self._get_fernet()
        if not fernet:
            raise RuntimeError(
                "Session file is encrypted but no decryption key is available. "
                "Set SESSION_ENCRYPTION_KEY or restore SESSION_ENCRYPTION_KEY_FILE."
            )

        try:
            plaintext = fernet.decrypt(payload["ciphertext"].encode("utf-8"))
        except InvalidToken as exc:
            raise RuntimeError("Session decryption failed. Invalid key or corrupted session file.") from exc

        decoded = json.loads(plaintext.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError("Session file payload is invalid.")
        return decoded

    def _write_session_file(self, session_data: Dict[str, Any]) -> None:
        payload = self._encode_payload(session_data)
        with open(self.session_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    async def save_session(self, context: BrowserContext) -> bool:
        """
        Save current browser session (cookies and storage state) to file.

        Args:
            context: Playwright BrowserContext to save

        Returns:
            bool: True if saved successfully
        """
        try:
            storage_state = await context.storage_state()

            cookies = storage_state.get("cookies", [])
            # Safety: never persist a logged-out / bot-flagged session. LinkedIn
            # auth requires the `li_at` session cookie; without it we would
            # overwrite a good session with an authwall/perimeterx state.
            has_li_at = any(
                c.get("name") == "li_at" and "linkedin.com" in c.get("domain", "")
                for c in cookies
            )
            if not has_li_at:
                print("WARNING: No li_at cookie found – NOT overwriting session file.")
                return False

            session_data = {
                "cookies": cookies,
                "origins": storage_state.get("origins", []),
            }

            self._write_session_file(session_data)

            print(f"Session saved to: {self.session_file}")
            if self._get_fernet():
                print("Session file is encrypted at rest.")
            return True

        except Exception as e:
            print(f"Error saving session: {e}")
            return False

    async def load_session(self, context: BrowserContext) -> bool:
        """
        Load previously saved browser session.

        Args:
            context: Playwright BrowserContext to restore session in

        Returns:
            bool: True if session loaded successfully
        """
        try:
            if not self.session_file.exists():
                print(f"No saved session found at: {self.session_file}")
                return False

            with open(self.session_file, "r", encoding="utf-8") as f:
                raw_payload = json.load(f)

            session_data = self._decode_payload(raw_payload)

            cookies = session_data.get("cookies", [])
            if cookies:
                await context.add_cookies(cookies)
                print(f"Loaded {len(cookies)} cookies")

            # Migrate legacy plaintext file to encrypted format when possible.
            if not self._is_encrypted_payload(raw_payload) and self._get_fernet():
                self._write_session_file(session_data)
                print("Migrated plaintext session file to encrypted format.")

            return True

        except Exception as e:
            print(f"Error loading session: {e}")
            return False

    def has_valid_session(self) -> bool:
        """Check if a saved session file exists."""
        return self.session_file.exists()

    def clear_session(self) -> bool:
        """Delete saved session file."""
        try:
            if self.session_file.exists():
                self.session_file.unlink()
                print(f"Session cleared: {self.session_file}")
                return True
            return False
        except Exception as e:
            print(f"Error clearing session: {e}")
            return False

    def get_session_info(self) -> Dict[str, Any]:
        """Get information about the saved session."""
        if not self.session_file.exists():
            return {"exists": False}

        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                raw_payload = json.load(f)

            session_data = self._decode_payload(raw_payload)
            cookies = session_data.get("cookies", [])
            linkedin_cookies = [c for c in cookies if "linkedin.com" in c.get("domain", "")]

            return {
                "exists": True,
                "file": str(self.session_file),
                "encrypted": self._is_encrypted_payload(raw_payload),
                "total_cookies": len(cookies),
                "linkedin_cookies": len(linkedin_cookies),
                "cookie_names": [c.get("name") for c in linkedin_cookies[:10]],
            }
        except Exception as e:
            return {"exists": False, "error": str(e)}
