"""
auto_seed — on every startup, if no accounts exist in the panel DB and MT5
env vars are configured, automatically insert the broker account.

WHY: Render free-tier wipes /tmp on every restart, erasing panel.db.
     Without this, users must manually re-add their account after each
     restart — which makes the panel appear broken ("No accounts configured").
"""
import os
import sqlite3
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Minimal DDL — kept in sync with storage/database.py
_ACCOUNTS_DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL,
    broker TEXT NOT NULL,
    server TEXT NOT NULL,
    login TEXT NOT NULL,
    password_encrypted TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    currency TEXT NOT NULL DEFAULT 'USD',
    leverage INTEGER NOT NULL DEFAULT 100,
    prop_firm_name TEXT,
    prop_challenge_phase TEXT,
    prop_max_daily_loss REAL,
    prop_max_total_loss REAL,
    prop_profit_target REAL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _encrypt(password: str) -> str:
    """Fernet encrypt if PANEL_ENCRYPTION_KEY is set, otherwise base64 fallback."""
    key = os.environ.get("PANEL_ENCRYPTION_KEY", "").strip()
    if key:
        try:
            from cryptography.fernet import Fernet
            return Fernet(key.encode()).encrypt(password.encode()).decode()
        except Exception as exc:
            logger.warning(f"auto_seed: Fernet encrypt failed ({exc}) — using base64 fallback")
    import base64
    # EncryptionService.decrypt() looks for the "b64:" prefix to recognise
    # legacy base64-obfuscated values.  Without the prefix the decryption path
    # falls through to Fernet (which fails with no key), returning None and
    # leaving the panel unable to read the broker password.
    return "b64:" + base64.b64encode(password.encode()).decode()


def run(db_path: str | None = None) -> None:
    """
    Seed one account from MT5_USER / MT5_PASSWORD / MT5_HOST env vars.

    Behaviour:
    - If no accounts exist → INSERT a new account (cold-start / after /tmp wipe).
    - If the seeded login already exists → UPDATE its server/password/type in case
      env vars changed between deployments (e.g. switched from demo to real).
    - If OTHER accounts exist but not this login → INSERT alongside them.
    - No-op if any required env var is missing.
    """
    db_path   = db_path or os.environ.get("PANEL_DB_PATH", "/tmp/panel.db")
    login     = os.environ.get("MT5_USER",         "").strip()
    password  = os.environ.get("MT5_PASSWORD",     "").strip()
    server    = os.environ.get("MT5_HOST",         "").strip()
    acct_type = os.environ.get("MT5_ACCOUNT_TYPE", "demo").strip().lower()

    if not login or not password or not server:
        logger.info("auto_seed: MT5_USER/MT5_PASSWORD/MT5_HOST not configured — skipping")
        return

    # Ensure parent dir exists (e.g. /tmp always exists, but just in case)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    try:
        conn = sqlite3.connect(db_path)
        conn.executescript(_ACCOUNTS_DDL)
        conn.commit()

        # Derive a short broker label from the server string (e.g. "AMarkets-Demo" → "AMarkets")
        broker_label = server.split("-")[0] if "-" in server else server
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        encrypted_pw = _encrypt(password)

        # Check if this exact login already exists
        existing = conn.execute(
            "SELECT id FROM accounts WHERE login = ?", (login,)
        ).fetchone()

        if existing:
            # UPDATE — refresh server/password/type so stale data from old deployments is fixed.
            # We do NOT touch broker name here because the live MT5 state will update it once
            # the robot connects (account_service.get_active_account enriches from MT5 info).
            conn.execute(
                """
                UPDATE accounts
                   SET server = ?, password_encrypted = ?, account_type = ?,
                       is_active = 1, is_enabled = 1, updated_at = ?
                 WHERE login = ?
                """,
                (server, encrypted_pw, acct_type, now, login),
            )
            conn.commit()
            conn.close()
            logger.info(f"auto_seed: ✅ Updated existing account  login={login}  server={server}")
            return

        # INSERT new account
        conn.execute(
            """
            INSERT INTO accounts
                (name, account_type, broker, server, login, password_encrypted,
                 is_active, is_enabled, currency, leverage, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 1, 'USD', 100, ?, ?)
            """,
            (
                f"{broker_label} – {login}",
                acct_type,
                broker_label,
                server,
                login,
                encrypted_pw,
                now,
                now,
            ),
        )
        # Make this account the active one
        conn.execute("UPDATE accounts SET is_active = 0")
        conn.execute("UPDATE accounts SET is_active = 1 WHERE login = ?", (login,))
        conn.commit()
        conn.close()
        logger.info(
            f"auto_seed: ✅ Seeded account  login={login}  server={server}  type={acct_type}"
        )

    except Exception as exc:
        logger.error(f"auto_seed: DB error — {exc}")
