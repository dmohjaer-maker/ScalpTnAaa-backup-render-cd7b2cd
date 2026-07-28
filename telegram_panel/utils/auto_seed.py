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
    return base64.b64encode(password.encode()).decode()


def run(db_path: str | None = None) -> None:
    """
    Seed one account from MT5_USER / MT5_PASSWORD / MT5_HOST env vars.
    No-op if any required var is missing OR if accounts already exist.
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

        count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        if count > 0:
            logger.info(f"auto_seed: {count} account(s) already exist — no seed needed")
            conn.close()
            return

        # Derive a short broker label from the server string
        broker_label = server.split("-")[0] if "-" in server else server
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

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
                _encrypt(password),
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        logger.info(
            f"auto_seed: ✅ Seeded account  login={login}  server={server}  type={acct_type}"
        )

    except Exception as exc:
        logger.error(f"auto_seed: DB error — {exc}")
