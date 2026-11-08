import os
import time
import json
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

# ==========================================
# XAUUSD MARKET POSITIONING BOT
# REPORT ONLY — NO SIGNALS — NO ORDERS
# ==========================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MYFXBOOK_EMAIL = os.getenv("MYFXBOOK_EMAIL")
MYFXBOOK_PASSWORD = os.getenv("MYFXBOOK_PASSWORD")

MYFXBOOK_API = "https://www.myfxbook.com/api"
DATA_URL = "https://biquote.io/api/XAUUSD/ohlc"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

BAGHDAD = ZoneInfo("Asia/Baghdad")

POLL_SECONDS = 15
REPORT_HOUR = 8

# خەمڵاندنی Notional:
# 1 standard lot = 100 oz ـی زێڕ
# ئەمە پێویستە لەگەڵ contract size ـی سەرچاوەکە بگونجێت.
GOLD_OZ_PER_LOT = 100

STATE_FILE = os.getenv("STATE_FILE", "market_report_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "XAUUSD-Market-Report-Bot"
})


# ==========================================
# STATE
# ==========================================

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "last_report_date": None,
            "telegram_offset": 0
        }


state = load_state()


def save_state():
    try:
        directory = os.path.dirname(STATE_FILE)

        if directory:
            os.makedirs(directory, exist_ok=True)

        temp_file = STATE_FILE + ".tmp"

        with open(temp_file, "w") as f:
            json.dump(state, f, indent=2)

        os.replace(temp_file, STATE_FILE)

    except Exception:
        logging.exception("State save failed")


# ==========================================
# TELEGRAM
# ==========================================

def send_message(message):

    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Telegram variables missing.")
        return False

    try:
        response = session.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=20
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            logging.error("Telegram error: %s", result)
            return False

        return True

    except Exception:
        logging.exception("Telegram send failed")
        return False


def get_updates():

    if not BOT_TOKEN:
        return []

    try:
        response = session.get(
            f"{TELEGRAM_URL}/getUpdates",
            params={
                "offset": state.get("telegram_offset", 0) + 1,
                "timeout": 5
            },
            timeout=10
        )

        response.raise_for_status()

        data = response.json()

        if data.get("ok"):
            return data.get("result", [])

    except Exception:
        logging.exception("Telegram updates failed")

    return []


# ==========================================
# MYFXBOOK
# ==========================================

def myfxbook_request(method, params=None):

    url = f"{MYFXBOOK_API}/{method}.json"

    response = session.get(
        url,
        params=params or {},
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if data.get("error"):
        raise RuntimeError(
            data.get("message") or "Myfxbook API error"
        )

    return data


def get_myfxbook_gold_data():

    if not MYFXBOOK_EMAIL or not MYFXBOOK_PASSWORD:
        raise RuntimeError(
            "MYFXBOOK_EMAIL or MYFXBOOK_PASSWORD is missing."
        )

    # Login to Myfxbook

    login_data = myfxbook_request(
        "login",
        {
            "email": MYFXBOOK_EMAIL,
            "password": MYFXBOOK_PASSWORD
        }
    )

    api_session = login_data.get("session")

    if not api_session:
        raise RuntimeError("Myfxbook login failed: no session.")

    try:

        outlook = myfxbook_request(
            "get-community-outlook",
            {
                "session": api_session
            }
        )

        symbols = outlook.get("symbols", [])

        gold = None

        for item in symbols:

            symbol_name = str(item.get("name", "")).upper()

            if symbol_name == "XAUUSD":
                gold = item
                break

        if not gold:
            raise RuntimeError(
                "XAUUSD was not found in Myfxbook community outlook."
            )

        return gold

    finally:

        # Revoke session after collecting the data.

        try:
            myfxbook_request(
                "logout",
                {
                    "session": api_session
                }
            )
        except Exception:
            logging.warning("Myfxbook logout failed.")


# ==========================================
# MARKET CANDLES
# ==========================================

def find_bars(data):

    if isinstance(data, list):

        if data and isinstance(data[0], dict):
            return data

        return []

    if isinstance(data, dict):

        for key in (
            "bars",
            "candles",
            "data",
            "result",
            "ohlc",
            "prices"
        ):

            value = data.get(key)

            if isinstance(value, list):

                if value and isinstance(value[0], dict):
                    return value

            if isinstance(value, dict):

                found = find_bars(value)

                if found:
                    return found

    return []


def get_value(candle, *names):

    for name in names:

        if name in candle and candle[name] is not None:
            return candle[name]

    return None


def time_key(value):

    value = str(value)

    try:
        return (0, float(value))
    except (ValueError, TypeError):
        return (1, value)


def fetch_closed_candles(interval="1h", limit=100):

    response = session.get(
        DATA_URL,
        params={
            "interval": interval,
            "limit": limit
        },
        timeout=25
    )

    response.raise_for_status()

    data = response.json()

    bars = find_bars(data)

    closed = []

    for candle in bars:

        is_open = get_value(
            candle,
            "isOpen",
            "is_open"
        )

        # تەنها کاندڵی داخراو و پشتڕاستکراو

        if is_open is not False:
            continue

        try:

            candle_time = get_value(
                candle,
                "openTime",
                "time",
                "timestamp",
                "datetime",
                "date",
                "open_time",
                "t"
            )

            if candle_time is None:
                continue

            item = {
                "time": str(candle_time),
                "open": float(get_value(
                    candle, "open", "Open", "o"
                )),
                "high": float(get_value(
                    candle, "high", "High", "h"
                )),
                "low": float(get_value(
                    candle, "low", "Low", "l"
                )),
                "close": float(get_value(
                    candle, "close", "Close", "c"
                ))
            }

            closed.append(item)

        except (TypeError, ValueError):
            continue

    unique = {
        item["time"]: item
        for item in closed
    }

    candles = sorted(
        unique.values(),
        key=lambda x: time_key(x["time"])
    )

    if len(candles) < 10:
        raise ValueError(
            f"Not enough closed {interval} candles."
        )

    return candles


# ==========================================
# LIQUIDITY ESTIMATION
# ==========================================

def find_liquidity_zones(candles):

    current_price = candles[-1]["close"]

    highs = []
    lows = []

    # Find local swing highs and lows.

    for i in range(2, len(candles) - 2):

        previous = candles[i - 2:i]
        following = candles[i + 1:i + 3]

        candle = candles[i]

        is_swing_high = all(
            candle["high"] > item["high"]
            for item in previous + following
        )

        is_swing_low = all(
            candle["low"] < item["low"]
            for item in previous + following
        )

        if is_swing_high:
            highs.append(candle["high"])

        if is_swing_low:
            lows.append(candle["low"])

    # Add recent extremes.

    recent = candles[-24:]

    highs.append(max(item["high"] for item in recent))
    lows.append(min(item["low"] for item in recent))

    # Group nearby levels.

    def group_levels(levels, tolerance=0.50):

        groups = []

        for price in sorted(levels):

            matched = False

            for group in groups:

                average = sum(group) / len(group)

                if abs(price - average) <= tolerance:

                    group.append(price)
                    matched = True
                    break

            if not matched:
                groups.append([price])

        results = []

        for group in groups:

            average = sum(group) / len(group)

            results.append({
                "price": round(average, 2),
                "touches": len(group)
            })

        return results

    high_zones = group_levels(highs)
    low_zones = group_levels(lows)

    resistance_zones = [
        item for item in high_zones
        if item["price"] > current_price
    ]

    support_zones = [
        item for item in low_zones
        if item["price"] < current_price
    ]

    resistance_zones.sort(
        key=lambda x: x["price"]
    )

    support_zones.sort(
        key=lambda x: x["price"],
        reverse=True
    )

    return {
        "current_price": current_price,
        "resistance": resistance_zones[:3],
        "support": support_zones[:3]
    }


# ==========================================
# FORMAT HELPERS
# ==========================================

def number(value, decimals=2):

    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "N/A"


def integer(value):

    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return "N/A"


def estimated_notional(lots, average_price):

    try:

        return (
            float(lots)
            * GOLD_OZ_PER_LOT
            * float(average_price)
        )

    except (TypeError, ValueError):
        return None


def format_zones(title, zones):

    lines = [title]

    if not zones:
        lines.append("• هیچ ئاستێکی دیاریکراو نییە.")
        return "\n".join(lines)

    for item in zones:

        lines.append(
            f"• {item['price']:.2f}"
            f" — {item['touches']} جار"
        )

    return "\n".join(lines)


# ==========================================
# REPORT
# ==========================================

def create_market_report():

    now = datetime.now(BAGHDAD)

    # Myfxbook positioning

    gold = get_myfxbook_gold_data()

    long_positions = gold.get("longPositions")
    short_positions = gold.get("shortPositions")

    long_volume = gold.get("longVolume")
    short_volume = gold.get("shortVolume")

    long_percent = gold.get("longPercentage")
    short_percent = gold.get("shortPercentage")

    avg_long = gold.get("avgLongPrice")
    avg_short = gold.get("avgShortPrice")

    long_money = estimated_notional(
        long_volume,
        avg_long
    )

    short_money = estimated_notional(
        short_volume,
        avg_short
    )

    # BiQuote 1H liquidity estimate

    candles = fetch_closed_candles("1h", 100)

    liquidity = find_liquidity_zones(candles)

    # Build report

    message = (
        "🦅 XAUUSD | MARKET POSITIONING REPORT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🕗 کات: {now.strftime('%Y-%m-%d %H:%M')} بەغدا\n\n"

        "📊 MYFXBOOK COMMUNITY\n"
        "🟢 BUY POSITIONS\n"
        f"• ژمارەی پۆزیشەن: {integer(long_positions)}\n"
        f"• ڕێژە: {number(long_percent, 1)}%\n"
        f"• قەبارە: {number(long_volume)} Lot\n"
        f"• Average Entry: {number(avg_long, 2)}\n\n"

        "🔴 SELL POSITIONS\n"
        f"• ژمارەی پۆزیشەن: {integer(short_positions)}\n"
        f"• ڕێژە: {number(short_percent, 1)}%\n"
        f"• قەبارە: {number(short_volume)} Lot\n"
        f"• Average Entry: {number(avg_short, 2)}\n\n"

        "💵 ESTIMATED NOTIONAL VALUE\n"
        f"🟢 BUY: ${number(long_money)}\n"
        f"🔴 SELL: ${number(short_money)}\n\n"

        "📍 MARKET LIQUIDITY ESTIMATE (1H)\n"
        f"💰 نرخی دوا کاندڵ: {number(liquidity['current_price'])}\n\n"

        + format_zones(
            "🔴 ناوچەکانی سەرەوە:",
            liquidity["resistance"]
        )
        + "\n\n"
        + format_zones(
            "🟢 ناوچەکانی خوارەوە:",
            liquidity["support"]
        )
        + "\n\n"

        "📅 COT: داتای هەفتانە لەم وەشانەدا هێشتا پەیوەست نەکراوە.\n\n"

        "⚠️ تێبینی:\n"
        "• داتای Myfxbook تەنها بەشداربووانی کۆمەڵگەی خۆی دەگرێتەوە.\n"
        "• ژمارەکان پۆزیشنن، نەک ژمارەی کەسەکان.\n"
        "• Notional خەمڵاندنە بە بنەمای 100 oz بۆ هەر Lot.\n"
        "• Liquidity ئاستی خەمڵێنراوە، ستۆپی ڕاستەقینە نییە.\n"
        "• هیچ BUY/SELL signal یان ئۆردەرێک نییە.\n\n"

        "📡 DATA: Myfxbook + BiQuote\n"
        "🛑 REPORT ONLY — NO ORDERS"
    )

    return message


# ==========================================
# RUN REPORT
# ==========================================

def run_report():

    try:

        logging.info("Creating XAUUSD market report.")

        report = create_market_report()

        if send_message(report):

            logging.info("Market report sent successfully.")
            return True

        return False

    except Exception as error:

        logging.exception("Market report failed.")

        send_message(
            "⚠️ XAUUSD MARKET REPORT ERROR\n\n"
            f"{str(error)}\n\n"
            "ڕاپۆرتەکە بە سەرکەوتوویی ئامادە نەکرا."
        )

        return False


# ==========================================
# STATUS
# ==========================================

def get_status_message():

    now = datetime.now(BAGHDAD)

    return (
        "🦅 XAUUSD MARKET REPORT BOT\n\n"
        "Mode: MARKET POSITIONING REPORT\n"
        "Sources: Myfxbook + BiQuote\n"
        "Liquidity TF: 1H\n"
        "Schedule: 08:00 Baghdad\n"
        "Signals: DISABLED\n"
        "Orders: DISABLED\n\n"
        f"Baghdad Time: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Last Report: {state.get('last_report_date')}\n\n"
        "🛑 REPORT ONLY — NO ORDERS"
    )


# ==========================================
# TELEGRAM COMMANDS
# ==========================================

def handle_commands():

    updates = get_updates()

    for update in updates:

        state["telegram_offset"] = update["update_id"]
        save_state()

        message = update.get("message", {})
        chat = message.get("chat", {})

        command = message.get("text", "").strip()

        if not command:
            continue

        if str(chat.get("id")) != str(CHAT_ID):
            continue

        if command == "/start":

            send_message(
                "🦅 XAUUSD MARKET REPORT BOT\n\n"
                "/report - ناردنی ڕاپۆرتی ئێستا\n"
                "/status - دۆخی بۆت\n\n"
                "Daily Report: 08:00 Baghdad\n"
                "Sources: Myfxbook + BiQuote\n\n"
                "REPORT ONLY — NO ORDERS"
            )

        elif command == "/report":

            send_message("⏳ ڕاپۆرت ئامادە دەکرێت...")

            run_report()

        elif command == "/status":

            send_message(get_status_message())


# ==========================================
# DAILY SCHEDULE
# ==========================================

def check_daily_schedule():

    now = datetime.now(BAGHDAD)

    today = now.strftime("%Y-%m-%d")

    # Saturday = 5, Sunday = 6

    if now.weekday() in (5, 6):
        return

    if (
        now.hour >= REPORT_HOUR
        and state.get("last_report_date") != today
    ):

        logging.info("Scheduled 08:00 report started.")

        if run_report():

            state["last_report_date"] = today
            save_state()


# ==========================================
# MAIN
# ==========================================

def main():

    logging.info(
        "XAUUSD Market Positioning Bot started."
    )

    if not BOT_TOKEN or not CHAT_ID:
        logging.error(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )

    if not MYFXBOOK_EMAIL or not MYFXBOOK_PASSWORD:
        logging.error(
            "Set MYFXBOOK_EMAIL and MYFXBOOK_PASSWORD."
        )

    send_message(
        "🦅 XAUUSD MARKET REPORT BOT STARTED\n\n"
        "Daily Report: 08:00 Baghdad\n"
        "Sources: Myfxbook + BiQuote\n"
        "Signals: DISABLED\n"
        "Orders: DISABLED"
    )

    while True:

        try:

            handle_commands()
            check_daily_schedule()

        except Exception:

            logging.exception("Main loop error")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
