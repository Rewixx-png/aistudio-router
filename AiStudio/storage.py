"""SQLite storage shared by the CLI, gateway, and Telegram bot."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import DEFAULT_EFFORT, DEFAULT_MODEL, PACKAGE_DIR


def _now() -> int:
    return int(time.time())


class Storage:
    """Small, thread-safe-by-connection SQLite store.

    Every operation opens a short-lived connection, so the CLI, gateway, and
    bot can run in separate threads/processes without sharing a connection.
    """

    def __init__(self, path: Optional[Path] = None):
        env_path = os.environ.get("AI_STUDIO_DB")
        self.path = Path(path or env_path or (PACKAGE_DIR / "aistudio.db"))
        self.path = self.path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    # ------------------------------------------------------------------ sqlite
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()
            self._secure_permissions()

    def _secure_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.path}{suffix}")
            if path.exists():
                try:
                    path.chmod(0o600)
                except OSError:
                    pass

    def ensure_schema(self) -> None:
        with self._db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    cookies TEXT NOT NULL,
                    web_api_key TEXT,
                    model TEXT,
                    effort TEXT,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    revoked INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    account_id INTEGER,
                    model TEXT,
                    effort TEXT,
                    prompt_chars INTEGER,
                    ok INTEGER NOT NULL,
                    error TEXT,
                    created_at INTEGER NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ accounts
    @staticmethod
    def _decode_cookies(raw: Any) -> Dict[str, str]:
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return (
            {str(k): str(v) for k, v in parsed.items()}
            if isinstance(parsed, dict)
            else {}
        )

    def _account_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        cookies = self._decode_cookies(row["cookies"])
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "cookies": cookies,
            "cookie_count": len(cookies),
            "web_api_key": row["web_api_key"],
            "model": row["model"] or DEFAULT_MODEL,
            "effort": row["effort"] or DEFAULT_EFFORT,
            "is_active": bool(row["is_active"]),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
        }

    def get_account(self, account_id: int) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return self._account_from_row(row) if row else None

    def get_active_account(self) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE is_active = 1 "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return self._account_from_row(row) if row else None

    def list_accounts(self) -> List[Dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts "
                "ORDER BY is_active DESC, updated_at DESC"
            ).fetchall()
        return [self._account_from_row(row) for row in rows]

    @staticmethod
    def _normalize_name(name: Optional[str]) -> str:
        value = (name or "").strip()
        return (value or "default")[:64]

    def _upsert_account(
        self,
        name: str,
        cookies: Dict[str, str],
        web_api_key: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        set_active: bool = True,
    ) -> Dict[str, Any]:
        name = self._normalize_name(name)
        cookies_json = json.dumps(cookies, ensure_ascii=False)
        timestamp = _now()
        with self._db() as conn:
            row = conn.execute(
                "SELECT id FROM accounts WHERE name = ?", (name,)
            ).fetchone()
            if row:
                account_id = int(row["id"])
                conn.execute(
                    "UPDATE accounts SET cookies = ?, web_api_key = ?, "
                    "model = ?, effort = ?, updated_at = ? WHERE id = ?",
                    (
                        cookies_json,
                        web_api_key,
                        model or DEFAULT_MODEL,
                        effort or DEFAULT_EFFORT,
                        timestamp,
                        account_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO accounts "
                    "(name, cookies, web_api_key, model, effort, is_active, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        name,
                        cookies_json,
                        web_api_key,
                        model or DEFAULT_MODEL,
                        effort or DEFAULT_EFFORT,
                        timestamp,
                        timestamp,
                    ),
                )
                account_id = int(cursor.lastrowid)
            if set_active:
                conn.execute("UPDATE accounts SET is_active = 0")
                conn.execute(
                    "UPDATE accounts SET is_active = 1 WHERE id = ?",
                    (account_id,),
                )
        result = self.get_account(account_id)
        if result is None:
            raise RuntimeError("Не удалось сохранить профиль")
        return result

    def import_account(
        self,
        name: str,
        cookies: Dict[str, str],
        web_api_key: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        set_active: bool = True,
    ) -> Dict[str, Any]:
        return self._upsert_account(
            name, cookies, web_api_key, model, effort, set_active
        )

    def add_account(
        self,
        name: str,
        cookies: Dict[str, str],
        model: Optional[str] = None,
        effort: Optional[str] = None,
        set_active: bool = True,
    ) -> Dict[str, Any]:
        return self._upsert_account(
            name, cookies, None, model, effort, set_active
        )

    def save_active_account(
        self,
        cookies: Dict[str, str],
        web_api_key: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        active = self.get_active_account()
        if active:
            with self._db() as conn:
                conn.execute(
                    "UPDATE accounts SET cookies = ?, web_api_key = ?, "
                    "model = ?, effort = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(cookies, ensure_ascii=False),
                        web_api_key,
                        model or active["model"],
                        effort or active["effort"],
                        _now(),
                        active["id"],
                    ),
                )
            updated = self.get_account(active["id"])
            if updated is None:
                raise RuntimeError("Не удалось обновить профиль")
            return updated
        return self._upsert_account(
            "default", cookies, web_api_key, model, effort, True
        )

    def update_active_cookies(
        self, cookies: Dict[str, str]
    ) -> Optional[Dict[str, Any]]:
        active = self.get_active_account()
        if active is None:
            return self._upsert_account(
                "default", cookies, None, DEFAULT_MODEL, DEFAULT_EFFORT, True
            )
        with self._db() as conn:
            conn.execute(
                "UPDATE accounts SET cookies = ?, web_api_key = NULL, "
                "updated_at = ? WHERE id = ?",
                (json.dumps(cookies, ensure_ascii=False), _now(), active["id"]),
            )
        return self.get_account(active["id"])

    def update_active_model(
        self, model: str, effort: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        active = self.get_active_account()
        if active is None:
            return None
        with self._db() as conn:
            conn.execute(
                "UPDATE accounts SET model = ?, effort = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    model,
                    effort or active["effort"],
                    _now(),
                    active["id"],
                ),
            )
        return self.get_account(active["id"])

    def set_active_account(self, account_id: int) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT id FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if not row:
                return False
            conn.execute("UPDATE accounts SET is_active = 0")
            conn.execute(
                "UPDATE accounts SET is_active = 1, updated_at = ? WHERE id = ?",
                (_now(), account_id),
            )
        return True

    def delete_account(self, account_id: int) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, is_active FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            if row["is_active"]:
                replacement = conn.execute(
                    "SELECT id FROM accounts ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                if replacement:
                    conn.execute("UPDATE accounts SET is_active = 0")
                    conn.execute(
                        "UPDATE accounts SET is_active = 1 WHERE id = ?",
                        (int(replacement["id"]),),
                    )
        return True

    # ------------------------------------------------------------------ settings
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def delete_setting(self, key: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    # ------------------------------------------------------------------ api keys
    @staticmethod
    def _hash_key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def create_api_key(self, name: Optional[str] = None) -> Dict[str, Any]:
        full_key = f"sk-aistudio-{secrets.token_urlsafe(32)}"
        prefix = full_key[:20]
        key_name = (name or "api").strip()[:64] or "api"
        with self._db() as conn:
            cursor = conn.execute(
                "INSERT INTO api_keys (name, prefix, key_hash, created_at, revoked) "
                "VALUES (?, ?, ?, ?, 0)",
                (key_name, prefix, self._hash_key(full_key), _now()),
            )
            key_id = int(cursor.lastrowid)
        return {
            "id": key_id,
            "name": key_name,
            "key": full_key,
            "prefix": prefix,
        }

    def list_api_keys(self, include_revoked: bool = False) -> List[Dict[str, Any]]:
        query = (
            "SELECT id, name, prefix, created_at, last_used_at, revoked "
            "FROM api_keys "
        )
        if not include_revoked:
            query += "WHERE revoked = 0 "
        query += "ORDER BY id DESC"
        with self._db() as conn:
            rows = conn.execute(query).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "prefix": str(row["prefix"]),
                "created_at": int(row["created_at"]),
                "last_used_at": row["last_used_at"],
                "revoked": bool(row["revoked"]),
            }
            for row in rows
        ]

    def revoke_api_key(self, key_id: int) -> bool:
        with self._db() as conn:
            cursor = conn.execute(
                "UPDATE api_keys SET revoked = 1 WHERE id = ? AND revoked = 0",
                (key_id,),
            )
            changed = cursor.rowcount
        return changed > 0

    def validate_api_key(self, key: str) -> bool:
        if not key:
            return False
        key_hash = self._hash_key(key)
        with self._db() as conn:
            row = conn.execute(
                "SELECT id FROM api_keys WHERE key_hash = ? AND revoked = 0",
                (key_hash,),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (_now(), int(row["id"])),
            )
        return True

    def has_api_keys(self) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM api_keys WHERE revoked = 0 LIMIT 1"
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ history
    def log_generation(
        self,
        source: str,
        account_id: Optional[int],
        model: Optional[str],
        effort: Optional[str],
        prompt_chars: int,
        ok: bool,
        error: str = "",
    ) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT INTO generations "
                "(source, account_id, model, effort, prompt_chars, ok, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    account_id,
                    model,
                    effort,
                    prompt_chars,
                    1 if ok else 0,
                    (error or "")[:1000],
                    _now(),
                ),
            )


_storage: Optional[Storage] = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage
