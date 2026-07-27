"""
Render web-service wrapper — GoldScalperPro v4 Live Trading Engine.

Design: health server runs forever (process never exits); robot loop
restarts with exponential backoff on any failure.
Ping /health every 14 min (UptimeRobot free) to keep the free-tier warm.
"""
import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from aiohttp import web

PORT = int(os.environ.get("PORT", 10000))

_BACKOFF_BASE = 30
_BACKOFF_MAX  = 300
_backoff      = _BACKOFF_BASE
_robot_status = "STARTING"
_UNHEALTHY_STATUSES = {"CONFIG_ERROR", "DISCONNECTED", "ERROR", "STOPPED"}
_HEARTBEAT_MAX_AGE_SECONDS = 180


def _health_response(status: str) -> web.Response:
    normalized = status.upper()
    unhealthy = (
        normalized in _UNHEALTHY_STATUSES
        or normalized.startswith("RETRY_IN_")
    )
    return web.Response(
        status=503 if unhealthy else 200,
        text=f"OK status={status}",
        content_type="text/plain",
    )


def _heartbeat_is_fresh(value: object, now: datetime | None = None) -> bool:
    """Return whether a state heartbeat is recent enough to prove liveness."""
    if not isinstance(value, str) or not value:
        return False
    try:
        heartbeat = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        age = (current - heartbeat).total_seconds()
        return 0 <= age <= _HEARTBEAT_MAX_AGE_SECONDS
    except (TypeError, ValueError):
        return False


async def _health(_req):
    # Prefer real robot status from Redis (written by the trading engine).
    # _robot_status only distinguishes STARTING / CONNECTING / CONFIG_ERROR /
    # DISCONNECTED / RETRY_IN_Xs — once the engine is running it never
    # updates this variable (engine.start() is an infinite loop).
    try:
        from live_trading.redis_ipc import redis_read_state, redis_available
        if redis_available():
            state = redis_read_state()
            if state and "status" in state:
                # A cached RUNNING/WAITING state is not proof that the loop is
                # alive. Redis keeps it for five minutes, so a crashed or
                # hung engine could otherwise make Render keep the dead
                # service alive indefinitely.
                if not _heartbeat_is_fresh(state.get("last_heartbeat")):
                    return _health_response("DISCONNECTED")
                return _health_response(str(state["status"]))
    except Exception:
        pass
    return _health_response(_robot_status)


async def _status(_req):
    """JSON status endpoint — consumed by the Telegram panel's HTTP fallback."""
    try:
        from live_trading.redis_ipc import redis_read_state, redis_available
        if redis_available():
            state = redis_read_state()
            if state:
                return web.Response(
                    status=200,
                    text=json.dumps(state),
                    content_type="application/json",
                )
    except Exception:
        pass
    return web.Response(
        status=200,
        text=json.dumps({"status": _robot_status.lower()}),
        content_type="application/json",
    )


async def _run_health_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/status", _status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[health] Listening on 0.0.0.0:{PORT}", flush=True)
    while True:
        await asyncio.sleep(60)


async def _run_robot_once():
    global _robot_status
    from live_trading.config import MTAPI_URL, MT5_USER, MT5_PASSWORD
    from live_trading.logger import get_logger
    from live_trading.trading.live_loop import GoldScalperLive

    log = get_logger()
    missing = [v for v, val in [
        ("MT5_USER",    MT5_USER),
        ("MT5_PASSWORD", MT5_PASSWORD),
        ("MTAPI_URL",   MTAPI_URL),
    ] if not val]
    if missing:
        msg = "Missing required env vars: " + ", ".join(missing)
        print(f"[robot] {msg}", flush=True)
        _robot_status = "CONFIG_ERROR"
        raise RuntimeError(msg)

    print(f"[robot] MTAPI_URL={MTAPI_URL}  MT5_USER={MT5_USER}", flush=True)
    _robot_status = "CONNECTING"
    engine = GoldScalperLive()
    result = await engine.start()
    if result is False:
        _robot_status = "DISCONNECTED"
        raise RuntimeError("engine.start() returned False — MT5 connection failed")
    _robot_status = "STOPPED"


async def _robot_supervisor():
    global _backoff, _robot_status
    attempt = 0
    while True:
        attempt += 1
        print(f"[supervisor] Starting robot attempt #{attempt} …", flush=True)
        try:
            await _run_robot_once()
            print("[supervisor] Robot exited cleanly — scheduling restart.", flush=True)
            _backoff = _BACKOFF_BASE
        except Exception:
            wait = min(_backoff, _BACKOFF_MAX)
            _backoff = min(_backoff * 2, _BACKOFF_MAX)
            _robot_status = f"RETRY_IN_{wait}s"
            print(
                f"[supervisor] Robot error (attempt #{attempt}), retrying in {wait}s:",
                flush=True,
            )
            traceback.print_exc()
            await asyncio.sleep(wait)
        else:
            await asyncio.sleep(_BACKOFF_BASE)


async def _main():
    print(f"[server] Python {sys.version}  PORT={PORT}", flush=True)
    health = asyncio.create_task(_run_health_server())
    # Give the health server a moment to bind before loading the trading engine.
    await asyncio.sleep(1)
    supervisor = asyncio.create_task(_robot_supervisor())
    try:
        await asyncio.gather(health, supervisor)
    except Exception:
        traceback.print_exc()
        # Never exit — keep alive even if both tasks somehow die.
        print("[server] gather() raised — entering keep-alive loop", flush=True)
        while True:
            await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("[server] Keyboard interrupt — shutting down.", flush=True)
    except Exception:
        traceback.print_exc()
        # Last-resort: keep process alive even on unexpected crash.
        import time
        while True:
            time.sleep(60)
