import os
import time
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

# =====================================
# CONFIGURATION
# =====================================

BAGHDAD = ZoneInfo("Asia/Baghdad")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MYFXBOOK_EMAIL = os.getenv("MYFXBOOK_EMAIL", "").strip()
MYFXBOOK_PASSWORD = os.getenv("MYFXBOOK_PASSWORD", "").strip()

MYFXBOOK_API = "https://www.myfxbook.com/api"
BIQUOTE_URL = "https://biquote.io/api/XAUUSD/ohlc"

REPORT_HOUR = 8
REPORT_MINUTE = 0

CANDLE_LIMIT = 200
POLL_SECONDS = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()

telegram_offset = 0
last_report_date = None


# =====================================
# TELEGRAM CONNECTION
# =====================================

def telegram_request(method, data=None):

    if not TELEGRAM_TOKEN:
        logging.error("Telegram token missing")
        return None

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/{method}"
    )

    try:
        response = session.post(
            url,
            data=data or {},
            timeout=35
        )

        if response.status_code != 200:
            logging.error(
                "Telegram HTTP error: %s",
                response.status_code
            )
            return None

        result = response.json()

        if not result.get("ok"):
            logging.error(
                "Telegram request failed: %s",
                result.get("description", "Unknown error")
            )
            return None

        return result.get("result")

    except requests.RequestException:
        logging.error("Telegram connection failed")
        return None

    except ValueError:
        logging.error("Invalid Telegram response")
        return None


def send_message(message):

    if not TELEGRAM_CHAT_ID:
        logging.error("Telegram chat ID missing")
        return False

    result = telegram_request(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        }
    )

    if result:
        logging.info("Telegram message sent")
        return True

    logging.error("Telegram message could not be sent")
    return False


def initialize_telegram():

    result = telegram_request(
        "deleteWebhook",
        {
            "drop_pending_updates": "false"
        }
    )

    if result is not None:
        logging.info("Telegram webhook cleared")


# =====================================
# MYFXBOOK API
# =====================================

def myfxbook_request(endpoint, params):

    url = f"{MYFXBOOK_API}/{endpoint}"

    try:
        response = session.get(
            url,
            params=params,
            timeout=25
        )

    except requests.Timeout:
        raise RuntimeError("Myfxbook connection timed out")

    except requests.RequestException:
        raise RuntimeError("Myfxbook connection failed")

    if response.status_code == 429:
        raise RuntimeError("Myfxbook rate limit reached")

    if response.status_code != 200:
        raise RuntimeError(
            f"Myfxbook HTTP {response.status_code}"
        )

    try:
        data = response.json()

    except ValueError:
        raise RuntimeError("Invalid Myfxbook response")

    if data.get("error"):

        message = str(
            data.get("message", "Unknown error")
        )

        logging.error(
            "Myfxbook rejected %s: %s",
            endpoint,
            message[:200]
        )

        raise RuntimeError(
            f"Myfxbook {endpoint}: {message[:200]}"
        )

    return data


def get_myfxbook_outlook():

    if not MYFXBOOK_EMAIL or not MYFXBOOK_PASSWORD:
        raise RuntimeError(
            "Myfxbook credentials missing"
        )

    login_data = myfxbook_request(
        "login.json",
        {
            "email": MYFXBOOK_EMAIL,
            "password": MYFXBOOK_PASSWORD
        }
    )

    api_session = login_data.get("session")

    if not api_session:
        raise RuntimeError("Myfxbook login failed")

    try:

        outlook = myfxbook_request(
            "get-community-outlook.json",
            {
                "session": api_session
            }
        )

        symbols = outlook.get("symbols", [])

        for item in symbols:

            name = str(
                item.get("name", "")
            ).upper().replace("/", "").replace(".", "")

            if name in ("XAUUSD", "GOLD"):
                return item

        available = [
            str(item.get("name", ""))
            for item in symbols
        ]

        logging.warning(
            "XAUUSD not found. Available symbols: %s",
            available[:30]
        )

        raise RuntimeError(
            "XAUUSD not found in Myfxbook data"
        )

    finally:

        try:
            session.get(
                f"{MYFXBOOK_API}/logout.json",
                params={"session": api_session},
                timeout=10
            )

        except requests.RequestException:
            logging.warning("Myfxbook logout failed")


# =====================================
# NUMBER HELPERS
# =====================================

def number(value):

    try:
        return float(value or 0)

    except (TypeError, ValueError):
        return 0.0


def calculate_notional(lots, price):

    return number(lots) * 100 * number(price)


# =====================================
# POSITION REPORT
# =====================================

def build_position_report(item):

    long_positions = number(
        item.get("longPositions")
    )

    short_positions = number(
        item.get("shortPositions")
    )

    long_volume = number(
        item.get("longVolume")
    )

    short_volume = number(
        item.get("shortVolume")
    )

    long_percentage = number(
        item.get("longPercentage")
    )

    short_percentage = number(
        item.get("shortPercentage")
    )

    avg_long = number(
        item.get("avgLongPrice")
    )

    avg_short = number(
        item.get("avgShortPrice")
    )

    total_positions = (
        long_positions + short_positions
    )

    total_volume = (
        long_volume + short_volume
    )

    long_position_share = (
        long_positions / total_positions * 100
        if total_positions else 0
    )

    short_position_share = (
        short_positions / total_positions * 100
        if total_positions else 0
    )

    long_volume_share = (
        long_volume / total_volume * 100
        if total_volume else 0
    )

    short_volume_share = (
        short_volume / total_volume * 100
        if total_volume else 0
    )

    long_notional = calculate_notional(
        long_volume, avg_long
    )

    short_notional = calculate_notional(
        short_volume, avg_short
    )

    report = (
        "🦅 XAUUSD MARKET POSITIONING\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "🟢 BUY — کڕیاران\n"
        f"📊 Positions: {long_positions:,.0f}\n"
        f"💪 Strength: {long_percentage:.2f}%\n"
        f"📦 Lots: {long_volume:,.2f}\n"
        f"📍 Average Entry: {avg_long:.2f}\n"
        f"💰 Estimated Notional: ${long_notional:,.0f}\n\n"

        "🔴 SELL — فرۆشیاران\n"
        f"📊 Positions: {short_positions:,.0f}\n"
        f"💪 Strength: {short_percentage:.2f}%\n"
        f"📦 Lots: {short_volume:,.2f}\n"
        f"📍 Average Entry: {avg_short:.2f}\n"
        f"💰 Estimated Notional: ${short_notional:,.0f}\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "📊 POSITION DISTRIBUTION\n\n"

        f"🟢 BUY Positions: {long_position_share:.2f}%\n"
        f"🔴 SELL Positions: {short_position_share:.2f}%\n"
        f"🟢 BUY Volume: {long_volume_share:.2f}%\n"
        f"🔴 SELL Volume: {short_volume_share:.2f}%\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Myfxbook Community Data Only\n"
        "Not the entire global gold market.\n"
        "Notional is estimated using 100 oz per lot."
    )

    return report


# =====================================
# BIQUOTE 1H CANDLES
# =====================================

def extract_candles(data):

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        for key in (
            "data",
            "candles",
            "ohlc",
            "result"
        ):

            value = data.get(key)

            if isinstance(value, list):
                return value

    raise RuntimeError("Could not read candle data")


def get_hourly_candles():

    try:

        response = session.get(
            BIQUOTE_URL,
            params={
                "timeframe": "1h",
                "limit": CANDLE_LIMIT
            },
            timeout=20
        )

    except requests.RequestException:
        raise RuntimeError("BiQuote connection failed")

    if response.status_code != 200:
        raise RuntimeError(
            f"BiQuote HTTP error: {response.status_code}"
        )

    try:
        data = response.json()

    except ValueError:
        raise RuntimeError("Invalid BiQuote data")

    candles = extract_candles(data)

    if candles and isinstance(candles[-1], dict):

        if "isOpen" in candles[-1]:

            candles = [
                c for c in candles
                if not c.get("isOpen", False)
            ]

        else:
            candles = candles[:-1]

    if len(candles) < 10:
        raise RuntimeError("Not enough closed 1H candles")

    return candles


# =====================================
# LIQUIDITY ZONES
# =====================================

def get_liquidity_zones():

    candles = get_hourly_candles()

    highs = []
    lows = []

    for i in range(2, len(candles) - 2):

        current = candles[i]

        try:

            high = number(current.get("high"))
            low = number(current.get("low"))

            previous_highs = [
                number(candles[j].get("high"))
                for j in range(i - 2, i)
            ]

            next_highs = [
                number(candles[j].get("high"))
                for j in range(i + 1, i + 3)
            ]

            previous_lows = [
                number(candles[j].get("low"))
                for j in range(i - 2, i)
            ]

            next_lows = [
                number(candles[j].get("low"))
                for j in range(i + 1, i + 3)
            ]

        except (AttributeError, TypeError):
            continue

        if high > max(previous_highs + next_highs):
            highs.append(high)

        if low < min(previous_lows + next_lows):
            lows.append(low)

    last_close = number(candles[-1].get("close"))

    if last_close <= 0:
        raise RuntimeError("Invalid latest price")

    resistances = sorted(
        set(round(x, 2) for x in highs if x > last_close)
    )[:3]

    supports = sorted(
        set(round(x, 2) for x in lows if x < last_close),
        reverse=True
    )[:3]

    return last_close, resistances, supports


def format_zones(zones):

    if not zones:
        return "Not available"

    return " | ".join(
        f"{x:.2f}" for x in zones
    )


# =====================================
# COMPLETE REPORT
# =====================================

def generate_report():

    item = get_myfxbook_outlook()

    positioning = build_position_report(item)

    try:

        price, resistances, supports = (
            get_liquidity_zones()
        )

        liquidity = (
            "\n\n🧭 LIQUIDITY AREAS — 1H\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💵 Reference Price: {price:.2f}\n\n"
            "🔴 Potential High Liquidity:\n"
            f"{format_zones(resistances)}\n\n"
            "🟢 Potential Low Liquidity:\n"
            f"{format_zones(supports)}\n\n"
            "⚠️ Estimated swing areas only."
        )

    except Exception as error:

        logging.warning(
            "Liquidity calculation failed: %s",
            str(error)
        )

        liquidity = (
            "\n\n🧭 LIQUIDITY AREAS — 1H\n"
            "Temporarily unavailable."
        )

    now = datetime.now(BAGHDAD).strftime(
        "%Y-%m-%d %H:%M"
    )

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        f"📅 {now} Baghdad\n"
        "📌 Myfxbook Community Outlook\n"
        "📌 BiQuote 1H Candles\n\n"
        "⚪ INFORMATION ONLY\n"
        "🚫 No automatic BUY/SELL\n"
        "🚫 No Manual Bias\n"
        "🚫 No trading orders\n\n"
        "⚠️ Exact individual Stop Loss data "
        "is not provided by Myfxbook."
    )

    return positioning + liquidity + footer


# =====================================
# REPORT EXECUTION
# =====================================

def run_report():

    send_message(
        "⏳ Preparing XAUUSD Market Report..."
    )

    try:

        report = generate_report()

        send_message(report)

    except RuntimeError as error:

        logging.warning(
            "Report error: %s",
            str(error)
        )

        send_message(
            "⚠️ XAUUSD REPORT ERROR\n\n"
            f"{str(error)}"
        )

    except Exception as error:

        logging.exception(
            "Report failed"
        )

        send_message(
            "⚠️ Report could not be completed.\n"
            "Check Railway logs."
        )


# =====================================
# TELEGRAM UPDATES
# =====================================

def get_updates():

    global telegram_offset

    result = telegram_request(
        "getUpdates",
        {
            "offset": telegram_offset,
            "timeout": 25,
            "allowed_updates": '["message"]'
        }
    )

    return result if isinstance(result, list) else []


# =====================================
# MESSAGE HANDLER
# =====================================

def handle_message(message):

    text = message.get("text", "").strip()

    chat_id = str(
        message.get("chat", {}).get("id", "")
    )

    if not text:
        return

    # Only the configured chat can use this bot.
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return

    command = text.split()[0].split("@")[0].lower()

    if command in ("/start", "/help"):

        send_message(
            "🦅 XAUUSD MARKET POSITIONING BOT\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "هەر نامەیەکی ئاسایی بنێرە "
            "بۆ وەرگرتنی ڕاپۆرت.\n\n"
            "📊 /report - Market Report\n"
            "📌 /status - Bot Status\n"
            "ℹ️ /help - Help\n\n"
            "Sources: Myfxbook + BiQuote\n"
            "Liquidity: 1H\n"
            "Signals: DISABLED\n"
            "Orders: DISABLED"
        )

    elif command == "/report":

        run_report()

    elif command == "/status":

        send_message(
            "🦅 XAUUSD BOT STATUS\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Mode: MARKET POSITIONING\n"
            "Myfxbook: "
            + (
                "Configured"
                if MYFXBOOK_EMAIL and MYFXBOOK_PASSWORD
                else "Missing credentials"
            )
            + "\nBiQuote: 1H\n"
            "Telegram: Connected\n"
            "Signals: DISABLED\n"
            "Orders: DISABLED"
        )

    elif text.startswith("/"):

        send_message(
            "⚠️ Unknown command.\n"
            "Send /help for instructions."
        )

    else:

        logging.info("Ordinary message received")

        run_report()


# =====================================
# MAIN LOOP
# =====================================

def main():

    global telegram_offset
    global last_report_date

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        logging.error(
            "Telegram environment variables missing"
        )

        return

    logging.info(
        "Starting XAUUSD MARKET POSITIONING BOT"
    )

    initialize_telegram()

    send_message(
        "🦅 XAUUSD MARKET POSITIONING BOT STARTED\n\n"
        "📊 Myfxbook BUY/SELL positioning\n"
        "📍 BiQuote 1H Liquidity\n\n"
        "Send ANY ordinary message for a report.\n\n"
        "Signals: DISABLED\n"
        "Manual Bias: DISABLED\n"
        "Orders: DISABLED"
    )

    logging.info(
        "XAUUSD MARKET POSITIONING BOT started successfully"
    )

    while True:

        try:

            now = datetime.now(BAGHDAD)

            # Automatic weekday report at 08:00.
            if (
                now.weekday() < 5
                and now.hour == REPORT_HOUR
                and now.minute == REPORT_MINUTE
                and last_report_date != now.date()
            ):

                last_report_date = now.date()

                run_report()

            updates = get_updates()

            for update in updates:

                telegram_offset = (
                    update["update_id"] + 1
                )

                message = update.get("message")

                if message:
                    handle_message(message)

        except Exception:

            logging.exception("Main loop error")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":

    main()
