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

FROM timurila/mt5rest:26.07.22-06.37

# ── Render PORT proxy ──────────────────────────────────────────────────────────
# ROOT CAUSE FIX: mt5rest always listens on port 80, but Render's reverse-proxy
# routes all external HTTPS traffic — including the /Ping health check — to the
# container on $PORT (typically 10000+ on the free tier).  Without this fix,
# Render marks the service unhealthy on every health check, restarts it in a
# continuous loop, and the trading robot can never establish an MT5 connection.
#
# Solution: install socat as a lightweight TCP forwarder.  render-entrypoint.sh
# starts  socat $PORT → 80  before exec-ing the original container command, so
# mt5rest starts exactly as before and no application-level changes are needed.
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*

# Copy the thin entrypoint wrapper that starts the socat proxy.
COPY render-entrypoint.sh /render-entrypoint.sh
RUN chmod +x /render-entrypoint.sh

# Override ENTRYPOINT with our wrapper; the original CMD is forwarded verbatim
# via exec "$@" so mt5rest starts exactly as the base image intends.
# When PORT=80 (local dev / Docker Compose), the proxy is a no-op.
ENTRYPOINT ["/render-entrypoint.sh"]

EXPOSE 80

# Health check: verify /Ping responds on the internal port 80.
# (Render's external health check goes through the socat proxy on $PORT.)
HEALTHCHECK --interval=60s --timeout=30s --start-period=120s --retries=3 \
    CMD wget -qO- http://localhost:80/Ping || exit 1
