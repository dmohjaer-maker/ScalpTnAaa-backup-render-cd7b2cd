"""
mt5rest HTTP Connector – GoldScalperPro v4

Direct MT5 connection via the mt5rest Docker bridge (no MetaAPI cloud).
Runs fully self-hosted on Render.

Required env vars:
    MTAPI_URL     – URL of the mt5rest Docker service
                    e.g. https://goldscalper-mtapi.onrender.com
    MT5_HOST      – broker server name  (e.g. AMarkets-Demo)
    MT5_USER      – MT5 account login number
    MT5_PASSWORD  – MT5 account password

mt5rest endpoints used:
    POST /connect          – authenticate with broker
    GET  /account_info     – balance, equity, margin
    GET  /positions        – open positions
    GET  /history          – OHLCV candles
    POST /order            – place market order
    POST /close_position   – close position by ticket
    GET  /health           – liveness probe
"""

import asyncio
from datetime import datetime, timezone
from typing import List, Optional

import aiohttp

from live_trading.config import (
    MTAPI_URL, MT5_HOST, MT5_PORT,
    MT5_USER, MT5_PASSWORD,
)
from live_trading.signals.gold_engine import OHLCV
from live_trading.logger import get_logger

log = get_logger()

# ── Module-level state ────────────────────────────────────────────────────────
_session:   Optional[aiohttp.ClientSession] = None
_connected: bool = False
_base_url:  str  = ""

# ── Timeframe map  (label → mt5rest integer minutes) ─────────────────────────
_TF_MAP = {
    "1m":  1,   "5m":  5,   "15m": 15,  "30m": 30,
    "1h":  60,  "4h":  240, "1d":  1440,
    "M1":  1,   "M5":  5,   "M15": 15,  "M30": 30,
    "H1":  60,  "H4":  240, "D1":  1440,
}

_TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
    return _session


# ── Connection lifecycle ──────────────────────────────────────────────────────

async def connect(*args, **kwargs) -> bool:
    """
    Connect to MT5 via the mt5rest HTTP bridge.
    Reads credentials from env vars; positional args are ignored (compat shim).
    """
    global _connected, _base_url

    base     = MTAPI_URL.rstrip("/") if MTAPI_URL else ""
    host     = MT5_HOST
    user     = int(MT5_USER) if str(MT5_USER).isdigit() else 0
    password = MT5_PASSWORD

    if not base:
        log.error(
            "MTAPI_URL is not set. "
            "Deploy the mt5rest Docker service and set MTAPI_URL to its URL."
        )
        return False
    if not user or not password:
        log.error("MT5_USER and MT5_PASSWORD must be set.")
        return False

    _base_url = base
    sess = _get_session()

    try:
        log.info(f"Connecting to MT5 via mt5rest at {base} ...")
        async with sess.post(
            f"{base}/connect",
            json={"server": host, "login": user, "password": password},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json(content_type=None)
            log.debug(f"connect response ({resp.status}): {data}")

            # mt5rest returns {"authorized": true} on success
            if resp.status == 200 and data.get("authorized"):
                _connected = True
                log.info(f"MT5 connected – broker: {host}  login: {user}")
                return True

            log.error(f"MT5 connect rejected (status={resp.status}): {data}")
            _connected = False
            return False

    except Exception as exc:
        log.error(f"MT5 connect error: {exc}")
        _connected = False
        return False


async def disconnect() -> None:
    global _connected, _session
    _connected = False
    if _session and not _session.closed:
        await _session.close()
    _session = None
    log.info("MT5 disconnected.")


async def ensure_connected() -> bool:
    """Ping /health; reconnect automatically if the bridge is unreachable."""
    global _connected
    if _base_url:
        try:
            async with _get_session().get(
                f"{_base_url}/health",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    _connected = True
                    return True
        except Exception:
            pass
    log.warning("Bridge health-check failed – reconnecting ...")
    _connected = False
    return await connect()


# ── Market data ───────────────────────────────────────────────────────────────

async def fetch_candles(
    symbol: str,
    timeframe: str,
    count: int = 300,
) -> List[OHLCV]:
    """Fetch closed OHLCV candles from the mt5rest bridge."""
    if not _connected:
        await ensure_connected()

    tf_int = _TF_MAP.get(timeframe, 5)
    sess   = _get_session()

    try:
        async with sess.get(
            f"{_base_url}/history",
            params={"symbol": symbol, "timeframe": tf_int, "count": count + 5},
        ) as resp:
            raw = await resp.json(content_type=None)
            if not isinstance(raw, list):
                log.error(f"Unexpected candle response: {str(raw)[:300]}")
                return []

            candles: List[OHLCV] = []
            for bar in raw:
                ts = bar.get("time", 0)
                if isinstance(ts, (int, float)):
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                else:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                candles.append(OHLCV(
                    time=dt,
                    open=float(bar["open"]),
                    high=float(bar["high"]),
                    low=float(bar["low"]),
                    close=float(bar["close"]),
                    volume=float(bar.get("tick_volume", bar.get("volume", 0))),
                ))

            # Exclude the last (still-forming) candle
            return candles[:-1] if len(candles) > 1 else candles

    except Exception as exc:
        log.error(f"fetch_candles error: {exc}")
        return []


async def get_account_info() -> dict:
    if not _connected:
        await ensure_connected()
    try:
        async with _get_session().get(f"{_base_url}/account_info") as resp:
            return await resp.json(content_type=None)
    except Exception as exc:
        log.error(f"get_account_info error: {exc}")
        return {}


async def get_account_balance() -> float:
    info = await get_account_info()
    return float(info.get("balance", 0.0))


async def get_open_positions(symbol: str = "") -> List[dict]:
    if not _connected:
        await ensure_connected()
    try:
        params = {"symbol": symbol} if symbol else {}
        async with _get_session().get(
            f"{_base_url}/positions", params=params
        ) as resp:
            data = await resp.json(content_type=None)
            return data if isinstance(data, list) else []
    except Exception as exc:
        log.error(f"get_open_positions error: {exc}")
        return []


async def get_last_completed_bar_time(
    symbol: str, timeframe: str
) -> Optional[datetime]:
    candles = await fetch_candles(symbol, timeframe, count=3)
    return candles[-1].time if candles else None


def mt5_pos_to_dict(pos: dict) -> dict:
    """Normalise a raw mt5rest position dict into the standard internal format."""
    type_map = {0: "BUY", 1: "SELL"}
    return {
        "id":         str(pos.get("ticket", pos.get("identifier", ""))),
        "ticket":     pos.get("ticket", pos.get("identifier", 0)),
        "symbol":     pos.get("symbol", ""),
        "type":       type_map.get(int(pos.get("type", 0)), "BUY"),
        "volume":     float(pos.get("volume", 0.0)),
        "open_price": float(pos.get("price_open", 0.0)),
        "sl":         float(pos.get("sl", 0.0)),
        "tp":         float(pos.get("tp", 0.0)),
        "profit":     float(pos.get("profit", 0.0)),
        "open_time":  pos.get("time", 0),
        "comment":    pos.get("comment", ""),
    }


def get_connection() -> Optional[str]:
    """Return the base URL when connected, None otherwise (used by executor)."""
    return _base_url if _connected else None


def is_connected() -> bool:
    return _connected
