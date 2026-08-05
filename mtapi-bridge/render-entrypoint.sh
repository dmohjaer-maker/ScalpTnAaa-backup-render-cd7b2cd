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
#   This script starts the proxy in the background, then runs the original
#   container command in a supervised restart loop so that:
#     - mt5rest starts exactly as before (no application changes)
#     - Render's health check reaches /Ping through the proxy
#     - The trading robot's MTAPI_URL works through Render's reverse proxy
#     - Wine/dotnet transient startup failures are recovered automatically
#       without the container exiting (which Render would count as earlyExit
#       and mark the deploy as failed)
#
# Wine startup behaviour on Render free tier:
#   The mt5rest image runs a Windows .NET application under Wine.  Wine can
#   fail to initialise on the first attempt due to resource contention on
#   shared infrastructure.  The original "exec" approach caused the container
#   to exit on every Wine crash, triggering a Render deploy failure and a
#   restart loop visible in the dashboard.  The supervised loop below keeps
#   the container (PID 1 = this script) alive through transient Wine crashes,
#   restarting the child process automatically until Wine initialises
#   successfully and /Ping begins responding.
#
# Local / Docker Compose usage:
#   When PORT is not set or equals 80, the proxy is skipped entirely so
#   local development and non-Render deployments are unaffected.

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

# ── Supervised restart loop ───────────────────────────────────────────────────
# Keep PID 1 (this script) alive so Render never sees an earlyExit.
# On SIGTERM/SIGINT (graceful shutdown from Render), forward the signal to the
# running child and exit cleanly.

_CHILD_PID=""

_shutdown() {
    echo "[render-entrypoint] Shutdown signal received — stopping mt5rest (pid=${_CHILD_PID})"
    if [ -n "$_CHILD_PID" ]; then
        kill "$_CHILD_PID" 2>/dev/null || true
        wait "$_CHILD_PID" 2>/dev/null || true
    fi
    exit 0
}

trap _shutdown SIGTERM SIGINT

echo "[render-entrypoint] Starting mt5rest with supervised restart loop..."
while true; do
    # Run the original container command as a child (not exec) so this script
    # remains PID 1 and can catch signals and restart on failure.
    "$@" &
    _CHILD_PID=$!
    wait "$_CHILD_PID"
    _EXIT=$?
    _CHILD_PID=""

    if [ "$_EXIT" -eq 0 ]; then
        echo "[render-entrypoint] mt5rest exited cleanly (code=0) — shutting down"
        break
    fi

    echo "[render-entrypoint] mt5rest exited (code=${_EXIT}) — Wine/dotnet crash detected, restarting in 5s..."
    sleep 5
done
