
import os
import time
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

# =========================
# CONFIGURATION
# =========================

BAGHDAD = ZoneInfo("Asia/Baghdad")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MYFXBOOK_EMAIL = os.getenv("MYFXBOOK_EMAIL", "").strip()
MYFXBOOK_PASSWORD = os.getenv("MYFXBOOK_PASSWORD", "").strip()

MYFXBOOK_API = "https://www.myfxbook.com/api"
BIQUOTE_URL = "https://biquote.io/api/XAUUSD/ohlc"

REPORT_HOUR = 8
REPORT_MINUTE = 0

POLL_SECONDS = 5
CANDLE_LIMIT = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
telegram_offset = 0
last_report_date = None


# =========================
# TELEGRAM
# =========================

def telegram_request(method, data=None):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"

    try:
        response = session.post(
            url,
            data=data or {},
            timeout=20
        )

        if response.status_code != 200:
            logging.error(
                "Telegram API returned HTTP %s",
                response.status_code
            )
            return None

        result = response.json()

        if not result.get("ok"):
            logging.error("Telegram API returned an unsuccessful response")
            return None

        return result.get("result")

    except requests.RequestException:
        logging.error("Telegram connection error")
        return None


def send_message(message):
    if not TELEGRAM_CHAT_ID:
        logging.error("TELEGRAM_CHAT_ID is missing")
        return

    telegram_request(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        }
    )


# =========================
# MYFXBOOK
# =========================

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
        raise RuntimeError(
            "Myfxbook rate limit: HTTP 429. Wait before trying again."
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Myfxbook API returned HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("Myfxbook returned invalid data")

    if data.get("error"):
        message = str(data.get("message", "API error"))

        # Never send raw URLs, credentials, or API response details.
        safe_messages = {
            "Invalid credentials",
            "Invalid session",
            "Invalid symbol"
        }

        if message in safe_messages:
            raise RuntimeError(message)

        raise RuntimeError("Myfxbook API rejected the request")

    return data


def get_myfxbook_outlook():
    if not MYFXBOOK_EMAIL or not MYFXBOOK_PASSWORD:
        raise RuntimeError(
            "MYFXBOOK_EMAIL or MYFXBOOK_PASSWORD is missing"
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
        raise RuntimeError("Myfxbook login did not return a session")

    try:
        outlook_data = myfxbook_request(
            "get-community-outlook.json",
            {
                "session": api_session
            }
        )

        symbols = outlook_data.get("symbols", [])

        for item in symbols:
            symbol_name = str(item.get("name", "")).upper()

            if symbol_name in ("XAUUSD", "GOLD"):
                return item

        raise RuntimeError("XAUUSD was not found in Myfxbook data")

    finally:
        # Logout is best-effort. Never expose the session or URL.
        try:
            session.get(
                f"{MYFXBOOK_API}/logout.json",
                params={"session": api_session},
                timeout=10
            )
        except requests.RequestException:
            logging.warning("Myfxbook logout request failed")


# =========================
# FORMAT HELPERS
# =========================

def number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def format_number(value, decimals=2):
    return f"{number(value):,.{decimals}f}"


def calculate_notional(lots, average_price):
    # Estimate only: assumes 1 standard lot = 100 oz of gold.
    return number(lots) * 100 * number(average_price)


def build_position_report(item):
    long_positions = number(item.get("longPositions"))
    short_positions = number(item.get("shortPositions"))

    long_volume = number(item.get("longVolume"))
    short_volume = number(item.get("shortVolume"))

    long_percentage = number(item.get("longPercentage"))
    short_percentage = number(item.get("shortPercentage"))

    avg_long = number(item.get("avgLongPrice"))
    avg_short = number(item.get("avgShortPrice"))

    long_notional = calculate_notional(long_volume, avg_long)
    short_notional = calculate_notional(short_volume, avg_short)

    total_positions = long_positions + short_positions
    total_volume = long_volume + short_volume

    if total_positions > 0:
        long_position_share = long_positions / total_positions * 100
        short_position_share = short_positions / total_positions * 100
    else:
        long_position_share = 0
        short_position_share = 0

    if total_volume > 0:
        long_volume_share = long_volume / total_volume * 100
        short_volume_share = short_volume / total_volume * 100
    else:
        long_volume_share = 0
        short_volume_share = 0

    report = (
        "🦅 XAUUSD MARKET POSITIONING\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🟢 BUY POSITIONS\n"
        f"Positions: {long_positions:,.0f}\n"
        f"Share: {long_percentage:.2f}%\n"
        f"Lots: {long_volume:,.2f}\n"
        f"Average Entry: {avg_long:.2f}\n"
        f"Estimated Notional: ${long_notional:,.0f}\n\n"
        "🔴 SELL POSITIONS\n"
        f"Positions: {short_positions:,.0f}\n"
        f"Share: {short_percentage:.2f}%\n"
        f"Lots: {short_volume:,.2f}\n"
        f"Average Entry: {avg_short:.2f}\n"
        f"Estimated Notional: ${short_notional:,.0f}\n\n"
        "📊 POSITION DISTRIBUTION\n"
        f"BUY positions: {long_position_share:.2f}%\n"
        f"SELL positions: {short_position_share:.2f}%\n"
        f"BUY volume: {long_volume_share:.2f}%\n"
        f"SELL volume: {short_volume_share:.2f}%\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ These figures represent Myfxbook community data, "
        "not the entire global gold market.\n"
        "Notional values are estimates based on 100 oz per standard lot. "
        "They are not actual cash invested or margin."
    )

    return report


# =========================
# BIQUOTE 1H DATA
# =========================

def extract_candles(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("data", "candles", "ohlc", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    raise RuntimeError("Could not read BiQuote candle data")


def get_hourly_candles():
    try:
        response = session.get(
            BIQUOTE_URL,
            params={"timeframe": "1h", "limit": CANDLE_LIMIT},
            timeout=20
        )

    except requests.RequestException:
        raise RuntimeError("BiQuote connection failed")

    if response.status_code != 200:
        raise RuntimeError(
            f"BiQuote returned HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("BiQuote returned invalid data")

    candles = extract_candles(data)

    # Use only closed candles when the API provides an isOpen field.
    if candles and isinstance(candles[-1], dict):
        if "isOpen" in candles[-1]:
            candles = [
                candle for candle in candles
                if not candle.get("isOpen", False)
            ]
        else:
            # Without an isOpen field, exclude the newest candle
            # because it may still be forming.
            candles = candles[:-1]

    if len(candles) < 10:
        raise RuntimeError("Not enough closed 1H candles")

    return candles


def get_liquidity_zones():
    candles = get_hourly_candles()

    highs = []
    lows = []

    for i in range(2, len(candles) - 2):
        current = candles[i]

        try:
            high = number(current.get("high"))
            low = number(current.get("low"))
        except AttributeError:
            continue

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

        if high > max(previous_highs + next_highs):
            highs.append(high)

        if low < min(previous_lows + next_lows):
            lows.append(low)

    last_close = number(candles[-1].get("close"))

    if last_close <= 0:
        raise RuntimeError("Could not read latest 1H price")

    # Nearest swing highs above price and swing lows below price.
    resistance_zones = sorted(
        set(round(x, 2) for x in highs if x > last_close)
    )[:3]

    support_zones = sorted(
        set(round(x, 2) for x in lows if x < last_close),
        reverse=True
    )[:3]

    return last_close, resistance_zones, support_zones


def format_zones(zones):
    if not zones:
        return "Not available"

    return " | ".join(f"{level:.2f}" for level in zones)


# =========================
# COMPLETE REPORT
# =========================

def generate_report():
    item = get_myfxbook_outlook()

    positioning = build_position_report(item)

    try:
        price, resistances, supports = get_liquidity_zones()

        liquidity = (
            "\n\n🧭 ESTIMATED LIQUIDITY AREAS (1H)\n"
            f"Reference Price: {price:.2f}\n"
            f"Potential High Zones: {format_zones(resistances)}\n"
            f"Potential Low Zones: {format_zones(supports)}\n\n"
            "These are estimated swing-high/low areas, "
            "not confirmed stop orders."
        )

    except Exception:
        logging.warning("Could not calculate 1H liquidity estimates")
        liquidity = (
            "\n\n🧭 ESTIMATED LIQUIDITY AREAS (1H)\n"
            "Temporarily unavailable."
        )

    now = datetime.now(BAGHDAD).strftime("%Y-%m-%d %H:%M")

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        "📅 " + now + " Baghdad\n"
        "📌 Source: Myfxbook Community Outlook\n"
        "📌 Price zones: BiQuote 1H candles\n"
        "📌 COT: Not connected yet\n\n"
        "⚪ REPORT ONLY\n"
        "🚫 No BUY/SELL signals\n"
        "🚫 No orders"
    )

    return positioning + liquidity + footer


def run_report():
    send_message("⏳ Preparing XAUUSD market report...")

    try:
        report = generate_report()
        send_message(report)

    except RuntimeError as error:
        # Only safe, controlled error messages are sent.
        send_message(
            "⚠️ XAUUSD REPORT ERROR\n\n"
            f"{str(error)}\n\n"
            "No credentials or request URLs are displayed."
        )

    except Exception as error:
        # Do not log raw exception text; it may contain request URLs.
        logging.error(
            "Report failed. Exception type: %s",
            type(error).__name__
        )

        send_message(
            "⚠️ Report could not be completed.\n"
            "Check Railway logs for the safe error category."
        )


# =========================
# TELEGRAM COMMANDS
# =========================

def get_updates():
    global telegram_offset

    result = telegram_request(
        "getUpdates",
        {
            "offset": telegram_offset,
            "timeout": 10,
            "allowed_updates": '["message"]'
        }
    )

    return result or []


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
            "🦅 XAUUSD MARKET REPORT BOT\n\n"
            "/report - Get market positioning report\n"
            "/status - Check bot status\n\n"
            "Daily report: 08:00 Baghdad\n"
            "Signals: DISABLED\n"
            "Orders: DISABLED"
        )

    elif command == "/report":
        run_report()

    elif command == "/status":
        send_message(
            "🦅 XAUUSD BOT STATUS\n\n"
            "Mode: MARKET REPORT ONLY\n"
            "Myfxbook: Configured"
            if MYFXBOOK_EMAIL and MYFXBOOK_PASSWORD
            else "🦅 XAUUSD BOT STATUS\n\nMyfxbook: Missing credentials"
        )


# =========================
# MAIN LOOP
# =========================

def main():
    global telegram_offset, last_report_date

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram environment variables are missing")
        return

    logging.info("XAUUSD MARKET REPORT BOT started")

    send_message(
        "🦅 XAUUSD MARKET REPORT BOT STARTED\n\n"
        "Daily Report: 08:00 Baghdad\n"
        "Sources: Myfxbook + BiQuote\n"
        "Signals: DISABLED\n"
        "Orders: DISABLED"
    )

    while True:
        try:
            now = datetime.now(BAGHDAD)

            # Automatic weekday report at 08:00 Baghdad.
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
                telegram_offset = update["update_id"] + 1

                message = update.get("message")
                if message:
                    handle_message(message)

        except Exception as error:
            logging.error(
                "Main loop error. Exception type: %s",
                type(error).__name__
            )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
