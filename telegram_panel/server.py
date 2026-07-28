"""
Render worker service wrapper — Telegram Control Panel.
Runs the Telegram bot with a supervisor loop for auto-restart on crash.
Runs a /health HTTP endpoint on \$PORT for manual debugging via curl.

type: worker — workers NEVER sleep on Render's free tier.
    Telegram bots use long-polling (outbound requests to Telegram) and receive
    no inbound HTTP traffic, so 'type: web' would cause the service to spin down
    after 15 min of inactivity.  Workers run 24/7 for free.
"""
import asyncio
import os
import sys
import traceback

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from aiohttp import web

# Render workers don't provision PORT, but we keep a health server for
# manual debugging (e.g. curl /health from Render shell).
PORT = int(os.environ.get("PORT", 8081))

# Supervisor backoff: start at 10 s, double on each crash, cap at 5 min.
# Reset to base on a clean (non-error) exit.
_BACKOFF_BASE = 10
_BACKOFF_MAX  = 300


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


async def _run_panel() -> None:
    from telegram_panel.main import TelegramPanel
    panel = TelegramPanel()
    await panel.run()


async def _main() -> None:
    # Health server runs in the background for debugging; never stopped.
    asyncio.create_task(_run_health_server())
    await asyncio.sleep(1)
    print("[server] Health server ready. Starting Telegram panel...", flush=True)

    # ── Auto-seed broker account from env vars once at startup ────────────────
    # Fixes "No accounts configured" after every Render restart that wipes
    # the ephemeral /tmp/panel.db SQLite database.
    try:
        from telegram_panel.utils.auto_seed import run as _auto_seed
        _auto_seed()
        print("[server] auto_seed completed", flush=True)
    except Exception as _seed_exc:
        print(f"[server] auto_seed warning (non-fatal): {_seed_exc}", flush=True)

    # ── Supervisor loop — restart panel on any crash ──────────────────────────
    # The panel runs indefinitely; this loop ensures it restarts automatically
    # with exponential backoff rather than letting the entire worker process exit.
    _backoff = _BACKOFF_BASE
    attempt = 0
    while True:
        attempt += 1
        print(f"[supervisor] Starting panel (attempt #{attempt})...", flush=True)
        try:
            await _run_panel()
            # Panel exited cleanly (rare — normally runs forever until signal).
            print(
                f"[supervisor] Panel exited cleanly — restarting in {_BACKOFF_BASE}s...",
                flush=True,
            )
            _backoff = _BACKOFF_BASE  # reset on clean exit
        except SystemExit as exc:
            code = exc.code if exc.code is not None else 1
            if code == 0:
                print(
                    f"[supervisor] Panel sys.exit(0) — restarting in {_BACKOFF_BASE}s...",
                    flush=True,
                )
                _backoff = _BACKOFF_BASE
            else:
                print(
                    f"[supervisor] Panel sys.exit({code}) — check config/env vars above. "
                    f"Restarting in {_backoff}s...",
                    flush=True,
                )
        except Exception as exc:
            print(f"[supervisor] Panel crashed ({type(exc).__name__}): {exc}", flush=True)
            traceback.print_exc()

        await asyncio.sleep(_backoff)
        _backoff = min(_backoff * 2, _BACKOFF_MAX)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("[server] Keyboard interrupt — shutting down.", flush=True)
