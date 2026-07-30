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

    EXPOSE 80

    # Render sets PORT but mt5rest listens on 80 by default; document this.
    # The robot uses MTAPI_URL=https://goldscalper-mtapi.onrender.com (no port suffix).

    # Health check: verify /Ping responds within 30s, retry every 60s.
    HEALTHCHECK --interval=60s --timeout=30s --start-period=120s --retries=3 \
    CMD wget -qO- http://localhost:80/Ping || exit 1
    