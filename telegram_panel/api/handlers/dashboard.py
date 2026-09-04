"""
Dashboard Handler — main home screen and robot control.
"""

import asyncio
import logging
from typing import Optional
from telegram import Update
from telegram.ext import ContextTypes
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter
from ...models.account import Account
from ...config.constants import AccountType, ConnectionStatus


def _account_from_robot_state(robot_state: dict) -> Optional[Account]:
    """Synthesize a transient Account from robot_state when no DB account exists.

    This is a display-only fallback so the dashboard account block is never
    blank just because panel.db was wiped on a Render restart.  The object is
    NOT persisted — it is only passed to the formatter.
    """
    # Prefer the richer account_info dict (written by state_writer after our fix)
    info = robot_state.get("account_info") or robot_state.get("account") or {}
    if not info:
        return None

    balance = float(info.get("balance", 0.0))
    equity  = float(info.get("equity",  0.0))
    # Only skip if we have no identifying info at all.
    # Previously we returned None whenever balance=0 — this caused the entire
    # account block to vanish while the robot was starting up or reconnecting,
    # even though broker/server/login were already available from the state.
    broker_check = str(info.get("broker") or "").strip()
    server_check = str(info.get("server") or "").strip()
    login_check  = str(info.get("login")  or info.get("name") or "").strip()
    if balance == 0.0 and equity == 0.0 and not broker_check and not server_check and not login_check:
        return None  # truly no data yet

    broker = str(info.get("broker") or "")
    server = str(info.get("server") or "")
    login  = str(info.get("login")  or info.get("name") or "")
    currency = str(info.get("currency", "USD"))
    leverage = int(info.get("leverage") or 100)

    raw_conn = info.get("connection_status", robot_state.get("connection_status", "disconnected"))
    try:
        conn_status = ConnectionStatus(str(raw_conn).lower())
    except ValueError:
        conn_status = ConnectionStatus.DISCONNECTED

    try:
        acct_type = AccountType("demo")
    except ValueError:
        acct_type = AccountType.DEMO

    return Account(
        id=None,
        name=f"{broker} – {login}" if broker and login else (broker or login or "MT5 Account"),
        account_type=acct_type,
        broker=broker or "—",
        server=server or "—",
        login=login or "—",
        balance=balance,
        equity=equity,
        margin=float(info.get("margin", 0.0)),
        free_margin=float(info.get("free_margin", info.get("freeMargin", 0.0))),
        floating_profit=float(info.get("floating_profit", equity - balance)),
        currency=currency,
        leverage=leverage,
        connection_status=conn_status,
    )

logger = logging.getLogger(__name__)


class DashboardHandler(BaseHandler):
    def __init__(
        self,
        robot_service,
        mt5_service,
        account_service,
        system_service,
        auth_middleware,
        formatter: MessageFormatter,
    ) -> None:
        self._robot = robot_service
        self._mt5 = mt5_service
        self._accounts = account_service
        self._system = system_service
        self._auth = auth_middleware
        self._fmt = formatter

    async def show_home(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, user = await self._auth.is_authorized(update)
        if not ok:
            # Answer the callback so Telegram doesn't show "An error occurred"
            if update.callback_query:
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
            return
        formatter = MessageFormatter()
        text = formatter.welcome(user.display_name if user else "User")
        await self.edit_or_reply(update, context, text, Keyboards.main_menu())

    async def show_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Answer callback immediately — prevents Telegram timeout / "An error occurred"
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except Exception:
                pass

        ok, user = await self._auth.is_authorized(update)
        if not ok:
            return

        # Gather all data concurrently; each service is individually guarded
        try:
            robot_state, account, today_profit, floating, drawdown, sys_stats = await asyncio.gather(
                self._robot.get_state(),
                self._accounts.get_active_account(),
                self._mt5.get_today_profit(),
                self._mt5.get_floating_profit(),
                self._mt5.get_drawdown(),
                self._system.get_system_stats(),
            )
        except Exception as exc:
            logger.error(f"show_dashboard gather failed: {exc}", exc_info=True)
            # Fallback: show minimal dashboard rather than crashing
            robot_state = {"status": "unknown", "active_trades": 0, "pending_orders": 0}
            account = None
            today_profit = 0.0
            floating = 0.0
            drawdown = {"current_percent": 0.0, "max_percent": 0.0}
            sys_stats = {"cpu_percent": 0.0, "ram_percent": 0.0}

        active_count = robot_state.get("active_trades", 0)
        pending_count = robot_state.get("pending_orders", 0)

        # ── Synthesize account from robot_state when DB account is missing ────
        # This handles the case where /tmp/panel.db was wiped on Render restart
        # and auto_seed didn't recreate it (e.g. MT5_USER env not set in panel).
        # The robot writes full account_info to its state; we use that as fallback.
        if account is None:
            account = _account_from_robot_state(robot_state)

        text = self._fmt.dashboard(
            robot_state=robot_state,
            account=account,
            active_trades=active_count,
            pending_orders=pending_count,
            today_profit=today_profit,
            floating_profit=floating,
            drawdown=drawdown,
            system_stats=sys_stats,
        )
        await self.edit_or_reply(update, context, text, Keyboards.dashboard())

    async def show_robot_control(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show the complete robot-control screen from the main menu."""
        ok, _ = await self._auth.check_permission(update, "can_control_robot")
        if not ok:
            return

        state = await self._robot.get_state()
        status = str(state.get("status", "unknown")).upper()
        mt5_status = str(
            state.get("mt5_status", state.get("connection_status", "unknown"))
        ).upper()
        raw_symbols = state.get("symbols") or state.get("symbol") or "—"
        if isinstance(raw_symbols, (list, tuple)):
            symbols = ", ".join(str(item) for item in raw_symbols)
        else:
            symbols = str(raw_symbols)

        text = (
            "⚙️ <b>ROBOT CONTROL</b>\n"
            "────────────────────\n"
            f"🤖 Status: <b>{status}</b>\n"
            f"🔌 MT5: <b>{mt5_status}</b>\n"
            f"💱 Symbols: <b>{symbols}</b>\n\n"
            "Use confirmations for restart and shutdown actions."
        )
        await self.edit_or_reply(update, context, text, Keyboards.robot_control())

    async def test_connection(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Run a fresh live connection test from the dashboard."""
        ok, _ = await self._auth.check_permission(update, "can_view_dashboard")
        if not ok:
            return

        account = await self._accounts.get_active_account()
        result = None
        if account and account.id is not None:
            result = await self._accounts.test_connection(account.id)

        if result is None:
            state = await self._robot.get_fresh_state()
            mt5_status = str(
                state.get("mt5_status", state.get("connection_status", "unknown"))
            ).upper()
            symbols = state.get("symbols") or state.get("symbol") or "—"
            if isinstance(symbols, (list, tuple)):
                symbols = ", ".join(str(item) for item in symbols)
            result = {
                "success": mt5_status == "CONNECTED",
                "robot_status": str(state.get("status", "unknown")).upper(),
                "mt5_status": mt5_status,
                "last_heartbeat": state.get("last_heartbeat", "—"),
                "error": "No active account record" if mt5_status != "CONNECTED" else "",
                "symbols": symbols,
            }

        if result.get("success"):
            message = (
                "✅ <b>CONNECTION TEST PASSED</b>\n\n"
                f"🤖 Robot: <b>{str(result.get('robot_status', 'running')).upper()}</b>\n"
                "🔌 MT5: <b>CONNECTED</b>\n"
                f"💱 Symbols: <b>{result.get('symbols', 'XAUUSD, EURUSD')}</b>\n"
                f"💰 Balance: <b>{result.get('currency', 'USD')} "
                f"{float(result.get('balance', 0.0)):,.2f}</b>\n"
                f"💓 Heartbeat: <b>{result.get('last_heartbeat', '—')}</b>"
            )
        else:
            message = (
                "⚠️ <b>CONNECTION TEST FAILED</b>\n\n"
                f"🤖 Robot: <b>{str(result.get('robot_status', 'unknown')).upper()}</b>\n"
                f"🔌 MT5: <b>{str(result.get('mt5_status', 'unknown')).upper()}</b>\n"
                f"💓 Heartbeat: <b>{result.get('last_heartbeat', '—')}</b>\n"
                f"❗ {result.get('error', 'Connection could not be verified')}"
            )

        await self.answer_callback(update, message, show_alert=True)

    async def handle_robot_control(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: str
    ) -> None:
        ok, user = await self._auth.check_permission(update, "can_control_robot")
        if not ok:
            return

        # Keyboard buttons send "stop_confirm", "emergency_confirm" etc.
        # Normalise to base action so confirm_needed lookup works and
        # Keyboards.confirm_action(action, label) produces "robot:<action>_confirmed"
        # which matches the action_map keys below.
        if action.endswith("_confirm"):
            action = action[: -len("_confirm")]

        # Actions that require explicit confirmation
        confirm_needed = {
            "stop": ("stop_confirmed", "Stop Robot"),
            "emergency": ("emergency_confirmed", "EMERGENCY STOP"),
            "restart_engine": ("restart_engine_confirmed", "Restart Engine"),
            "restart_mt5": ("restart_mt5_confirmed", "Restart MT5"),
            "restart_telegram": ("restart_telegram_confirmed", "Restart Telegram Bot"),
            "shutdown": ("shutdown_confirmed", "Safe Shutdown"),
        }

        if action in confirm_needed:
            confirmed_action, label = confirm_needed[action]
            await self.edit_or_reply(
                update, context,
                f"⚠️ <b>Confirm Action</b>\n\nAre you sure you want to: <b>{label}</b>?\n\n"
                f"This action will take effect immediately.",
                Keyboards.confirm_action(action, label),
            )
            return

        # Execute confirmed actions
        result = False
        action_map = {
            "start": self._robot.start,
            "pause": self._robot.pause,
            "resume": self._robot.resume,
            "stop_confirmed": self._robot.emergency_stop,
            "emergency_confirmed": self._robot.emergency_stop,
            "restart_engine_confirmed": self._robot.restart_engine,
            "restart_mt5_confirmed": self._robot.restart_mt5,
            "restart_telegram_confirmed": self._robot.restart_telegram,
            "shutdown_confirmed": self._robot.safe_shutdown,
        }

        fn = action_map.get(action)
        if fn:
            result = await fn()
            await self._auth.record_action(
                user,
                f"ROBOT_{action.upper()}",
                f"Robot control: {action}",
                success=result,
            )

        if result:
            await self.answer_callback(update, f"✅ Command sent: {action}", show_alert=False)
            await self.show_dashboard(update, context)
        else:
            await self.answer_callback(
                update, f"⚠️ Command may not have reached the robot.", show_alert=True
            )
