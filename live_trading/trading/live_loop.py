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
)
from live_trading.logger import get_logger
from live_trading.risk.guardian import RiskGuardian, GuardianStatus
from live_trading.signals.decision_engine import run_decision_engine, DecisionResult
from live_trading.signals.wyckoff_engine import calibrate_wyckoff, set_calibrated_config
from live_trading.mt5.connector import (
    connect, disconnect, ensure_connected,
    connect_with_retry, keepalive_mtapi,
    fetch_candles, get_account_balance, get_account_info,
    get_open_positions, get_last_completed_bar_time,
    mt5_pos_to_dict,
)
from live_trading.mt5.executor import (
    place_market_order, close_position, TradeResult
)
from live_trading.utils.state_writer import (
    write_robot_state, write_mt5_snapshot,
    read_commands, clear_command, log_trade,
)

log = get_logger()

# ── Exponential backoff constants ─────────────────────────────────────────────
_RECONNECT_MAX_DELAY = 300   # seconds — hard cap regardless of attempt count
_RECONNECT_BASE      = RECONNECT_DELAY  # first-failure delay (from config, default 30s)


class GoldScalperLive:
    def __init__(self):
        self.running: bool = True
        self.paused:  bool = False
        self.loop_count: int = 0
        self.last_bar_time: Optional[datetime] = None
        self.trade_history: List[dict] = []
        self.last_decision: Optional[DecisionResult] = None

        # Risk Guardian — initialized after mt5rest bridge connects
        self.guardian = RiskGuardian(
            daily_loss_limit_pct=DAILY_LOSS_LIMIT_PCT,
            max_drawdown_pct=MAX_DRAWDOWN_PCT,
        )
        self._last_guardian_status: Optional[GuardianStatus] = None
        self._last_acc_info: Optional[dict] = None  # cache for WAITING state writes

        # Exponential backoff state
        self._reconnect_attempts: int = 0

    # ── Entry point ───────────────────────────────────────────────────────────

    async def start(self) -> bool:
        log.info("=" * 60)
        log.info("  GoldScalperPro v4 — LIVE TRADING ENGINE (mt5rest)")
        log.info(f"  Symbol: {SYMBOL}  |  TF: {TIMEFRAME}")
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
            log.error("Could not connect to MT5 via mt5rest bridge after 3 attempts. "
                      "Check MTAPI_URL, MT5_USER, and MT5_PASSWORD.")
            self._write_state("DISCONNECTED",
                              extra={"error": "mt5rest connection failed after retries"})
            return False  # non-False return signals failure to main.py for sys.exit(1)

        # ── MTAPI keepalive task — prevents Render free-tier sleep ─────────────
        asyncio.create_task(self._keepalive_loop(), name="mtapi_keepalive")

        # ── Guardian state restore (VB-02) ───────────────────────────────────
        # Redis is the cross-service persistent copy on Render; the local file
        # remains a fallback for local/single-process deployments. If Guardian
        # was halted before restart it must remain halted, and healthy baselines
        # must also be restored so a restart cannot reset the risk window.
        # State older than 26 hours is considered stale and is discarded.
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
                    self.guardian.restore_state(_gs_data)
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
        except Exception as _gs_exc:
            log.warning(
                f"Could not read Guardian state file — cold start: {_gs_exc}"
            )

        # Initialise Guardian with live account data (must be after connect).
        # Skipped when a halted state was restored from disk — the restored
        # baselines are already active and must not be overwritten.
        if not _guardian_restored:
            acc_info = await get_account_info()
            balance  = float(acc_info.get("balance", 0))
            equity   = float(acc_info.get("equity",  0))
            if balance > 0:
                self.guardian.initialize(balance, equity)
            else:
                log.warning(
                    "Could not fetch balance for Guardian initialization — "
                    "Guardian will block trades until account data is available"
                )

        await self._calibrate_wyckoff()
        self._write_state("RUNNING")
        await self._run_loop()

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
        log.info("Entering main loop — checking every "
                 f"{BAR_CHECK_INTERVAL}s for new M5 bar …")
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

                new_bar = await self._is_new_bar()
                if new_bar:
                    self.loop_count += 1
                    log.info(f"─── Bar #{self.loop_count} "
                             f"at {new_bar.isoformat()} ───")
                    await self._on_new_bar(new_bar)
                else:
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
            await disconnect()
            self._write_state("STOPPED")
            log.info("Engine stopped.")

    # ── Bar detection ─────────────────────────────────────────────────────────

    async def _is_new_bar(self) -> Optional[datetime]:
        bar_time = await get_last_completed_bar_time(SYMBOL, TIMEFRAME)
        if bar_time is None:
            return None
        if self.last_bar_time is None or bar_time > self.last_bar_time:
            self.last_bar_time = bar_time
            return bar_time
        return None

    # ── Per-bar handler ───────────────────────────────────────────────────────

    async def _on_new_bar(self, bar_time: datetime) -> None:
        # 1. Fetch candles
        candles = await fetch_candles(SYMBOL, TIMEFRAME, CANDLE_WINDOW)
        if len(candles) < 50:
            log.warning(f"Only {len(candles)} candles returned — skipping bar")
            return

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
        write_mt5_snapshot(
            candle_time=last_c.time,
            price=last_c.close,
            regime=decision.regime,
            adx=decision.quality_filter.adx,
            atr=_snap_atr,
            smc_signal=decision.smc.smc_signal,
            trend=decision.trend.trend,
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

        # 9. ── PLACE ORDER ────────────────────────────────────────────────────
        tp_params = decision.trade_params
        log.info(
            f"🔔 SIGNAL {decision.direction}  "
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
            }
            log_trade(self.trade_history, entry_log)
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
        else:
            log.error(f"❌ Trade failed: {result.message}")

        self._write_state(
            "RUNNING", acc_info, decision, pos,
            extra=self._guardian_extra(gs),
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
