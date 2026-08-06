"""
Live Trading Loop — async M5 candle-close event handler via mt5rest bridge.

Flow per tick:
  1. Wait for next M5 candle close
  2. Fetch 300 closed candles via mt5rest bridge
  3. Run decision engine (all 7 signal engines)
  4. Gate: RiskGuardian circuit breakers (daily loss / drawdown)
  5. Gate: max open positions + trade allowed + Telegram not paused
  6. Place order via mt5rest executor (with slippage control)
  7. Write robot_state.json for Telegram panel

Resilience improvements over baseline:
  • RiskGuardian — daily loss limit + peak drawdown stop, both configurable
    via env vars.  Guardian halts block trade entry without stopping the loop.
  • Exponential backoff — reconnect delay doubles on each consecutive failure
    (cap: 5 minutes) then resets to base on success.
  • Slippage control — SLIPPAGE_POINTS env var limits max fill deviation.
  • Trade-history persistence — trade log is restored from robot_state.json
    on startup, so the Telegram panel shows history after a restart.
  • Duplicate-entry safety — live MT5 position check already prevents
    double-entry (unchanged), but now also guarded by Guardian state.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

from live_trading.config import (
    SYMBOL, TIMEFRAME, CANDLE_WINDOW, RISK_PERCENT,
    MAX_OPEN_TRADES, COMMENT,
    BAR_CHECK_INTERVAL, RECONNECT_DELAY, SYNC_TIMEOUT,
    MIN_CONFIRMATIONS, USE_ATR_HIGH_VOL_FILTER,
    DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT, SLIPPAGE_POINTS,
    STATE_FILE, GUARDIAN_STATE_FILE,
    TRAIL_ENABLED, TRAIL_ACTIVATION_R, TRAIL_STEP_R,
    TRAIL_LOCK_BUFFER_R, TRAIL_ATR_GAP_MULT, TRAIL_MIN_STEP_PRICE,
    MTF_ENABLED, MTF_TIMEFRAME, MTF_CANDLE_WINDOW,
    TRADE_TIMEFRAMES,
)
from live_trading.logger import get_logger
from live_trading.risk.guardian import RiskGuardian, GuardianStatus
from live_trading.risk.trailing_stop import (
    TrailingConfig, compute_staircase_sl, should_apply, r_multiple_of,
)
from live_trading.signals.decision_engine import run_decision_engine, DecisionResult, describe_strategy
from live_trading.signals.mtf_filter import compute_mtf_bias, mtf_allows_trade, MtfBias
from live_trading.signals.wyckoff_engine import calibrate_wyckoff, set_calibrated_config
from live_trading.mt5.connector import (
    connect, disconnect, ensure_connected,
    connect_with_retry, keepalive_mtapi,
    fetch_candles, get_account_balance, get_account_info,
    get_open_positions, get_last_completed_bar_time,
    get_current_quote, mt5_pos_to_dict,
)
from live_trading.mt5.executor import (
    place_market_order, close_position, modify_position, TradeResult
)
from live_trading.utils.state_writer import (
    write_robot_state, write_mt5_snapshot,
    read_commands, clear_command, log_trade,
)

log = get_logger()

# ── Exponential backoff constants ─────────────────────────────────────────────
_RECONNECT_MAX_DELAY = 300   # seconds — hard cap regardless of attempt count
_RECONNECT_BASE      = RECONNECT_DELAY  # first-failure delay (from config, default 30s)

# How often (seconds) to refresh account info between M5 candles.
# Without this, _last_acc_info is only updated inside _on_new_bar() — once
# every 5 minutes.  Refreshing every 30 s keeps balance/equity current even
# when no new candle has fired (deposits, withdrawals, positions closed elsewhere).
_ACC_REFRESH_INTERVAL = 30.0


class GoldScalperLive:
    def __init__(self):
        self.running: bool = True
        self.paused:  bool = False
        self.loop_count: int = 0
        # Multi-TF bar tracking: one last-seen bar-time per trade timeframe.
        # Initialised to None so the first bar on every TF is always processed.
        self._last_bar_times: dict[str, Optional[datetime]] = {
            tf: None for tf in TRADE_TIMEFRAMES
        }
        self.trade_history: List[dict] = []
        self.last_decision: Optional[DecisionResult] = None

        # Risk Guardian — initialized after mt5rest bridge connects
        self.guardian = RiskGuardian(
            daily_loss_limit_pct=DAILY_LOSS_LIMIT_PCT,
            max_drawdown_pct=MAX_DRAWDOWN_PCT,
        )
        self._last_guardian_status: Optional[GuardianStatus] = None
        self._last_acc_info: Optional[dict] = None  # cache for WAITING state writes
        self._last_acc_refresh_ts: float = 0.0        # monotonic time of last between-bar refresh

        # Exponential backoff state
        self._reconnect_attempts: int = 0

        # Handle for the MTAPI keepalive background task (cancelled on stop)
        self._keepalive_task: Optional[asyncio.Task] = None

        # ── Staircase Trailing Stop ──────────────────────────────────────────
        # Toggleable at runtime via the Telegram panel's "Auto Trail" switch
        # (routed through the update_risk command — see _process_commands).
        self.trailing_enabled: bool = TRAIL_ENABLED
        self._trailing_cfg = TrailingConfig(
            enabled=TRAIL_ENABLED,
            activation_r=TRAIL_ACTIVATION_R,
            step_r=TRAIL_STEP_R,
            lock_buffer_r=TRAIL_LOCK_BUFFER_R,
            atr_gap_mult=TRAIL_ATR_GAP_MULT,
            min_step_price=TRAIL_MIN_STEP_PRICE,
        )
        # Baseline for the currently open position: its ORIGINAL entry price
        # and ORIGINAL risk distance (never mutated once a trade opens), so
        # the staircase always measures R from where the trade actually
        # started — not from wherever the stop happens to be right now.
        self._trail_baseline: Optional[dict] = None  # {"id", "direction", "entry", "risk_distance"}
        self._last_trailing_status: Optional[dict] = None  # for panel telemetry
        # Cached ATR (price units) from the last completed bar — reused by the
        # trailing engine between bars so it doesn't need its own candle fetch.
        self._last_atr: float = 0.0

    # ── Entry point ───────────────────────────────────────────────────────────

    async def start(self) -> bool:
        log.info("=" * 60)
        log.info("  GoldScalperPro v4 — LIVE TRADING ENGINE (mt5rest)")
        log.info(f"  Symbol: {SYMBOL}  |  Trade TFs: {chr(44).join(TRADE_TIMEFRAMES)} (highest first)")
        log.info(f"  Risk: {RISK_PERCENT}%  |  Max positions: {MAX_OPEN_TRADES}")
        log.info(f"  Min confirmations: {MIN_CONFIRMATIONS}")
        log.info(f"  Daily loss limit: {DAILY_LOSS_LIMIT_PCT}%  |  "
                 f"Max drawdown: {MAX_DRAWDOWN_PCT}%  |  "
                 f"Slippage: ≤{SLIPPAGE_POINTS}pts")
        log.info("=" * 60)

        self._write_state("STARTING")

        # Restore trade history from previous session (survives restarts)
        self.trade_history = self._load_trade_history()

        connected = await connect_with_retry(max_attempts=5, retry_delay=60.0)
        if not connected:
            log.error("Could not connect to MT5 via mt5rest bridge after 5 attempts. "
                      "Check MTAPI_URL, MT5_USER, and MT5_PASSWORD.")
            self._write_state("DISCONNECTED",
                              extra={"error": "mt5rest connection failed after retries"})
            return False  # non-False return signals failure to main.py for sys.exit(1)

        # ── MTAPI keepalive task — prevents Render free-tier sleep ─────────────
        # Store the handle so we can cancel it when the engine stops; without
        # this, the task becomes orphaned on every supervisor-driven restart and
        # multiple background pings accumulate across restarts.
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="mtapi_keepalive"
        )
        # Belt-and-suspenders: if any step below raises before _run_loop() is
        # entered, _run_loop()'s own finally block never executes, leaving the
        # keepalive task orphaned.  This outer try/finally guarantees cleanup
        # on every exit path from start() after the task is created.
        try:
            # ── Guardian state restore (VB-02) ───────────────────────────────────
            # Redis is the cross-service persistent copy on Render; the local file
            # remains a fallback for local/single-process deployments. If Guardian
            # was halted before restart it must remain halted, and healthy baselines
            # must also be restored so a restart cannot reset the risk window.
            # State older than 26 hours is considered stale and is discarded.
            # ROOT-CAUSE FIX: Fetch live account info FIRST so we can:
            #   a) supply account identity to the Guardian before restore_state(),
            #      which lets it reject stale state from a different account; and
            #   b) always call initialize() when restore_state() is rejected.
            acc_info = await get_account_info()
            if acc_info:
                self._last_acc_info = acc_info
                _live_login  = str(acc_info.get("login",  ""))
                _live_server = str(acc_info.get("server", ""))
                self.guardian.set_account_identity(_live_login, _live_server)

            _guardian_restored = False
            try:
                _gs_data = None
                try:
                    from live_trading.redis_ipc import (
                        redis_read_guardian_state,
                        redis_available,
                    )
                    if redis_available():
                        _gs_data = redis_read_guardian_state()
                except Exception as _redis_gs_exc:
                    log.warning(
                        f"Could not read Guardian state from Redis — "
                        f"falling back to disk: {_redis_gs_exc}"
                    )

                if _gs_data is None and os.path.exists(GUARDIAN_STATE_FILE):
                    with open(GUARDIAN_STATE_FILE, "r", encoding="utf-8") as _gsf:
                        _gs_data = json.load(_gsf)

                if _gs_data:
                    _written_at_str = _gs_data.get("written_at")
                    _state_fresh = False
                    if _written_at_str:
                        try:
                            _written_at = datetime.fromisoformat(_written_at_str)
                            if _written_at.tzinfo is None:
                                _written_at = _written_at.replace(tzinfo=timezone.utc)
                            _age_hours = (
                                (datetime.now(timezone.utc) - _written_at).total_seconds()
                                / 3600
                            )
                            if _age_hours <= 26:
                                _state_fresh = True
                            else:
                                log.warning(
                                    f"Guardian state on disk is stale "
                                    f"({_age_hours:.1f}h old, limit=26h) — cold start"
                                )
                        except Exception as _ts_exc:
                            log.warning(
                                f"Could not parse Guardian state timestamp — cold start: {_ts_exc}"
                            )
                    if _state_fresh:
                        _restored = self.guardian.restore_state(_gs_data)
                        if _restored:
                            if _gs_data.get("halted"):
                                self.paused = True
                                log.critical(
                                    "🛡️  Guardian HALT restored — trading PAUSED.  "
                                    "Use /reset_guardian in Telegram to resume."
                                )
                            else:
                                log.info(
                                    "🛡️  Guardian baseline restored — "
                                    "risk window continues across restart."
                                )
                            _guardian_restored = True
                        # else: restore_state() rejected state → fall through to initialize()
            except Exception as _gs_exc:
                log.warning(
                    f"Could not read Guardian state file — cold start: {_gs_exc}"
                )

            # Initialise Guardian with live account data when no valid persisted
            # state was found.  This also covers the case where restore_state()
            # rejected the state due to account-identity or baseline mismatch
            # (the most common cause of false "violation" alerts on a demo account).
            if not _guardian_restored:
                balance = float((acc_info or {}).get("balance", 0))
                equity  = float((acc_info or {}).get("equity",  0))
                if balance > 0:
                    self.guardian.initialize(balance, equity)
                else:
                    # Re-fetch if the first attempt returned empty/no data
                    _retry_acc = await get_account_info()
                    if _retry_acc:
                        self._last_acc_info = _retry_acc
                        balance = float(_retry_acc.get("balance", 0))
                        equity  = float(_retry_acc.get("equity",  0))
                    if balance > 0:
                        self.guardian.initialize(balance, equity)
                    else:
                        log.warning(
                            "Could not fetch balance for Guardian initialization — "
                            "Guardian will block trades until account data is available"
                        )

            await self._calibrate_wyckoff()
            # Write RUNNING state immediately after connect with real account data
            # so the panel shows live balance before the first bar fires.
            self._write_state("RUNNING", self._last_acc_info)
            await self._run_loop()
        finally:
            # Cancel the keepalive task if it is still running.  _run_loop()'s
            # own finally already handles the normal exit path; this block only
            # fires when start() exits before _run_loop() is called (e.g. an
            # unexpected exception in _calibrate_wyckoff or get_account_info).
            # Cancelling an already-done task is a safe no-op.
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                try:
                    await self._keepalive_task
                except asyncio.CancelledError:
                    pass
                log.debug("MTAPI keepalive task cancelled in start() finally.")

    # ── Wyckoff calibration ───────────────────────────────────────────────────

    async def _calibrate_wyckoff(self) -> None:
        log.info("Calibrating Wyckoff config from live data …")
        candles = await fetch_candles(SYMBOL, TIMEFRAME, 500)
        if candles:
            cfg = calibrate_wyckoff(candles)
            set_calibrated_config(cfg)
            log.info(f"Wyckoff calibrated — "
                     f"maxRangePct={cfg.max_range_pct:.5f}  "
                     f"springMargin={cfg.spring_margin:.2f}")
        else:
            log.warning("Could not fetch candles for Wyckoff calibration; "
                        "using defaults")

    # ── Main async loop ───────────────────────────────────────────────────────

    async def _keepalive_loop(self) -> None:
        """Ping mt5rest bridge every 10 minutes to prevent Render free-tier sleep."""
        _INTERVAL = 600  # 10 minutes
        while True:
            await asyncio.sleep(_INTERVAL)
            try:
                await keepalive_mtapi()
            except Exception:
                pass

    async def _run_loop(self) -> None:
        log.info(
            f"Entering main loop — checking every {BAR_CHECK_INTERVAL}s "
            f"across {len(TRADE_TIMEFRAMES)} timeframes: "
            + ", ".join(TRADE_TIMEFRAMES) + " …"
        )
        try:
            while self.running:
                await self._process_commands()

                if self.paused:
                    await asyncio.sleep(BAR_CHECK_INTERVAL)
                    continue

                # ── Reconnect with exponential backoff ────────────────────────
                ok = await ensure_connected(
                    SYNC_TIMEOUT,
                    attempt=self._reconnect_attempts + 1,
                )
                if not ok:
                    self._reconnect_attempts += 1
                    backoff = min(
                        _RECONNECT_BASE * (2 ** (self._reconnect_attempts - 1)),
                        _RECONNECT_MAX_DELAY,
                    )
                    log.error(
                        f"Reconnect failed (attempt #{self._reconnect_attempts}) "
                        f"— backing off {backoff:.0f}s …"
                    )
                    self._write_state("DISCONNECTED")
                    await asyncio.sleep(backoff)
                    continue

                # Successful (re)connect — reset backoff counter
                if self._reconnect_attempts > 0:
                    log.info(
                        f"✅ Reconnected after {self._reconnect_attempts} attempt(s)"
                    )
                    self._reconnect_attempts = 0

                # Staircase trailing stop — checked every tick (not just on
                # candle close) so it reacts within seconds of price crossing
                # a step, not up to 5 minutes late.
                await self._manage_trailing_stop()

                new_bars = await self._check_new_bars()
                if new_bars:
                    for _tf, _bar_time in new_bars:
                        self.loop_count += 1
                        log.info(
                            f"─── Bar #{self.loop_count} [{_tf}] "
                            f"at {_bar_time.isoformat()} ───"
                        )
                        await self._on_new_bar(_bar_time, _tf)
                else:
                    # Refresh account info every _ACC_REFRESH_INTERVAL seconds
                    # so the panel shows current balance/equity between candles.
                    now_ts = asyncio.get_event_loop().time()
                    if now_ts - self._last_acc_refresh_ts >= _ACC_REFRESH_INTERVAL:
                        try:
                            fresh = await get_account_info()
                            if fresh and "balance" in fresh and "equity" in fresh:
                                self._last_acc_info = fresh
                                self._last_acc_refresh_ts = now_ts
                                log.debug(
                                    f"Between-bar acc refresh: "
                                    f"balance={fresh['balance']:.2f}  "
                                    f"equity={fresh['equity']:.2f}"
                                )
                        except Exception as _acc_err:
                            log.debug(f"Between-bar acc refresh skipped: {_acc_err}")
                    self._write_state("WAITING", self._last_acc_info)

                await asyncio.sleep(BAR_CHECK_INTERVAL)

        except asyncio.CancelledError:
            log.info("Loop cancelled")
        except Exception as exc:
            log.exception(f"Fatal error in main loop: {exc}")
            self._write_state("ERROR", extra={"error": str(exc)})
            raise  # Re-raise to supervisor — do NOT sys.exit() here.
            # sys.exit() kills the *entire* process and bypasses the supervisor's
            # exponential-backoff restart logic.  Raising lets the supervisor in
            # server.py catch the exception, apply the configured backoff, and
            # restart the engine without Render having to restart the container.
        finally:
            # Cancel the MTAPI keepalive background task so it does not remain
            # orphaned when the supervisor restarts the engine inside the same
            # asyncio event loop.  Multiple orphaned tasks would fire redundant
            # pings every 10 min and accumulate across restarts indefinitely.
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                try:
                    await self._keepalive_task
                except asyncio.CancelledError:
                    pass
                log.debug("MTAPI keepalive task cancelled.")
            await disconnect()
            self._write_state("STOPPED")
            log.info("Engine stopped.")

    # ── Bar detection ─────────────────────────────────────────────────────────

    async def _check_new_bars(self) -> list[tuple[str, datetime]]:
        """Poll every configured trade timeframe and return a list of
        (timeframe, bar_time) pairs for every TF that has a new completed bar
        since the last tick.  Results preserve TRADE_TIMEFRAMES order, which
        is already sorted highest-first, so M20 signals are processed before
        M15, then M10, then M5.  If M20 opens a position, the M15/M10/M5
        handlers in the same tick will see it via get_open_positions() and
        skip entry, preventing duplicate positions.
        Never raises — individual TF errors are logged and skipped."""
        results: list[tuple[str, datetime]] = []
        for tf in TRADE_TIMEFRAMES:
            try:
                bt = await get_last_completed_bar_time(SYMBOL, tf)
                if bt is None:
                    continue
                prev = self._last_bar_times.get(tf)
                if prev is None or bt > prev:
                    self._last_bar_times[tf] = bt
                    results.append((tf, bt))
            except Exception as _bar_err:
                log.warning(f"[{tf}] Bar time check failed: {_bar_err}")
        return results

    # ── Per-bar handler ───────────────────────────────────────────────────────

    async def _on_new_bar(self, bar_time: datetime, tf: str = TIMEFRAME) -> None:
        # 1. Fetch candles for this timeframe (M5 / M10 / M15 / M20)
        candles = await fetch_candles(SYMBOL, tf, CANDLE_WINDOW)
        if len(candles) < 50:
            log.warning(f"Only {len(candles)} candles returned — skipping bar")
            return

        # 1b. Fetch HTF candles for Multi-Timeframe filter (fail-safe: skipped on error)
        # HTF bias is computed here — before account / guardian checks — so the
        # fetch latency overlaps with the (slower) account info call that follows.
        # compute_mtf_bias() never raises; a bad fetch simply yields htf_bias=None.
        htf_bias: Optional[MtfBias] = None
        if MTF_ENABLED:
            try:
                htf_candles = await fetch_candles(SYMBOL, MTF_TIMEFRAME, MTF_CANDLE_WINDOW)
                if len(htf_candles) >= 50:
                    htf_bias = compute_mtf_bias(htf_candles)
                    log.info(
                        f"[{tf}] HTF ({MTF_TIMEFRAME}) bias: {htf_bias.direction}  "
                        f"trend={htf_bias.trend}  smc={htf_bias.smc_signal}  "
                        f"regime={htf_bias.regime}  strength={htf_bias.strength}"
                    )
                else:
                    log.warning(
                        f"HTF candles insufficient ({len(htf_candles)}) "
                        f"— MTF filter skipped this bar"
                    )
            except Exception as _mtf_exc:
                log.warning(f"MTF fetch/analysis error (fail-safe, skipping): {_mtf_exc}")

        # 2. Account info (live, required for Guardian)
        acc_info = await get_account_info()
        # Never continue with fabricated account values.  A failed account
        # request must block trading rather than make the risk guard appear
        # healthy and allow an order with stale/default data.
        if not acc_info or "balance" not in acc_info or "equity" not in acc_info:
            log.error("Account data unavailable — skipping this bar")
            self._write_state(
                "DISCONNECTED",
                acc_info=acc_info,
                extra={"error": "Live account data unavailable"},
            )
            return

        self._last_acc_info = acc_info  # update cache so WAITING writes show real balance
        balance  = float(acc_info["balance"])
        equity   = float(acc_info["equity"])

        # 3. ── RISK GUARDIAN CHECK ────────────────────────────────────────────
        #    Must run BEFORE any position check or order placement.
        #
        # Lazy initialization: if get_account_info() failed at start() time
        # (e.g. broker was slow to respond) the Guardian was left uninitialized
        # and would block every bar forever — even after /reset_guardian, because
        # reset_halt() clears _halted but not the _initialized=False flag, so
        # check() would return halted=True again on the next poll.
        # Now that we have fresh account data, initialize the Guardian on the
        # first bar where it is still uninitialized.
        if not self.guardian.is_initialized:
            log.info(
                "Guardian was not initialized at startup — performing lazy "
                f"initialization now (balance={balance:.2f}  equity={equity:.2f})"
            )
            self.guardian.initialize(balance, equity)

        gs = self.guardian.check(balance, equity)
        self._last_guardian_status = gs

        if gs.halted:
            log.warning(
                f"🛡️  GUARDIAN HALT — no trade this bar.  "
                f"Reason: {gs.reason}  "
                f"Daily PnL: {gs.daily_pnl:+.2f} ({gs.daily_pnl_pct:+.3f}%)  "
                f"Drawdown: {gs.drawdown_pct:.3f}%"
            )
            # Auto-pause the robot so Telegram panel shows PAUSED (not RUNNING)
            if not self.paused:
                self.paused = True
                log.critical(
                    "🛡️  Robot AUTO-PAUSED by RiskGuardian.  "
                    "Use /reset_guardian in Telegram to resume."
                )
            self._write_state(
                "PAUSED", acc_info,
                extra=self._guardian_extra(gs, "GUARDIAN_HALT"),
            )
            return

        # 4. Check open positions (live MT5 — prevents duplicate entry on restart)
        # get_open_positions() raises RuntimeError if mt5rest returns an error
        # response.  Treat that as a missing position check — skip trade entry
        # for this bar rather than risking duplicate-entry or crashing the loop.
        try:
            raw_positions = await get_open_positions(SYMBOL)
        except RuntimeError as _pos_err:
            log.error(
                f"Cannot verify open positions — skipping trade entry this bar: {_pos_err}"
            )
            self._write_state("WAITING", acc_info)
            return
        pos_dicts = [mt5_pos_to_dict(p) for p in raw_positions]
        pos       = pos_dicts[0] if pos_dicts else None

        if pos:
            log.info(f"Open position: id={pos['id']}  "
                     f"dir={pos['type']}  profit={pos.get('profit', 0):.2f}")

        # 5. Run decision engine (synchronous — all heavy math)
        decision = run_decision_engine(
            candles,
            balance,
            risk_percent=RISK_PERCENT,
            min_confirmations=MIN_CONFIRMATIONS,
            use_atr_high_vol=USE_ATR_HIGH_VOL_FILTER,
        )
        self.last_decision = decision

        # 6. Write MT5 snapshot for Telegram panel
        last_c = candles[-1]
        # Compute ATR in price units using a 5-bar average True Range.
        # A single-candle TR (trs[-1]) makes the displayed ATR jump on every
        # wick, misleading the panel operator.  Using the same 5-bar window
        # as market_regime._calc_atr_values (fixed in Fix 1) keeps both
        # values consistent.
        _snap_trs = [
            max(candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low  - candles[i - 1].close))
            for i in range(1, len(candles))
        ]
        _snap_win = min(5, len(_snap_trs))
        _snap_atr = round(sum(_snap_trs[-_snap_win:]) / _snap_win, 4) if _snap_trs else 0.0
        # Cached for the staircase trailing engine, which runs between bars
        # (every BAR_CHECK_INTERVAL) and has no candle fetch of its own.
        self._last_atr = _snap_atr
        # Build normalized account_info for the snapshot (snake_case keys to
        # match what telegram_panel's mt5_service expects).
        _snap_account_info = {
            "balance":          float(acc_info.get("balance",  0.0)),
            "equity":           float(acc_info.get("equity",   0.0)),
            "margin":           float(acc_info.get("margin",   0.0)),
            "free_margin":      float(acc_info.get("freeMargin",
                                      acc_info.get("free_margin", 0.0))),
            "floating_profit":  float(acc_info.get("equity", 0.0))
                                - float(acc_info.get("balance", 0.0)),
            "currency":         acc_info.get("currency", "USD"),
            "leverage":         acc_info.get("leverage", 0),
            "broker":           acc_info.get("broker", ""),
            "server":           acc_info.get("server", ""),
            "login":            acc_info.get("login",  ""),
            "connection_status": "connected",
        }
        # today_profit: sum of realised profits from trades closed today.
        _snap_today = __import__("datetime").date.today().isoformat()
        _snap_today_profit = round(sum(
            float(t.get("profit", 0.0))
            for t in self.trade_history
            if isinstance(t, dict) and t.get("profit") is not None
            and str(t.get("logged_at") or t.get("bar_time") or "").startswith(
                _snap_today
            )
        ), 2)
        write_mt5_snapshot(
            candle_time=last_c.time,
            price=last_c.close,
            regime=decision.regime,
            adx=decision.quality_filter.adx,
            atr=_snap_atr,
            smc_signal=decision.smc.smc_signal,
            trend=decision.trend.trend,
            # FIX: Full account data so the panel shows real balance, not USD 0.00
            account_info=_snap_account_info,
            open_positions=pos_dicts,
            recent_trades=self.trade_history[-20:],
            today_profit=_snap_today_profit,
            floating_profit=_snap_account_info["floating_profit"],
            drawdown={
                "current_percent": float(gs.drawdown_pct),
                "max_percent":     float(gs.max_drawdown_pct),
            },
        )

        # 7. Gate: max positions
        if len(raw_positions) >= MAX_OPEN_TRADES:
            log.info(f"Max positions ({MAX_OPEN_TRADES}) open — skipping entry")
            self._write_state(
                "HOLDING", acc_info, decision, pos,
                extra=self._guardian_extra(gs),
            )
            return

        # 8. Gate: decision engine
        if not decision.allowed:
            reasons = " | ".join(decision.blocked_reasons or ["No signal"])
            log.info(f"No trade → {reasons}")
            self._write_state(
                "SCANNING", acc_info, decision, pos,
                extra=self._guardian_extra(gs),
            )
            return

        # 8b. Gate: Multi-Timeframe alignment
        # Only runs when decision.allowed=True (we never block an already-rejected
        # trade with extra noise).  mtf_allows_trade() is a pure function that
        # never raises and returns (True, "") when htf_bias is None or NEUTRAL.
        if MTF_ENABLED and htf_bias is not None:
            _mtf_ok, _mtf_reason = mtf_allows_trade(htf_bias, decision.direction)
            if not _mtf_ok:
                log.info(f"⛔  {_mtf_reason}")
                _mtf_extra = {
                    **self._guardian_extra(gs),
                    "htf_bias": {
                        "direction": htf_bias.direction,
                        "trend":     htf_bias.trend,
                        "smc":       htf_bias.smc_signal,
                        "regime":    htf_bias.regime,
                        "strength":  htf_bias.strength,
                        "reasoning": htf_bias.reasoning,
                        "blocked":   _mtf_reason,
                    },
                }
                self._write_state("SCANNING", acc_info, decision, pos, extra=_mtf_extra)
                return

        # 9. ── PLACE ORDER ────────────────────────────────────────────────────
        tp_params = decision.trade_params
        log.info(
            f"🔔 SIGNAL [{tf}] {decision.direction}  "
            f"conf={decision.confidence:.1f}%  "
            f"lot={tp_params.lot_size}  "
            f"SL={tp_params.stop_loss}  TP={tp_params.take_profit}  "
            f"R:R={tp_params.risk_reward_ratio:.2f}  "
            f"slippage≤{SLIPPAGE_POINTS}pts"
        )

        result: TradeResult = await place_market_order(
            symbol    = SYMBOL,
            direction = decision.direction,
            lot_size  = tp_params.lot_size,
            sl        = tp_params.stop_loss,
            tp        = tp_params.take_profit,
            comment   = COMMENT,
            deviation = SLIPPAGE_POINTS,
        )

        if result.success:
            strategy = describe_strategy(decision)
            entry_log = {
                "position_id": result.position_id,
                "direction":   decision.direction,
                "entry":       tp_params.entry_price,
                "sl":          tp_params.stop_loss,
                "tp":          tp_params.take_profit,
                "lot":         tp_params.lot_size,
                "rr":          tp_params.risk_reward_ratio,
                "confidence":  decision.confidence,
                "grade":       decision.grade,
                "regime":      decision.regime,
                "bar_time":    bar_time.isoformat(),
                "strategy":    strategy,
            }
            log_trade(self.trade_history, entry_log)
            # Publish the "why" behind this trade, keyed by ticket, so the
            # Telegram panel's TRADE OPENED notification can explain the
            # strategy instead of showing only price/volume/SL/TP. Best
            # effort — never let a Redis hiccup affect trading itself.
            try:
                from live_trading.redis_ipc import redis_set_trade_strategy
                redis_set_trade_strategy(result.position_id, strategy)
            except Exception as exc:
                log.debug(f"Could not publish trade strategy for panel: {exc}")
            # Anchor the staircase trailing-stop baseline to this trade's
            # ORIGINAL entry price and ORIGINAL risk distance. This is set
            # exactly once, at open, and never touched again — the staircase
            # always measures its R-multiples from here, never from wherever
            # the stop has since been trailed to.
            self._trail_baseline = {
                "id":            result.position_id,
                "direction":     decision.direction,
                "entry":         tp_params.entry_price,
                "risk_distance": abs(tp_params.entry_price - tp_params.stop_loss),
            }
            # Build a synthetic position so the Telegram panel reflects the
            # newly opened trade immediately rather than waiting up to 5 min
            # for the next bar to re-fetch live positions.
            pos = {
                "id":         result.position_id,
                "ticket":     result.position_id,
                "symbol":     SYMBOL,
                "type":       decision.direction,
                "volume":     tp_params.lot_size,
                "open_price": tp_params.entry_price,
                "sl":         tp_params.stop_loss,
                "tp":         tp_params.take_profit,
                "profit":     0.0,
                "comment":    COMMENT,
            }
            # ROOT-CAUSE FIX: push the newly opened position into the live
            # Redis snapshot immediately. write_mt5_snapshot() above (step 6)
            # already ran BEFORE this order was placed, so without this the
            # panel's "open positions" view (which reads goldscalper:snapshot,
            # not goldscalper:state) would not show this trade until the next
            # M5 bar — up to 5 minutes later.
            try:
                from live_trading.redis_ipc import redis_update_snapshot_positions
                redis_update_snapshot_positions(pos_dicts + [pos])
            except Exception as _sync_exc:
                log.debug(f"Snapshot position sync skipped: {_sync_exc}")
        else:
            log.error(f"❌ Trade failed: {result.message}")

        self._write_state(
            "RUNNING", acc_info, decision, pos,
            extra=self._guardian_extra(gs),
        )

    # ── Staircase trailing stop ───────────────────────────────────────────────

    def _restore_trail_baseline(self, pos: dict) -> Optional[dict]:
        """Recover the trailing baseline for an already-open position.

        Runs when the engine (re)starts with a position already open (e.g.
        after a Render restart) and self._trail_baseline is empty. Finds the
        entry log written when this exact position was opened — that record
        was written once, before any SL modification, so it still holds the
        trade's true original risk distance.

        Falls back to the position's *current* entry/SL if no matching log
        entry survives (e.g. very old trade, log rotated away). This is an
        approximation only: if the stop had already been trailed before the
        log was lost, the recovered "risk distance" will be smaller than the
        true original — the staircase would then activate a little earlier
        than intended, but it can never move the stop backwards, so this is
        safe, just slightly more conservative.
        """
        pos_id = pos.get("id")
        for entry in reversed(self.trade_history):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("position_id")) == str(pos_id) and "sl" in entry and "entry" in entry:
                risk = abs(float(entry["entry"]) - float(entry["sl"]))
                if risk > 0:
                    return {
                        "id":            pos_id,
                        "direction":     entry.get("direction", pos.get("type", "BUY")),
                        "entry":         float(entry["entry"]),
                        "risk_distance": risk,
                    }
                break
        # Fallback: derive from the position's live snapshot.
        risk = abs(float(pos.get("open_price", 0.0)) - float(pos.get("sl", 0.0)))
        if risk > 0:
            log.warning(
                f"Trailing baseline for position {pos_id} could not be found in "
                f"trade history — approximating from live position data."
            )
            return {
                "id":            pos_id,
                "direction":     pos.get("type", "BUY"),
                "entry":         float(pos.get("open_price", 0.0)),
                "risk_distance": risk,
            }
        return None

    async def _manage_trailing_stop(self) -> None:
        """Check the open position (if any) and ratchet its SL forward.

        Runs on every loop tick (every BAR_CHECK_INTERVAL seconds) — not just
        on M5 candle close — so the stop reacts within seconds of price
        moving through a staircase step, instead of waiting up to 5 minutes.
        """
        if not self.trailing_enabled:
            return

        try:
            raw_positions = await get_open_positions(SYMBOL)
        except RuntimeError as exc:
            log.debug(f"Trailing check skipped — could not fetch positions: {exc}")
            return

        if not raw_positions:
            self._trail_baseline = None
            self._last_trailing_status = None
            return

        pos = mt5_pos_to_dict(raw_positions[0])

        if not self._trail_baseline or str(self._trail_baseline.get("id")) != str(pos["id"]):
            self._trail_baseline = self._restore_trail_baseline(pos)
        if not self._trail_baseline:
            return  # no reliable risk baseline — never trail blind

        quote = await get_current_quote(SYMBOL)
        if not quote:
            return  # no live price this tick — try again next tick

        direction = self._trail_baseline["direction"]
        # Close-side price: a BUY exits (and is stopped out) at the bid,
        # a SELL exits at the ask — trailing off the wrong side would trail
        # too aggressively by the full spread.
        current_price = quote["bid"] if direction.upper() == "BUY" else quote["ask"]

        candidate_sl = compute_staircase_sl(
            direction=direction,
            entry=self._trail_baseline["entry"],
            risk_distance=self._trail_baseline["risk_distance"],
            current_price=current_price,
            atr=self._last_atr,
            cfg=self._trailing_cfg,
        )

        r_now = r_multiple_of(
            direction, self._trail_baseline["entry"],
            self._trail_baseline["risk_distance"], current_price,
        )
        self._last_trailing_status = {
            "active":       candidate_sl is not None,
            "r_multiple":   r_now,
            "current_sl":   pos["sl"],
            "candidate_sl": candidate_sl,
        }

        if not should_apply(direction, pos["sl"], candidate_sl, self._trailing_cfg.min_step_price):
            return

        result = await modify_position(pos["id"], candidate_sl, pos["tp"])
        if result.success:
            log.info(
                f"📐 Trailing stop advanced — position {pos['id']}  "
                f"{direction}  +{r_now:.2f}R  SL {pos['sl']:.2f} → {candidate_sl:.2f}"
            )
            log_trade(self.trade_history, {
                "position_id": pos["id"],
                "action":      "TRAIL_SL",
                "direction":   direction,
                "r_multiple":  r_now,
                "old_sl":      pos["sl"],
                "new_sl":      candidate_sl,
            })
        else:
            log.warning(
                f"Trailing stop modify failed for position {pos['id']}: {result.message}"
            )

    # ── Telegram command processing ───────────────────────────────────────────

    async def _process_commands(self) -> None:
        cmds = read_commands()
        if not cmds:
            return

        # NOTE: "pause" takes priority over "resume" if both appear simultaneously
        # (e.g. two commands queued in the same JSON file between poll cycles).
        pause_applied = False
        if cmds.get("pause"):
            if not self.paused:
                self.paused = True
                log.info("⏸  Robot PAUSED by Telegram command")
                self._write_state("PAUSED")
            clear_command("pause")
            pause_applied = True

        if cmds.get("resume") and not pause_applied:
            if self.paused:
                # If Guardian is halted, don't allow resume without explicit reset
                if self.guardian.is_halted:
                    log.warning(
                        "⚠️  Cannot resume: RiskGuardian is still halted.  "
                        "Send /reset_guardian first."
                    )
                else:
                    self.paused = False
                    log.info("▶  Robot RESUMED by Telegram command")
                    self._write_state("RUNNING")
            clear_command("resume")

        if cmds.get("stop"):
            log.info("🛑 STOP command received from Telegram")
            self.running = False
            clear_command("stop")

        if cmds.get("close_all"):
            log.info("📤 Closing all positions (Telegram command)")
            await self._close_all_positions()
            clear_command("close_all")

        # New: manual Guardian reset from Telegram panel
        if cmds.get("reset_guardian"):
            log.warning("🛡️  Guardian reset requested from Telegram")
            self.guardian.reset_halt()
            if self.paused:
                self.paused = False
                log.info("▶  Robot RESUMED after Guardian reset")
                self._write_state("RUNNING")
            clear_command("reset_guardian")

        # "start" — sent by Telegram panel Start button.
        # If paused, treat as resume. If already running, log and ignore.
        if cmds.get("start") and not pause_applied:
            if self.paused:
                if self.guardian.is_halted:
                    log.warning(
                        "⚠️  Cannot start: RiskGuardian is still halted.  "
                        "Send /reset_guardian first."
                    )
                else:
                    self.paused = False
                    log.info("▶  Robot STARTED (resumed) by Telegram command")
                    self._write_state("RUNNING")
            else:
                log.info("ℹ️  START command received — robot is already running")
            clear_command("start")

        # "restart_engine" — sent by Telegram panel Restart Engine button.
        # Sets running=False so the engine loop exits cleanly; the supervisor
        # in server.py applies exponential-backoff and restarts it.
        if cmds.get("restart_engine"):
            log.info(
                "🔄 RESTART_ENGINE command received from Telegram — "
                "stopping engine for supervisor restart"
            )
            self.running = False
            clear_command("restart_engine")

        # "restart_mt5" — sent by Telegram panel Restart MT5 button.
        # Disconnects from the mt5rest bridge so the main loop reconnects
        # immediately (reconnect_attempts reset so no exponential backoff delay).
        if cmds.get("restart_mt5"):
            log.info(
                "🔌 RESTART_MT5 command received from Telegram — "
                "disconnecting for immediate reconnect"
            )
            await disconnect()
            self._reconnect_attempts = 0  # bypass exponential backoff
            self._write_state(
                "DISCONNECTED",
                extra={"info": "MT5 reconnect requested via Telegram"},
            )
            clear_command("restart_mt5")

        # "restart_telegram" — sent by Telegram panel Restart Telegram Bot button.
        # The Telegram panel service handles its own restart; the robot only
        # needs to acknowledge (clear) the command so it does not persist in Redis.
        if cmds.get("restart_telegram"):
            log.info("ℹ️  RESTART_TELEGRAM received — handled by panel service")
            clear_command("restart_telegram")
        # "update_risk" — sent by Telegram panel risk settings.
        # Payload keys (all optional): risk_percent, daily_loss_limit_pct,
        # max_drawdown_pct, slippage_points.  Each value updates the live
        # config and the Guardian thresholds without a restart.
        if cmds.get("update_risk"):
            payload = cmds["update_risk"]
            if isinstance(payload, dict):
                import live_trading.config as _live_cfg
                _g = globals()
                try:
                    if "risk_percent" in payload:
                        v = float(payload["risk_percent"])
                        _live_cfg.RISK_PERCENT = v; _g["RISK_PERCENT"] = v
                    if "daily_loss_limit_pct" in payload:
                        v = float(payload["daily_loss_limit_pct"])
                        _live_cfg.DAILY_LOSS_LIMIT_PCT = v; _g["DAILY_LOSS_LIMIT_PCT"] = v
                        self.guardian._daily_loss_limit_pct = v
                    if "max_drawdown_pct" in payload:
                        v = float(payload["max_drawdown_pct"])
                        _live_cfg.MAX_DRAWDOWN_PCT = v; _g["MAX_DRAWDOWN_PCT"] = v
                        self.guardian._max_drawdown_pct = v
                    if "slippage_points" in payload:
                        v = int(float(payload["slippage_points"]))
                        _live_cfg.SLIPPAGE_POINTS = v; _g["SLIPPAGE_POINTS"] = v
                    # "Auto Trail" switch on the Telegram panel's Risk menu —
                    # previously accepted but never actually applied anywhere.
                    if "auto_trailing" in payload:
                        v = bool(payload["auto_trailing"])
                        self.trailing_enabled = v
                        self._trailing_cfg.enabled = v
                        log.info(f"📐 Staircase trailing stop {'ENABLED' if v else 'DISABLED'} via Telegram")
                    log.info(f"🔧 Risk config updated via Telegram: {payload}")
                except Exception as _upd_err:
                    log.warning(f"update_risk payload error: {_upd_err}")
            clear_command("update_risk")

        # "update_strategy" — sent by Telegram panel strategy settings.
        # Payload keys (all optional): min_confirmations (int).
        if cmds.get("update_strategy"):
            payload = cmds["update_strategy"]
            if isinstance(payload, dict):
                import live_trading.config as _live_cfg
                _g = globals()
                try:
                    if "min_confirmations" in payload:
                        v = int(float(payload["min_confirmations"]))
                        _live_cfg.MIN_CONFIRMATIONS = v; _g["MIN_CONFIRMATIONS"] = v
                    log.info(f"🔧 Strategy config updated via Telegram: {payload}")
                except Exception as _upd_err:
                    log.warning(f"update_strategy payload error: {_upd_err}")
            clear_command("update_strategy")

    async def _close_all_positions(self) -> None:
        try:
            positions = await get_open_positions(SYMBOL)
        except RuntimeError as exc:
            log.error(f"CLOSE_ALL: could not fetch positions from mt5rest: {exc}")
            return
        for p in positions:
            d = mt5_pos_to_dict(p)
            result = await close_position(d["id"])
            if result.success:
                log_trade(self.trade_history, {
                    "position_id": d["id"],
                    "action":      "CLOSED_BY_TELEGRAM",
                    "profit":      d.get("profit"),
                })
                # ROOT-CAUSE FIX: persist trade history immediately after each
                # close so the Telegram panel shows it without waiting for the
                # next bar's write_mt5_snapshot() call.
                self._write_state("RUNNING", self._last_acc_info)

    # ── Trade history persistence (survives restarts) ─────────────────────────

    def _load_trade_history(self) -> List[dict]:
        """
        Restore trade history from the last written robot_state.json.
        This ensures the Telegram panel keeps showing history after a restart.
        No safety implications — the live MT5 position check prevents
        duplicate entries independently.
        """
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                history = state.get("recent_trades", [])
                if history:
                    log.info(
                        f"📂 Restored {len(history)} trade records "
                        f"from previous session"
                    )
                return list(history)
        except Exception as exc:
            log.warning(f"Could not restore trade history: {exc}")
        return []

    # ── Guardian state helper ─────────────────────────────────────────────────

    @staticmethod
    def _guardian_extra(
        gs: GuardianStatus,
        event: str = "",
    ) -> dict:
        """Build guardian sub-dict for injection into robot_state.json."""
        d = {
            "guardian": {
                "halted":               gs.halted,
                "reason":               gs.reason,
                "daily_pnl":            gs.daily_pnl,
                "daily_pnl_pct":        gs.daily_pnl_pct,
                "drawdown_pct":         gs.drawdown_pct,
                "equity_peak":          gs.equity_peak,
                "session_open_balance": gs.session_open_balance,
                "daily_loss_limit_pct": gs.daily_loss_limit_pct,
                "max_drawdown_pct":     gs.max_drawdown_pct,
                "triggered_at":         gs.triggered_at,
            }
        }
        if event:
            d["guardian"]["event"] = event
        return d

    # ── State writer ──────────────────────────────────────────────────────────

    def _write_state(
        self,
        status: str,
        acc_info: Optional[dict] = None,
        decision: Optional[DecisionResult] = None,
        position: Optional[dict] = None,
        extra: Optional[dict] = None,
    ) -> None:
        # Merge guardian data into extra (non-destructive)
        merged_extra: dict = {}
        if self._last_guardian_status is not None:
            merged_extra.update(
                self._guardian_extra(self._last_guardian_status)
            )
        if self._last_trailing_status is not None:
            merged_extra["trailing_stop"] = {
                "enabled": self.trailing_enabled,
                **self._last_trailing_status,
            }
        if extra:
            merged_extra.update(extra)

        write_robot_state(
            status           = status,
            decision         = decision or self.last_decision,
            open_position    = position,
            account_info     = acc_info or {},
            trade_history    = self.trade_history,
            loop_count       = self.loop_count,
            last_signal_time = (
                self.last_bar_time.isoformat() if self.last_bar_time else None
            ),
            extra = merged_extra or None,
        )

