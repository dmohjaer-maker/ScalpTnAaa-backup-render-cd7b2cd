"""
Inline Keyboard Layouts — professional keyboard UI for all menu pages.
Every screen has a consistent back button and breadcrumb navigation.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from ...config.constants import StrategyComponent, NotificationType, RiskParameter, ICONS
from ...models.account import Account
from ...models.trade import Position, PendingOrder
from ...i18n.fa import translate


_TelegramInlineKeyboardButton = InlineKeyboardButton


def _button_style(text: str, callback_data: str | None = None) -> str:
    """Map panel actions to Telegram's supported button color styles."""
    action = f"{text} {callback_data or ''}".casefold()

    if any(
        word in action
        for word in (
            "emergency",
            "safe stop",
            "shutdown",
            "delete",
            "close_loss",
            "all_off",
            "disable",
            "block",
            "cancel",
            "❌",
            "🚨",
        )
    ):
        return "danger"

    if any(
        word in action
        for word in (
            "start",
            "resume",
            "enable",
            "confirm",
            "close_profit",
            "close_buy",
            "all_on",
            "reconnect",
            "test connection",
            "✅",
            "▶️",
            "🟢",
        )
    ):
        return "success"

    return "primary"


def panel_button(
    text: str,
    callback_data: str | None = None,
    *,
    style: str | None = None,
    **kwargs,
) -> InlineKeyboardButton:
    """Create a Telegram button with a safe, consistent visual style."""
    return _TelegramInlineKeyboardButton(
        text=translate(text),
        callback_data=callback_data,
        style=style or _button_style(text, callback_data),
        **kwargs,
    )


class Keyboards:
    """
    Factory for all InlineKeyboardMarkup layouts.
    Callback data format: <section>:<action>:<param>
    """

    # ─── Main Menu ──────────────────────────────────────────────────────────

    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📊 Dashboard", callback_data="nav:dashboard"),
                InlineKeyboardButton("💼 Accounts", callback_data="nav:accounts"),
            ],
            [
                InlineKeyboardButton(
                    "📡 Latest Market Scan",
                    callback_data="trading:scan",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 10 Recent Trades · Live",
                    callback_data="trading:recent10",
                ),
            ],
            [
                InlineKeyboardButton("💱 Trading", callback_data="nav:trading"),
                InlineKeyboardButton("🛡️ Risk Control", callback_data="nav:risk"),
            ],
            [
                InlineKeyboardButton("🧠 Strategy", callback_data="nav:strategy"),
                InlineKeyboardButton("📰 News", callback_data="nav:news"),
            ],
            [
                InlineKeyboardButton("📈 Reports", callback_data="nav:reports"),
                InlineKeyboardButton("🔔 Notifications", callback_data="nav:notifications"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="nav:settings"),
                InlineKeyboardButton("🖥️ System", callback_data="nav:system"),
            ],
            [
                InlineKeyboardButton("⚙️ Robot Control", callback_data="nav:robot"),
                InlineKeyboardButton("🔄 Refresh Panel", callback_data="nav:refresh_home"),
            ],
        ])

    # ─── Dashboard ──────────────────────────────────────────────────────────

    @staticmethod
    def dashboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Refresh Status", callback_data="dashboard:refresh"),
                InlineKeyboardButton("🧪 Test Connection", callback_data="dashboard:test_connection"),
            ],
            [
                InlineKeyboardButton(
                    "📡 Latest Market Scan",
                    callback_data="trading:scan",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 10 Recent Trades · Live",
                    callback_data="trading:recent10",
                ),
            ],
            [
                InlineKeyboardButton("🔄 Reconnect MT5", callback_data="robot:restart_mt5_confirm"),
                InlineKeyboardButton("⚙️ Robot Control", callback_data="nav:robot"),
            ],
            [
                InlineKeyboardButton("▶️ Start Bot", callback_data="robot:start"),
                InlineKeyboardButton("⏸️ Pause Bot", callback_data="robot:pause"),
            ],
            [
                InlineKeyboardButton("⏹️ Safe Stop", callback_data="robot:stop_confirm"),
                InlineKeyboardButton("🚨 Emergency Stop", callback_data="robot:emergency_confirm"),
            ],
            [
                InlineKeyboardButton("↩️ Back to Home", callback_data="nav:home"),
            ],
        ])

    # ─── Robot Control ──────────────────────────────────────────────────────

    @staticmethod
    def robot_control() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("▶️ Start Bot", callback_data="robot:start"),
                InlineKeyboardButton("⏸️ Pause Bot", callback_data="robot:pause"),
            ],
            [
                InlineKeyboardButton("▶️ Resume Bot", callback_data="robot:resume"),
                InlineKeyboardButton("⏹️ Safe Stop", callback_data="robot:stop_confirm"),
            ],
            [
                InlineKeyboardButton("🚨 Emergency Stop", callback_data="robot:emergency_confirm"),
            ],
            [
                InlineKeyboardButton("🔄 Restart Engine", callback_data="robot:restart_engine_confirm"),
                InlineKeyboardButton("📡 Restart MT5", callback_data="robot:restart_mt5_confirm"),
            ],
            [
                InlineKeyboardButton("🤖 Restart Telegram", callback_data="robot:restart_telegram_confirm"),
                InlineKeyboardButton("🛑 Safe Shutdown", callback_data="robot:shutdown_confirm"),
            ],
            [
                InlineKeyboardButton("↩️ Back to Home", callback_data="nav:home"),
            ],
        ])

    @staticmethod
    def confirm_action(action: str, label: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"✅ Yes, {label}", callback_data=f"robot:{action}_confirmed"),
                InlineKeyboardButton(f"❌ Cancel", callback_data="nav:home"),
            ],
        ])

    # ─── Accounts ───────────────────────────────────────────────────────────

    @staticmethod
    def accounts_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Add Account", callback_data="accounts:add"),
                InlineKeyboardButton("📋 Account List", callback_data="accounts:list"),
            ],
            [
                InlineKeyboardButton("↩️ Back to Home", callback_data="nav:home"),
            ],
        ])

    @staticmethod
    def account_list(accounts: list[Account]) -> InlineKeyboardMarkup:
        rows = []
        for acc in accounts:
            rows.append([
                InlineKeyboardButton(
                    f"{acc.connection_icon} {acc.type_icon} {acc.name}",
                    callback_data=f"accounts:detail:{acc.id}",
                )
            ])
        rows.append([InlineKeyboardButton("➕ Add Account", callback_data="accounts:add")])
        rows.append([InlineKeyboardButton("↩️ Back to Home", callback_data="nav:home")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def account_detail(account: Account) -> InlineKeyboardMarkup:
        status_label = "Disable" if account.is_enabled else "Enable"
        status_action = "disable" if account.is_enabled else "enable"
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"{'⏸️' if account.is_enabled else '▶️'} {status_label}",
                    callback_data=f"accounts:{status_action}:{account.id}",
                ),
                InlineKeyboardButton(
                    f"⭐ Set Active",
                    callback_data=f"accounts:switch:{account.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🔄 Reconnect",
                    callback_data=f"accounts:reconnect:{account.id}",
                ),
                InlineKeyboardButton(
                    f"📡 Test Connection",
                    callback_data=f"accounts:test:{account.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🗑️ Delete Account",
                    callback_data=f"accounts:delete_confirm:{account.id}",
                ),
            ],
            [
                InlineKeyboardButton("↩️ Back to Accounts", callback_data="accounts:list"),
            ],
        ])

    @staticmethod
    def account_type_select() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Real", callback_data="accounts:type:real")],
            [InlineKeyboardButton("🎓 Demo", callback_data="accounts:type:demo")],
            [InlineKeyboardButton("🏆 Prop Firm", callback_data="accounts:type:prop_firm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="accounts:cancel_add")],
        ])

    # ─── Trading ────────────────────────────────────────────────────────────

    @staticmethod
    def trading_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📈 Open Positions", callback_data="trading:positions"),
                InlineKeyboardButton("⏳ Pending Orders", callback_data="trading:pending"),
            ],
            [
                InlineKeyboardButton(
                    "📡 Latest Market Scan",
                    callback_data="trading:scan",
                ),
                InlineKeyboardButton(
                    "📋 10 Recent Trades",
                    callback_data="trading:recent10",
                ),
            ],
            [
                InlineKeyboardButton(f"🔴 Close All", callback_data="trading:close_all_confirm"),
                InlineKeyboardButton(f"🟢 Close BUY", callback_data="trading:close_buy_confirm"),
                InlineKeyboardButton(f"🔴 Close SELL", callback_data="trading:close_sell_confirm"),
            ],
            [
                InlineKeyboardButton(f"💰 Close Profits", callback_data="trading:close_profit_confirm"),
                InlineKeyboardButton(f"🔻 Close Losses", callback_data="trading:close_loss_confirm"),
            ],
            [
                InlineKeyboardButton("↩️ Back to Home", callback_data="nav:home"),
            ],
        ])

    @staticmethod
    def positions_list(positions: list[Position]) -> InlineKeyboardMarkup:
        rows = []
        for pos in positions:
            profit_str = f"+{pos.floating_profit:.2f}" if pos.floating_profit >= 0 else f"{pos.floating_profit:.2f}"
            rows.append([
                InlineKeyboardButton(
                    f"{pos.direction_icon} #{pos.ticket} {pos.symbol} {pos.volume}L | {profit_str}",
                    callback_data=f"trading:position_detail:{pos.ticket}",
                )
            ])
        rows.append([InlineKeyboardButton("↩️ Back to Trading", callback_data="trading:menu")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def position_detail(ticket: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"❌ Close Position", callback_data=f"trading:close_confirm:{ticket}"),
                InlineKeyboardButton(f"🔄 Partial Close", callback_data=f"trading:partial_close:{ticket}"),
            ],
            [
                InlineKeyboardButton(f"🛡️ Move SL", callback_data=f"trading:move_sl:{ticket}"),
                InlineKeyboardButton(f"🎯 Move TP", callback_data=f"trading:move_tp:{ticket}"),
            ],
            [
                InlineKeyboardButton(f"⚖️ Move to Break-Even", callback_data=f"trading:breakeven:{ticket}"),
                InlineKeyboardButton(f"📐 Trailing Stop", callback_data=f"trading:trail:{ticket}"),
            ],
            [
                InlineKeyboardButton("↩️ Back to Positions", callback_data="trading:positions"),
            ],
        ])

    # ─── Risk ───────────────────────────────────────────────────────────────

    @staticmethod
    def risk_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📊 View Risk Configuration", callback_data="risk:view")],
            [
                InlineKeyboardButton(f"💹 Risk %", callback_data="risk:edit:risk_percent"),
                InlineKeyboardButton(f"📦 Lot Size", callback_data="risk:edit:lot_size"),
            ],
            [
                InlineKeyboardButton(f"📉 Daily Loss Limit", callback_data="risk:edit:daily_loss_limit"),
                InlineKeyboardButton(f"🔢 Max Open Trades", callback_data="risk:edit:max_concurrent_trades"),
            ],
            [
                InlineKeyboardButton(f"📡 Max Spread", callback_data="risk:edit:max_spread_pips"),
                InlineKeyboardButton(f"📉 Max Drawdown", callback_data="risk:edit:max_drawdown_percent"),
            ],
            [
                InlineKeyboardButton(f"⚖️ Risk / Reward", callback_data="risk:edit:rr_ratio"),
                InlineKeyboardButton(f"🛑 Stop Loss", callback_data="risk:edit:default_sl_pips"),
                InlineKeyboardButton(f"🎯 Take Profit", callback_data="risk:edit:default_tp_pips"),
            ],
            [
                InlineKeyboardButton(f"⚖️ Auto Break-Even", callback_data="risk:toggle:auto_breakeven"),
                InlineKeyboardButton(f"📐 Auto Trailing", callback_data="risk:toggle:auto_trailing"),
            ],
            [
                InlineKeyboardButton("↩️ Back to Home", callback_data="nav:home"),
            ],
        ])

    # ─── Strategy ───────────────────────────────────────────────────────────

    @staticmethod
    def strategy_menu(config=None) -> InlineKeyboardMarkup:
        def _btn(component: StrategyComponent, config) -> InlineKeyboardButton:
            enabled = config.is_component_enabled(component) if config else True
            icon = "🟢" if enabled else "🔴"
            return InlineKeyboardButton(
                f"{icon} {component.display_name}",
                callback_data=f"strategy:toggle:{component.value}",
            )

        components = list(StrategyComponent)
        rows = []
        # Pair buttons
        for i in range(0, len(components) - 1, 2):
            rows.append([
                _btn(components[i], config),
                _btn(components[i + 1], config),
            ])
        if len(components) % 2 == 1:
            rows.append([_btn(components[-1], config)])

        rows.extend([
            [
                InlineKeyboardButton(f"✅ Enable All", callback_data="strategy:all_on"),
                InlineKeyboardButton(f"❌ Disable All", callback_data="strategy:all_off"),
            ],
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home")],
        ])
        return InlineKeyboardMarkup(rows)

    # ─── Reports ────────────────────────────────────────────────────────────

    @staticmethod
    def reports_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📅 Daily", callback_data="reports:daily"),
                InlineKeyboardButton(f"📆 Weekly", callback_data="reports:weekly"),
                InlineKeyboardButton(f"📊 Monthly", callback_data="reports:monthly"),
            ],
            [
                InlineKeyboardButton(f"📋 Trade History", callback_data="reports:history"),
            ],
            [
                InlineKeyboardButton(f"⬇️ Export Daily CSV", callback_data="reports:export:daily"),
                InlineKeyboardButton(f"⬇️ Export Monthly CSV", callback_data="reports:export:monthly"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home"),
            ],
        ])

    # ─── Notifications ──────────────────────────────────────────────────────

    @staticmethod
    def notifications_menu(settings: list = None) -> InlineKeyboardMarkup:
        rows = []
        settings_map = {s.notification_type: s for s in (settings or [])}

        for ntype in NotificationType:
            setting = settings_map.get(ntype)
            enabled = setting.enabled if setting else True
            icon = "🔔" if enabled else "🔕"
            rows.append([
                InlineKeyboardButton(
                    f"{icon} {ntype.icon} {ntype.display_name}",
                    callback_data=f"notif:toggle:{ntype.value}",
                )
            ])

        rows.extend([
            [
                InlineKeyboardButton(f"🔔 Enable All", callback_data="notif:all_on"),
                InlineKeyboardButton(f"🔕 Disable All", callback_data="notif:all_off"),
            ],
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home")],
        ])
        return InlineKeyboardMarkup(rows)

    # ─── System ─────────────────────────────────────────────────────────────

    @staticmethod
    def system_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📊 System Stats", callback_data="system:stats"),
                InlineKeyboardButton(f"📋 View Logs", callback_data="system:logs"),
            ],
            [
                InlineKeyboardButton(f"⏱️ Uptime", callback_data="system:uptime"),
                InlineKeyboardButton(f"🌐 Network", callback_data="system:network"),
            ],
            [
                InlineKeyboardButton(f"👥 Users", callback_data="system:users"),
                InlineKeyboardButton(f"📋 Audit Log", callback_data="system:audit"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['refresh']} Refresh", callback_data="system:refresh"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home"),
            ],
        ])

    @staticmethod
    def users_menu(users: list = None) -> InlineKeyboardMarkup:
        rows = []
        for user in (users or []):
            rows.append([
                InlineKeyboardButton(
                    f"{user.role_icon} {user.display_name}",
                    callback_data=f"system:user_detail:{user.telegram_id}",
                )
            ])
        rows.append([InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="system:menu")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def user_role_select(telegram_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👑 Owner", callback_data=f"system:set_role:{telegram_id}:owner")],
            [InlineKeyboardButton(f"🛡️ Admin", callback_data=f"system:set_role:{telegram_id}:admin")],
            [InlineKeyboardButton(f"👁️ Viewer", callback_data=f"system:set_role:{telegram_id}:viewer")],
            [InlineKeyboardButton(f"🚫 Block", callback_data=f"system:set_role:{telegram_id}:blocked")],
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="system:users")],
        ])

    # ─── Settings ───────────────────────────────────────────────────────────

    @staticmethod
    def settings_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔑 Change Bot Token", callback_data="settings:token")],
            [InlineKeyboardButton(f"👥 Manage Admins", callback_data="settings:admins")],
            [InlineKeyboardButton(f"⏱️ Session Timeout", callback_data="settings:session_timeout")],
            [InlineKeyboardButton(f"🔒 Generate Encryption Key", callback_data="settings:gen_key")],
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home")],
        ])

    # ─── Generic ────────────────────────────────────────────────────────────

    @staticmethod
    def back_only(destination: str = "nav:home") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data=destination)],
        ])

    @staticmethod
    def confirm_cancel(confirm_data: str, cancel_data: str = "nav:home") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data=confirm_data),
                InlineKeyboardButton("❌ Cancel", callback_data=cancel_data),
            ],
        ])


# Keep the existing keyboard layouts readable while routing every button
# through the shared style/color policy above.
InlineKeyboardButton = panel_button
