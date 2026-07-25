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
    GET  /ConnectEx        – authenticate with broker, returns UUID conn id
    GET  /Disconnect       – close connection
    GET  /ConnectionStatus – check live connection
    GET  /AccountSummary   – balance, equity, margin
    GET  /OpenedOrders     – open positions
    GET  /PriceHistoryV2   – OHLCV candles (ISO datetime range)
    GET  /GetQuote         – current bid/ask price
    GET  /Ping             – liveness probe
"""

import asyncio
from datetime import datetime, timezone, timedelta
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
_session:    Optional[aiohttp.ClientSession] = None
_connected:  bool = False
_base_url:   str  = ""
_conn_id:    str  = ""   # UUID returned by ConnectEx; passed to every call

# ── Timeframe map  (label → mt5rest integer minutes) ─────────────────────────
_TF_MAP = {
    "1m":  1,   "5m":  5,   "15m": 15,  "30m": 30,
    "1h":  60,  "4h":  240, "1d":  1440,
    "M1":  1,   "M5":  5,   "M15": 15,  "M30": 30,
    "H1":  60,  "H4":  240, "D1":  1440,
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
    Connect to MT5 via the mt5rest HTTP bridge using GET /ConnectEx.
    Returns the connection UUID which is stored in _conn_id.
    """
    global _connected, _base_url, _conn_id

    base     = MTAPI_URL.rstrip("/") if MTAPI_URL else ""
    host     = MT5_HOST
    user     = MT5_USER.strip() if MT5_USER else ""
    password = MT5_PASSWORD.strip() if MT5_PASSWORD else ""

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
        async with sess.get(
            f"{base}/ConnectEx",
            params={
                "user":     user,
                "password": password,
                "server":   host,
                "connectTimeoutSeconds": 60,
            },
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            raw = await resp.text()
            log.debug(f"ConnectEx response ({resp.status}): {raw[:200]}")

            if resp.status != 200:
                log.error(f"ConnectEx failed (status={resp.status}): {raw[:300]}")
                _connected = False
                return False

            # Response is a plain UUID string (may be quoted JSON string or raw)
            conn_id = raw.strip().strip('"')
            if not conn_id or len(conn_id) < 10:
                log.error(f"ConnectEx returned unexpected value: {raw[:200]}")
                _connected = False
                return False

            _conn_id   = conn_id
            _connected = True
            log.info(f"MT5 connected – broker: {host}  user: {user}  conn_id: {conn_id}")
            return True

    except Exception as exc:
        log.error(f"MT5 connect error: {exc}")
        _connected = False
        return False


async def disconnect() -> None:
    global _connected, _session, _conn_id
    if _conn_id and _base_url:
        try:
            sess = _get_session()
            async with sess.get(
                f"{_base_url}/Disconnect",
                params={"id": _conn_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as _:
                pass
        except Exception:
            pass
    _connected = False
    _conn_id   = ""
    if _session and not _session.closed:
        await _session.close()
    _session = None


async def ensure_connected() -> bool:
    """Check live connection status; reconnect if not connected."""
    global _connected

    if _conn_id and _base_url:
        try:
            sess = _get_session()
            async with sess.get(
                f"{_base_url}/ConnectionStatus",
                params={"id": _conn_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                if isinstance(data, dict) and data.get("isConnected"):
                    _connected = True
                    return True
        except Exception:
            pass

    _connected = False
    log.info("MT5 not connected — reconnecting …")
    return await connect()


# ── Market data ───────────────────────────────────────────────────────────────

async def fetch_candles(
    symbol: str, timeframe: str, count: int = 300
) -> List[OHLCV]:
    """Fetch OHLCV candles via GET /PriceHistoryV2 (ISO datetime range)."""
    if not _conn_id:
        if not await ensure_connected():
            return []

    tf_min = _TF_MAP.get(timeframe, 5)

    # Request slightly more bars than needed to account for the current open bar
    request_count = count + 5
    now      = datetime.now(timezone.utc)
    from_dt  = now - timedelta(minutes=tf_min * request_count)

    from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_str   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        sess = _get_session()
        async with sess.get(
            f"{_base_url}/PriceHistoryV2",
            params={
                "id":        _conn_id,
                "symbol":    symbol,
                "from":      from_str,
                "to":        to_str,
                "timeFrame": tf_min,
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json(content_type=None)

            if not isinstance(data, list):
                log.error(f"fetch_candles unexpected response: {str(data)[:300]}")
                return []

            candles: List[OHLCV] = []
            for bar in data:
                # Keep time as ISO string (matches OHLCV.time: str)
                t = bar.get("time", "")
                candles.append(OHLCV(
                    time=t,
                    open=float(bar.get("openPrice",  0.0)),
                    high=float(bar.get("highPrice",  0.0)),
                    low=float(bar.get("lowPrice",    0.0)),
                    close=float(bar.get("closePrice", 0.0)),
                    volume=float(bar.get("tickVolume", bar.get("volume", 0))),
                ))

            # Drop the last bar (may be the still-open current bar)
            if candles:
                candles = candles[:-1]

            # Return only the last `count` completed bars
            return candles[-count:] if len(candles) > count else candles

    except Exception as exc:
        log.error(f"fetch_candles error: {exc}")
        return []


async def get_account_info() -> dict:
    if not _conn_id:
        await ensure_connected()
    try:
        sess = _get_session()
        async with sess.get(
            f"{_base_url}/AccountSummary",
            params={"id": _conn_id},
        ) as resp:
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and "balance" in data:
                return {
                    "balance":     float(data.get("balance",     0.0)),
                    "equity":      float(data.get("equity",      0.0)),
                    "margin":      float(data.get("margin",      0.0)),
                    "freeMargin":  float(data.get("freeMargin",  0.0)),
                    "marginLevel": float(data.get("marginLevel", 0.0)),
                    "currency":    data.get("currency", "USD"),
                    "leverage":    data.get("leverage",  1),
                }
            log.error(f"AccountSummary unexpected response: {data}")
            return {}
    except Exception as exc:
        log.error(f"get_account_info error: {exc}")
        return {}


async def get_account_balance() -> float:
    info = await get_account_info()
    return float(info.get("balance", 0.0))


async def get_open_positions(symbol: str = "") -> List[dict]:
    if not _conn_id:
        await ensure_connected()
    try:
        params: dict = {"id": _conn_id}
        if symbol:
            params["symbol"] = symbol
        async with _get_session().get(
            f"{_base_url}/OpenedOrders", params=params
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
    if not candles:
        return None
    t = candles[-1].time
    if isinstance(t, datetime):
        return t
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except Exception:
        return None


def mt5_pos_to_dict(pos: dict) -> dict:
    """Normalise a raw mt5rest OpenedOrder dict into the standard internal format."""
    type_map = {0: "BUY", 1: "SELL"}
    raw_type = pos.get("type", pos.get("orderType", 0))
    try:
        raw_type = int(raw_type)
    except (TypeError, ValueError):
        raw_type = 0

    return {
        "id":         str(pos.get("ticket", pos.get("identifier", ""))),
        "ticket":     pos.get("ticket", pos.get("identifier", 0)),
        "symbol":     pos.get("symbol", ""),
        "type":       type_map.get(raw_type, "BUY"),
        "volume":     float(pos.get("volume", pos.get("lots", 0.0))),
        "open_price": float(pos.get("openPrice", pos.get("price_open", 0.0))),
        "sl":         float(pos.get("stopLoss",  pos.get("sl", 0.0))),
        "tp":         float(pos.get("takeProfit", pos.get("tp", 0.0))),
        "profit":     float(pos.get("profit",    0.0)),
        "open_time":  pos.get("openTime",  pos.get("time",    0)),
        "comment":    pos.get("comment",   ""),
    }


def get_connection() -> Optional[str]:
    """Return the base URL when connected, None otherwise (used by executor)."""
    return _base_url if _connected else None


def get_conn_id() -> Optional[str]:
    """Return the active connection UUID (used by executor)."""
    return _conn_id if _connected else None


def is_connected() -> bool:
    return _connected
