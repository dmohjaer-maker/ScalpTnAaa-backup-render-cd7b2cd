#!/bin/bash
# Render PORT proxy — transparent wrapper for the mt5rest Docker service.
#
# ROOT CAUSE FIXED HERE:
#   Render's reverse-proxy routes all external HTTPS traffic (including the
#   /Ping health check) to the container on $PORT (typically 10000+ on the
#   free tier).  mt5rest always binds to port 80 and cannot be reconfigured
#   without patching the application.  Without this wrapper, Render's health
#   check never reaches mt5rest, the service is marked unhealthy and restarted
#   in a continuous loop, and the trading robot can never establish an MT5
#   connection.
#
# Fix strategy:
#   socat creates a lightweight TCP forwarder that listens on $PORT and
#   forwards every connection to localhost:80 where mt5rest is running.
#   This script starts the proxy in the background, then exec-s the original
#   container command so that:
#     - mt5rest starts exactly as before (no application changes)
#     - Render's health check reaches /Ping through the proxy
#     - The trading robot's MTAPI_URL works through Render's reverse proxy
#     - Signals (SIGTERM / SIGINT) are delivered directly to the mt5rest
#       process because we use exec, not a nested shell
#
# Local / Docker Compose usage:
#   When PORT is not set or equals 80, the proxy is skipped entirely so
#   local development and non-Render deployments are unaffected.

set -e

_PORT="${PORT:-80}"

if [ "$_PORT" != "80" ]; then
    echo "[render-entrypoint] Render PORT=${_PORT} detected — starting socat proxy: ${_PORT} → 80"
    # fork: handle each connection in a separate child process (concurrent requests)
    # reuseaddr: allow fast restart without "address already in use" errors
    socat TCP-LISTEN:${_PORT},fork,reuseaddr TCP:127.0.0.1:80 &
    _SOCAT_PID=$!
    echo "[render-entrypoint] socat proxy started (pid=${_SOCAT_PID})"
else
    echo "[render-entrypoint] PORT=80 — running without proxy (local / Docker Compose mode)"
fi

# Hand off to the original container command.
# exec replaces this shell with the mt5rest process so it becomes PID 1 and
# receives all OS signals directly — critical for graceful shutdown on Render.
exec "$@"
