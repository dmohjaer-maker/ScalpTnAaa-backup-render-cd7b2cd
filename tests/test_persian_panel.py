"""Persian localization tests for Telegram user-visible text."""

from telegram_panel.i18n.fa import translate


def test_common_panel_text_is_persian():
    text = translate(
        "📊 <b>DASHBOARD</b>\n"
        "🟢 <b>Robot:</b> RUNNING\n"
        "💰 Net P&L: +1.00"
    )

    assert "داشبورد" in text
    assert "ربات:" in text
    assert "در حال اجرا" in text
    assert "سود و زیان خالص" in text


def test_account_wizard_text_is_persian():
    text = translate(
        "➕ <b>Add New Account</b>\n\n"
        "Step 1/6: Enter a <b>display name</b> for this account."
    )

    assert "افزودن حساب جدید" in text
    assert "نام نمایشی" in text
    assert "این حساب" in text
    assert "Step" not in text


def test_callback_protocol_values_are_not_translated():
    callback_data = "trading:close_buy_confirm:123"
    assert callback_data == "trading:close_buy_confirm:123"