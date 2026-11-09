
def handle_message(message):
    text = message.get("text", "").strip()
    chat_id = str(message.get("chat", {}).get("id", ""))

    if not text:
        return

    # Only respond to the configured chat.
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return

    command = text.split()[0].split("@")[0].lower()

    if command in ("/start", "/help"):
        send_message(
            "🦅 XAUUSD MARKET POSITIONING BOT\n\n"
            "هەر نامەیەکی ئاسایی بنێرە بۆ وەرگرتنی ڕاپۆرت.\n\n"
            "/report - Market Report\n"
            "/status - Bot Status\n\n"
            "📊 Myfxbook: BUY / SELL\n"
            "📍 Liquidity: 1H\n"
            "🚫 No Trading Orders"
        )

    elif command == "/report":
        run_report()

    elif command == "/status":
        send_message(
            "🦅 XAUUSD BOT STATUS\n\n"
            "Mode: MARKET POSITIONING\n"
            f"Myfxbook: {'Configured' if MYFXBOOK_EMAIL and MYFXBOOK_PASSWORD else 'Missing credentials'}\n"
            "BiQuote: 1H Liquidity\n"
            "Signals: DISABLED\n"
            "Orders: DISABLED"
        )

    elif not text.startswith("/"):
        # Every ordinary message requests a fresh report.
        run_report()

    else:
        send_message("⚠️ فەرمانەکە نەناسراوە. /help بنێرە.")
