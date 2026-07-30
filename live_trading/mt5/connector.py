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
import time as _time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import aiohttp

from live_trading.config import (
    MTAPI_URL, MT5_HOST, MT5_PORT,
    MT5_USER, MT5_PASSWORD,
    SYNC_TIMEOUT,
)
from live_trading.signals.gold_engine import OHLCV
from live_trading.logger import get_logger

log = get_logger()

# ── Module-level state ────────────────────────────────────────────────────────
_session:    Optional[aiohttp.ClientSession] = None
_connected:  bool = False
_base_url:   str  = ""
_conn_id:    str  = ""   # UUID returned by ConnectEx; passed to every call
_last_connect_time: float = 0.0   # monotonic timestamp of last successful connect()

# After a fresh ConnectEx the mt5rest bridge may take a few seconds to report
# isConnected=true on ConnectionStatus.  During this window ensure_connected()
# would wrongly declare DISCONNECTED and trigger an immediate reconnect loop.
# The grace period suppresses that false failure.
_CONNECT_GRACE_PERIOD: float = 45.0   # seconds

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
            timeout=aiohttp.ClientTimeout(total=SYNC_TIMEOUT),
        ) as resp:
            raw = await resp.text()
            log.debug(f"ConnectEx response ({resp.status}): [response received]")

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
            _last_connect_time = _time.monotonic()
            log.info(f"MT5 connected – broker: {host}  user: {user}  conn_id: {conn_id}")
            return True

    except Exception as exc:
        log.error(f"MT5 connect error: {exc}")
        _connected = False
        return False


async def disconnect() -> None:
    """Close the bridge connection and always release the HTTP session."""
    global _connected, _session, _conn_id, _base_url

    conn_id = _conn_id
    base_url = _base_url
    session = _session
    _connected = False
    _conn_id = ""
    _base_url = ""

    if conn_id and base_url and session and not session.closed:
        try:
            async with session.get(
                f"{base_url}/Disconnect",
                params={"id": conn_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status >= 400:
                    log.warning(
                        f"MT5 disconnect request returned HTTP {response.status}"
                    )
        except Exception as exc:
            log.warning(f"MT5 disconnect request failed: {exc}")

    if session and not session.closed:
        try:
            await session.close()
        except Exception as exc:
            log.warning(f"MT5 HTTP session close failed: {exc}")
    _session = None



async def keepalive_mtapi() -> bool:
    """
    Ping the mt5rest bridge to prevent Render free-tier sleep (every ~10 min).
    Returns True if the bridge responded, False otherwise.
    """
    base = MTAPI_URL.rstrip("/") if MTAPI_URL else ""
    if not base:
        return False
    try:
        sess = _get_session()
        async with sess.get(
            f"{base}/Ping",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            ok = resp.status == 200
            if ok:
                log.debug("MTAPI keepalive ping OK")
            else:
                log.warning(f"MTAPI keepalive ping returned {resp.status}")
            return ok
    except Exception as exc:
        log.warning(f"MTAPI keepalive ping failed: {exc}")
        return False


async def connect_with_retry(max_attempts: int = 5, retry_delay: float = 60.0) -> bool:
    """
    Connect to MT5, retrying up to max_attempts times.
    On Render free tier the mt5rest bridge may be sleeping and need 60-90s to cold-start.
    """
    for attempt in range(1, max_attempts + 1):
        log.info(f"MT5 connect attempt {attempt}/{max_attempts} ...")
        ok = await connect()
        if ok:
            return True
        if attempt < max_attempts:
            log.warning(
                f"MT5 connect failed (attempt {attempt}). "
                f"Waiting {retry_delay}s for mt5rest bridge to wake up ..."
            )
            await asyncio.sleep(retry_delay)
    log.error(f"MT5 connect failed after {max_attempts} attempts.")
    return False
def _invalidate_connection() -> None:
    """Mark the current conn_id as stale so the next API call triggers a fresh ConnectEx.

    Called whenever an API endpoint returns an error-shaped response that indicates
    the conn_id is no longer recognised by the mt5rest bridge (e.g. after a bridge
    restart or broker-side session timeout on Render free tier).
    """
    global _connected, _conn_id
    log.warning("MT5 conn_id is stale — invalidating connection (will reconnect on retry)")
    _connected = False
    _conn_id   = ""


async def ensure_connected(*args, **kwargs) -> bool:
    """Check live connection status; reconnect if not connected.
    Extra positional/keyword args are accepted for backward compatibility
    with callers that pass MetaAPI-style token/account/timeout arguments.
    """
    global _connected

    # Grace period: right after a fresh ConnectEx the mt5rest bridge takes a
    # few seconds to report isConnected=true on ConnectionStatus.  Trusting the
    # module flag during this window prevents a false DISCONNECTED that would
    # otherwise trigger an immediate reconnect loop on every startup.
    if (_conn_id and _connected
            and _time.monotonic() - _last_connect_time < _CONNECT_GRACE_PERIOD):
        return True

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
    """Fetch OHLCV candles via GET /PriceHistoryV2 (ISO datetime range).

    FIX: retries once with a fresh ConnectEx on stale-conn_id errors.
    """
    for attempt in range(2):
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
                    # Error-shaped response — likely stale conn_id.
                    if attempt == 0:
                        log.warning(
                            f"fetch_candles unexpected response (stale conn_id?) "
                            f"— reconnecting and retrying. Response: {str(data)[:200]}"
                        )
                        _invalidate_connection()
                        continue
                    log.error(f"fetch_candles unexpected response after reconnect: {str(data)[:300]}")
                    return []

                candles: List[OHLCV] = []
                for bar in data:
                    # Normalise time to a plain string regardless of what
                    # mt5rest serialises it as (ISO string, integer timestamp,
                    # or datetime).  OHLCV.time is typed str; a non-string here
                    # would crash candle.time.replace() in
                    # get_last_completed_bar_time() and also break the sort
                    # key when types are mixed across bars.
                    t = str(bar.get("time", ""))
                    candles.append(OHLCV(
                        time=t,
                        open=float(bar.get("openPrice",  0.0)),
                        high=float(bar.get("highPrice",  0.0)),
                        low=float(bar.get("lowPrice",    0.0)),
                        close=float(bar.get("closePrice", 0.0)),
                        volume=float(bar.get("tickVolume", bar.get("volume", 0))),
                    ))

                # Sort and deduplicate by timestamp before removing the open bar.
                # The bridge can return overlapping pages with duplicate candles;
                # feeding those into indicators shifts the entire signal window.
                candles.sort(key=lambda candle: candle.time)
                deduplicated: List[OHLCV] = []
                seen_times: set[str] = set()
                for candle in candles:
                    if candle.time in seen_times:
                        continue
                    seen_times.add(candle.time)
                    deduplicated.append(candle)
                candles = deduplicated

                # Drop the last bar (may be the still-open current bar)
                if candles:
                    candles = candles[:-1]

                # Return only the last `count` completed bars
                return candles[-count:] if len(candles) > count else candles

        except Exception as exc:
            if attempt == 0:
                log.warning(f"fetch_candles error (attempt 1) — reconnecting: {exc}")
                _invalidate_connection()
                continue
            log.error(f"fetch_candles error after reconnect: {exc}")
            return []
    return []


async def get_account_info() -> dict:
    """Fetch account balance/equity/margin from mt5rest /AccountSummary.

    FIX: retries once with a fresh ConnectEx when the bridge returns an
    error-shaped response (stale conn_id after bridge restart or broker
    session timeout).  Previously a stale conn_id caused a silent {} return
    which the panel interpreted as balance = $0.
    """
    for attempt in range(2):  # attempt 0 = normal; attempt 1 = after reconnect
        if not _conn_id:
            if not await ensure_connected():
                log.error("get_account_info: not connected to mt5rest bridge")
                return {}
        try:
            sess = _get_session()
            async with sess.get(
                f"{_base_url}/AccountSummary",
                params={"id": _conn_id},
                timeout=aiohttp.ClientTimeout(total=30),
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
                        "leverage":    int(data.get("leverage") or 0),
                        # Identity fields — present in most mt5rest AccountSummary responses.
                        # These allow the Telegram panel to display broker/login even when the
                        # local SQLite DB was wiped (e.g. Render free-tier /tmp reset).
                        "broker":      str(data.get("broker") or data.get("company") or ""),
                        "server":      str(data.get("server") or ""),
                        "login":       str(data.get("login") or data.get("account") or ""),
                        "name":        str(data.get("name") or ""),
                    }
                # Error response — likely a stale conn_id (bridge restart / broker timeout).
                # Invalidate the connection and retry once with a fresh ConnectEx.
                if attempt == 0:
                    log.warning(
                        f"AccountSummary returned unexpected response "
                        f"(stale conn_id?) — reconnecting and retrying. "
                        f"Response: {str(data)[:200]}"
                    )
                    _invalidate_connection()
                    continue
                log.error(f"AccountSummary failed after reconnect: {str(data)[:200]}")
                return {}
        except Exception as exc:
            if attempt == 0:
                log.warning(f"get_account_info error (attempt 1) — reconnecting: {exc}")
                _invalidate_connection()
                continue
            log.error(f"get_account_info error after reconnect: {exc}")
            return {}
    return {}


async def get_account_balance() -> float:
    info = await get_account_info()
    return float(info.get("balance", 0.0))


async def get_open_positions(symbol: str = "") -> List[dict]:
    """Fetch open positions from mt5rest.

    Raises RuntimeError when mt5rest returns an error response (dict with
    code/stackTrace) so callers treat a bridge error as a connection failure
    rather than silently assuming zero open positions.  Returning [] on an
    error could cause duplicate-entry: the robot sees 0 positions and opens
    a second trade on top of an existing one.

    FIX: retries once with a fresh ConnectEx on stale-conn_id errors.
    """
    for attempt in range(2):
        if not _conn_id:
            if not await ensure_connected():
                raise RuntimeError("get_open_positions: not connected to mt5rest bridge")
        try:
            params: dict = {"id": _conn_id}
            if symbol:
                params["symbol"] = symbol
            async with _get_session().get(
                f"{_base_url}/OpenedOrders",
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                # If the bridge returns an error dict on attempt 0, the conn_id is likely
                # stale — invalidate and retry rather than raising immediately.
                if attempt == 0 and isinstance(data, dict) and "stackTrace" in data:
                    log.warning(
                        f"OpenedOrders error (stale conn_id?) — reconnecting and retrying. "
                        f"Response: {str(data)[:200]}"
                    )
                    _invalidate_connection()
                    continue
                return _parse_open_positions_response(data, resp.status)
        except RuntimeError:
            raise
        except Exception as exc:
            if attempt == 0:
                log.warning(f"get_open_positions error (attempt 1) — reconnecting: {exc}")
                _invalidate_connection()
                continue
            log.error(f"get_open_positions error after reconnect: {exc}")
            raise RuntimeError(f"get_open_positions failed: {exc}") from exc
    raise RuntimeError("get_open_positions: failed after reconnect attempt")


def _parse_open_positions_response(data: object, status: int) -> List[dict]:
    """Accept only a successful list response from OpenedOrders.

    Any other payload is an unknown position state. Returning an empty list for
    an error-shaped or malformed response could allow a duplicate entry.
    """
    if status < 200 or status >= 300:
        message = (
            data.get("message", f"HTTP {status}")
            if isinstance(data, dict)
            else f"HTTP {status}"
        )
        raise RuntimeError(f"mt5rest OpenedOrders error (HTTP {status}): {message}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        message = data.get("message", "unexpected object response")
    else:
        message = f"unexpected response type: {type(data).__name__}"
    raise RuntimeError(f"mt5rest OpenedOrders error: {message}")


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
