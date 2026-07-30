# mt5rest bridge — connects MT5 terminal to the trading robot via HTTP REST.
# Image: https://hub.docker.com/r/timurila/mt5rest
# The bridge listens on port 80 and exposes the following endpoints:
#   GET /Ping              — liveness probe (used by Render health check)
#   GET /ConnectEx         — authenticate and open MT5 connection
#   GET /AccountSummary    — account balance/equity
#   GET /OpenedOrders      — open positions
#   GET /PriceHistoryV2    — OHLCV candle data
#   GET /GetQuote          — current bid/ask
#   GET /Disconnect        — close connection
#
# Required env vars (set in render.yaml / Render dashboard):
#   MT5_HOST      — broker server name (e.g. AMarkets-Demo)
#   MT5_PORT      — broker port (default: 443)
#   MT5_USER      — MT5 account login number
#   MT5_PASSWORD  — MT5 account password

# ── Stage 1: obtain a known-good socat binary ─────────────────────────────────
# We build socat in a clean Ubuntu image instead of running apt-get inside the
# mt5rest base image (which is a 2022 Wine image whose apt repos may be stale).
# The socat binary and its runtime libraries are then copied into the final
# image — no apt-get is ever run inside the mt5rest layer.
FROM ubuntu:22.04 AS socat-source
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends socat libwrap0 \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: final image ──────────────────────────────────────────────────────
FROM timurila/mt5rest:26.07.22-06.37

# Copy socat and its shared-library dependency from the builder stage.
# Both mt5rest (Wine/Ubuntu base) and the builder run on glibc/amd64 so the
# binary and libs are ABI-compatible.
COPY --from=socat-source /usr/bin/socat          /usr/local/bin/socat
COPY --from=socat-source /usr/lib/x86_64-linux-gnu/libwrap.so.0 \
                                                  /usr/lib/x86_64-linux-gnu/libwrap.so.0

# ── Render PORT proxy entrypoint ──────────────────────────────────────────────
# ROOT CAUSE FIX: mt5rest always listens on port 80, but Render's reverse-proxy
# routes all external HTTPS traffic — including the /Ping health check — to the
# container on $PORT (typically 10000+ on the free tier).  Without this fix,
# Render marks goldscalper-mtapi unhealthy on every check, restarts it in a
# continuous loop, and the trading robot can never establish an MT5 connection.
#
# render-entrypoint.sh starts  socat $PORT → 80  in the background, then
# exec-s the original container command so mt5rest starts exactly as before.
# When PORT=80 (local / Docker Compose), the proxy step is skipped entirely.
#
# NOTE: Render sets the build context to the repository root when dockerfilePath
# is specified without dockerContext, so the COPY path below is relative to the
# repository root — not to the mtapi-bridge/ directory.
COPY mtapi-bridge/render-entrypoint.sh /render-entrypoint.sh
RUN chmod +x /render-entrypoint.sh

# Override ENTRYPOINT with the thin wrapper; the original CMD is forwarded
# verbatim via  exec "$@"  so mt5rest initialises exactly as before.
ENTRYPOINT ["/render-entrypoint.sh"]

EXPOSE 80

# Internal health check: verifies /Ping on mt5rest's fixed port 80.
# Render's external health check reaches the service through the socat proxy.
HEALTHCHECK --interval=60s --timeout=30s --start-period=120s --retries=3 \
    CMD wget -qO- http://localhost:80/Ping || exit 1
