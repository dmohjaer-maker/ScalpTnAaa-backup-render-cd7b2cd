"""
Dashboard Handler — main home screen and robot control.
"""

import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes
from .base import BaseHandler
from ..keyboards.inline import Keyboards
from ..formatters.messages import MessageFormatter

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
