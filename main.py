import os
import time
import json
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

# ==============================
# XAUUSD DAILY BIAS BOT
# SIGNAL ONLY - NO ORDERS
# ==============================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DATA_URL = "https://biquote.io/api/XAUUSD/ohlc"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

BAGHDAD = ZoneInfo("Asia/Baghdad")
POLL_SECONDS = 15
STATE_FILE = "bot_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({"User-Agent": "XAUUSD-Daily-Bias-Bot"})


# ==============================
# STATE
# ==============================

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "last_daily_date": None,
            "telegram_offset": 0
        }


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


state = load_state()


# ==============================
# TELEGRAM
# ==============================

def send_message(message):
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Telegram environment variables are missing.")
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
            logging.error("Telegram rejected the message: %s", result)
            return False

        return True

    except Exception:
        logging.exception("Telegram message failed")
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
        logging.exception("Telegram getUpdates failed")

    return []


# ==============================
# MARKET DATA
# ==============================

def find_bars(data):
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            return data
        return []

    if isinstance(data, dict):
        for key in ("bars", "candles", "data", "result", "ohlc", "prices"):
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


def fetch_daily_candles():
    response = session.get(
        DATA_URL,
        params={"interval": "1d", "limit": 100},
        timeout=20
    )
    response.raise_for_status()

    data = response.json()
    bars = find_bars(data)

    closed = []

    for candle in bars:
        is_open = get_value(candle, "isOpen", "is_open")

        # Only candles explicitly marked as closed are accepted.
        if is_open is not False:
            continue

        try:
            item = {
                "time": str(get_value(
                    candle, "openTime", "time", "timestamp",
                    "datetime", "date", "open_time", "t"
                )),
                "open": float(get_value(candle, "open", "Open", "o")),
                "high": float(get_value(candle, "high", "High", "h")),
                "low": float(get_value(candle, "low", "Low", "l")),
                "close": float(get_value(candle, "close", "Close", "c"))
            }

            if item["time"] == "None":
                continue

            closed.append(item)

        except (TypeError, ValueError):
            continue

    # Sort oldest to newest and remove duplicate timestamps.
    unique = {item["time"]: item for item in closed}
    candles = sorted(unique.values(), key=lambda x: x["time"])

    if len(candles) < 2:
        raise ValueError(
            f"BiQuote returned only {len(candles)} closed daily candles."
        )

    return candles


# ==============================
# DAILY BIAS CALCULATION
# ==============================

def analyze_daily():
    candles = fetch_daily_candles()

    # Previous two fully closed daily candles.
    candle1 = candles[-2]
    candle2 = candles[-1]

    # Decision
    if candle2["close"] > candle1["high"]:
        decision = "BUY"
    elif candle2["close"] < candle1["low"]:
        decision = "SELL"
    else:
        decision = "NEUTRAL"

    # Expansion
    range1 = candle1["high"] - candle1["low"]
    range2 = candle2["high"] - candle2["low"]

    if range2 > range1 and candle2["close"] > candle2["open"]:
        expansion = "BULLISH"
    elif range2 > range1 and candle2["close"] < candle2["open"]:
        expansion = "BEARISH"
    else:
        expansion = "NEUTRAL"

    # Contracting: strict inside candle
    contracting = (
        candle2["high"] < candle1["high"]
        and candle2["low"] > candle1["low"]
    )

    # Final Daily Bias
    if decision == "BUY" and expansion == "BULLISH":
        bias = "BUY"
    elif decision == "SELL" and expansion == "BEARISH":
        bias = "SELL"
    else:
        bias = "NEUTRAL"

    return {
        "candle1": candle1,
        "candle2": candle2,
        "range1": range1,
        "range2": range2,
        "decision": decision,
        "expansion": expansion,
        "contracting": contracting,
        "bias": bias
    }


def make_daily_message(result):
    c1 = result["candle1"]
    c2 = result["candle2"]

    contracting_text = "YES" if result["contracting"] else "NO"

    return (
        "🦅 XAUUSD DAILY BIAS ANALYSIS\n\n"
        f"📊 Decision: {result['decision']}\n"
        f"📈 Expansion: {result['expansion']}\n"
        f"📦 Contracting: {contracting_text}\n\n"
        f"🟢 BUY votes: "
        f"{int(result['decision'] == 'BUY') + int(result['expansion'] == 'BULLISH')}/2\n"
        f"🔴 SELL votes: "
        f"{int(result['decision'] == 'SELL') + int(result['expansion'] == 'BEARISH')}/2\n\n"
        f"🎯 DAILY BIAS: {result['bias']}\n\n"
        "📐 RANGE COMPARISON\n"
        f"Candle 1: {result['range1']:.2f}\n"
        f"Candle 2: {result['range2']:.2f}\n\n"
        "🕯 CANDLE 1\n"
        f"Open: {c1['open']}\n"
        f"Close: {c1['close']}\n"
        f"High: {c1['high']}\n"
        f"Low: {c1['low']}\n\n"
        "🕯 CANDLE 2\n"
        f"Open: {c2['open']}\n"
        f"Close: {c2['close']}\n"
        f"High: {c2['high']}\n"
        f"Low: {c2['low']}\n\n"
        f"🕒 {datetime.now(BAGHDAD).strftime('%Y-%m-%d %H:%M:%S')}\n"
        "\n⚠️ SIGNAL ONLY — NO ORDERS"
    )


def run_daily_analysis():
    try:
        result = analyze_daily()
        message = make_daily_message(result)

        if send_message(message):
            logging.info("Daily analysis sent. Bias: %s", result["bias"])
            return True

        return False

    except Exception as error:
        logging.exception("Daily analysis failed")
        send_message(
            "⚠️ XAUUSD Daily Analysis Error\n"
            f"{str(error)}\n\n"
            "Daily Bias was not calculated."
        )
        return False


# ==============================
# TELEGRAM COMMANDS
# ==============================

def handle_commands():
    updates = get_updates()

    for update in updates:
        state["telegram_offset"] = update["update_id"]
        save_state()

        message = update.get("message", {})
        chat = message.get("chat", {})
        text = message.get("text", "").strip()

        if not text:
            continue

        # Respond only to the configured chat.
        if str(chat.get("id")) != str(CHAT_ID):
            continue

        if text == "/start":
            send_message(
                "🦅 XAUUSD DAILY BIAS BOT\n\n"
                "Commands:\n"
                "/daily - Analyze daily candles now\n"
                "/status - Bot status\n\n"
                "SIGNAL ONLY — NO ORDERS"
            )

        elif text == "/daily":
            send_message("⏳ Daily analysis is starting...")
            run_daily_analysis()

        elif text == "/status":
            now = datetime.now(BAGHDAD)
            send_message(
                "🟡 XAUUSD BOT STATUS\n\n"
                "Mode: DAILY BIAS — SIGNAL ONLY\n"
                "Data: BiQuote\n"
                "Timeframe: 1D\n"
                "Scheduled analysis: 07:00 Baghdad\n"
                f"Last scheduled date: {state.get('last_daily_date')}\n"
                f"Baghdad time: {now.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "NO ORDERS"
            )


# ==============================
# DAILY SCHEDULE
# ==============================

def check_daily_schedule():
    now = datetime.now(BAGHDAD)
    today = now.strftime("%Y-%m-%d")

    if (
        now.hour >= 7
        and state.get("last_daily_date") != today
    ):
        if run_daily_analysis():
            state["last_daily_date"] = today
            save_state()


# ==============================
# MAIN
# ==============================

def main():
    logging.info("XAUUSD Daily Bias Bot started — SIGNAL ONLY.")

    if not BOT_TOKEN or not CHAT_ID:
        logging.error(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway."
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
