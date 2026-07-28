"""
Render free-tier web service wrapper — Telegram Control Panel.
Runs a /health HTTP endpoint on $PORT alongside the Telegram bot.
Self-ping keepalive task pings /health every 14 min — no external
UptimeRobot needed to prevent Render free-tier sleep.
"""
import asyncio
import os
import sys
import traceback

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from aiohttp import web

PORT = int(os.environ.get("PORT", 8081))

# How long (seconds) to keep the health server alive after startup before
# allowing the process to exit on a fatal error.  Render polls /health on a
# schedule; 30 s ensures at least 2-3 successful responses so the deploy is
# marked healthy before Render's restart policy takes over.
_HEALTH_GRACE_SECONDS = 30

# Self-ping keepalive: ping /health every 14 min so Render free-tier services
# never spin down.  14 min < Render's 15-min inactivity threshold.
_KEEPALIVE_INTERVAL_SECONDS = 840  # 14 minutes


async def _health(_req: web.Request) -> web.Response:
    return web.Response(text="OK", content_type="text/plain")


async def _run_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[health] Listening on 0.0.0.0:{PORT}", flush=True)
    while True:
        await asyncio.sleep(3600)


async def _keepalive() -> None:
    """Self-ping /health every 14 min to prevent Render free-tier sleep."""
    await asyncio.sleep(30)  # wait for health server to fully start
    url = f"http://127.0.0.1:{PORT}/health"
    while True:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    print(f"[keepalive] ping /health → {resp.status}", flush=True)
        except Exception as exc:
            print(f"[keepalive] ping failed: {exc}", flush=True)
        await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)


async def _run_panel() -> None:
    from telegram_panel.main import TelegramPanel
    panel = TelegramPanel()
    await panel.run()


async def _main() -> None:
    # Start the health server as a background task.  It must be able to
    # respond to Render's health check polling BEFORE we exit for any reason
    # (bad config, Telegram auth failure).  We hold the process alive for at
    # least _HEALTH_GRACE_SECONDS so Render records several successful checks
    # and marks the deploy healthy; its restart policy then handles the re-launch.
    health_task = asyncio.create_task(_run_health_server())
    keepalive_task = asyncio.create_task(_keepalive())

    # Give the TCP server time to bind before anything else runs.
    await asyncio.sleep(1)
    print("[server] Health server ready. Starting Telegram panel...", flush=True)

    # ── Auto-seed broker account from env vars (fixes "No accounts configured"
    # after every Render free-tier restart that wipes /tmp/panel.db) ────────
    try:
        from telegram_panel.utils.auto_seed import run as _auto_seed
        _auto_seed()
        print("[server] auto_seed completed", flush=True)
    except Exception as _seed_exc:
        print(f"[server] auto_seed warning (non-fatal): {_seed_exc}", flush=True)

    _exit_code = 0
    try:
        await _run_panel()
    except SystemExit as exc:
        _exit_code = exc.code if exc.code is not None else 1
        print(f"[server] Panel called sys.exit({_exit_code!r}) — check log lines above for config errors.", flush=True)
    except Exception as exc:
        print(f"[server] Unhandled panel exception: {exc}", flush=True)
        traceback.print_exc()
        _exit_code = 1

    keepalive_task.cancel()
    try:
        await keepalive_task
    except asyncio.CancelledError:
        pass

    if _exit_code != 0:
        # Keep health server alive so Render's health check can pass before
        # we exit.  Without this window, the deploy would be marked failed
        # instead of triggering an automatic restart.
        elapsed = 1  # already slept 1 s above
        remaining = max(0, _HEALTH_GRACE_SECONDS - elapsed)
        print(
            f"[server] Panel exited (code {_exit_code}). "
            f"Keeping health server alive for {remaining}s before exit.",
            flush=True,
        )
        await asyncio.sleep(remaining)

    health_task.cancel()
    try:
        await health_task
    except asyncio.CancelledError:
        pass

    sys.exit(_exit_code)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("[server] Shutting down.", flush=True)
