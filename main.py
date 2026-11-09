import os
import time
import json
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

BIQUOTE_URL = "https://biquote.io/api/XAUUSD/ohlc"

TIMEFRAME = "5m"
RSI_PERIOD = 14

BUY_RSI = 30
SELL_RSI = 70

# XAUUSD: 10 pips = $1.00
PIP_SIZE = 0.10

TARGET_PIPS = [40, 60, 80, 100]

POLL_SECONDS = 2
STATE_FILE = "bot_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()

telegram_offset = 0


# =====================================
# STATE
# =====================================

def default_state():

    return {
        "bias": None,
        "armed": False,
        "pending_rearm": False,
        "trade": None
    }


def load_state():

    if not os.path.exists(STATE_FILE):
        return default_state()

    try:

        with open(STATE_FILE, "r") as file:
            state = json.load(file)

        default = default_state()
        default.update(state)

        return default

    except Exception:

        logging.exception("Could not load state")

        return default_state()


state = load_state()


def save_state():

    try:

        with open(STATE_FILE, "w") as file:
            json.dump(state, file, indent=4)

    except Exception:

        logging.exception("Could not save state")


# =====================================
# TELEGRAM
# =====================================

def telegram_request(method, data=None):

    if not TELEGRAM_TOKEN:
        return None

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/{method}"
    )

    try:

        response = session.post(
            url,
            data=data or {},
            timeout=10
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
                "Telegram error: %s",
                result.get("description")
            )
            return None

        return result.get("result")

    except Exception:

        logging.exception("Telegram connection failed")

        return None


def send_message(message):

    if not TELEGRAM_CHAT_ID:
        return False

    result = telegram_request(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        }
    )

    return result is not None


# =====================================
# NUMBER HELPERS
# =====================================

def number(value):

    try:
        return float(value or 0)

    except (TypeError, ValueError):
        return 0.0


# =====================================
# BIQUOTE 5M DATA
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

    raise RuntimeError("Could not read 5M candles")


def get_5m_candles():

    response = session.get(
        BIQUOTE_URL,
        params={
            "timeframe": TIMEFRAME,
            "limit": 200
        },
        timeout=15
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"BiQuote HTTP {response.status_code}"
        )

    data = response.json()

    candles = extract_candles(data)

    if len(candles) < RSI_PERIOD + 2:
        raise RuntimeError("Not enough 5M candles")

    return candles


# =====================================
# RSI CALCULATION
# =====================================

def calculate_rsi(candles):

    closes = [
        number(candle.get("close"))
        for candle in candles
    ]

    if len(closes) < RSI_PERIOD + 1:
        raise RuntimeError("Not enough RSI data")

    changes = []

    for i in range(1, len(closes)):

        changes.append(
            closes[i] - closes[i - 1]
        )

    gains = [
        max(change, 0)
        for change in changes
    ]

    losses = [
        abs(min(change, 0))
        for change in changes
    ]

    avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD

    for i in range(RSI_PERIOD, len(gains)):

        avg_gain = (
            avg_gain * (RSI_PERIOD - 1) + gains[i]
        ) / RSI_PERIOD

        avg_loss = (
            avg_loss * (RSI_PERIOD - 1) + losses[i]
        ) / RSI_PERIOD

    if avg_gain == 0 and avg_loss == 0:
        return 50.0

    if avg_loss == 0:
        return 100.0

    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# =====================================
# TARGET CALCULATION
# =====================================

def calculate_targets(direction, entry):

    targets = []

    for pips in TARGET_PIPS:

        distance = pips * PIP_SIZE

        if direction == "BUY":
            price = entry + distance

        else:
            price = entry - distance

        targets.append({
            "pips": pips,
            "price": round(price, 2)
        })

    return targets


# =====================================
# SIGNAL
# =====================================

def create_signal(direction, entry, rsi):

    targets = calculate_targets(
        direction,
        entry
    )

    state["trade"] = {
        "direction": direction,
        "entry": round(entry, 2),
        "rsi": round(rsi, 2),
        "targets": targets,
        "next_target": 0,
        "started": datetime.now(BAGHDAD).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    }

    state["armed"] = False
    state["pending_rearm"] = False

    save_state()

    emoji = "🟢" if direction == "BUY" else "🔴"

    target_text = ""

    for i, target in enumerate(targets, 1):

        target_text += (
            f"🎯 TP{i}: {target['price']:.2f} "
            f"(+{target['pips']} pip)\n"
        )

    message = (
        "🦅 XAUUSD RSI SIGNAL\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"{emoji} SIGNAL: {direction}\n"
        f"📍 ENTRY: {entry:.2f}\n"
        f"📊 RSI 5M: {rsi:.2f}\n\n"

        f"{target_text}\n"

        "🛑 Stop Loss: NONE\n"
        "⚠️ Signal Only — No Orders\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    send_message(message)

    logging.info(
        "%s signal created at %.2f",
        direction,
        entry
    )


# =====================================
# TARGET MONITORING
# =====================================

def monitor_targets(current_price):

    trade = state.get("trade")

    if not trade:
        return

    direction = trade["direction"]

    targets = trade["targets"]

    next_target = trade["next_target"]

    while next_target < len(targets):

        target = targets[next_target]

        target_price = target["price"]

        if direction == "BUY":

            reached = current_price >= target_price

        else:

            reached = current_price <= target_price

        if not reached:
            break

        pips = target["pips"]

        message = (
            "🎉 XAUUSD TARGET REACHED!\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"📊 Direction: {direction}\n"
            f"📍 Entry: {trade['entry']:.2f}\n"
            f"💵 Current Price: {current_price:.2f}\n\n"

            f"🏆 TP{next_target + 1} COMPLETED\n"
            f"💰 Profit Distance: +{pips} PIPS\n"
            f"🎯 Target Price: {target_price:.2f}\n"
        )

        if next_target == 3:

            message += (
                "\n🏆 ALL FOUR TARGETS COMPLETED!\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "The trade plan is finished.\n"
            )

        else:

            message += (
                "\n⏳ Waiting for the next target.\n"
                "🚫 No new signal during this trade.\n"
            )

        send_message(message)

        next_target += 1

        trade["next_target"] = next_target

        save_state()

    # All targets completed.
    if next_target >= len(targets):

        logging.info("All four targets completed")

        state["trade"] = None

        if state["pending_rearm"]:

            state["armed"] = True

            state["pending_rearm"] = False

            send_message(
                "✅ Previous trade completed.\n"
                "New daily bias is ready.\n"
                "Waiting for RSI 5M."
            )

        else:

            state["armed"] = False

            send_message(
                "⏳ Waiting for your next daily bias:\n\n"
                "Bias Buy\n"
                "or\n"
                "Bias Sell"
            )

        save_state()


# =====================================
# MARKET MONITOR
# =====================================

def monitor_market():

    if not state.get("bias"):
        return

    if not state.get("armed") and not state.get("trade"):
        return

    candles = get_5m_candles()

    latest = candles[-1]

    current_price = number(
        latest.get("close")
    )

    if current_price <= 0:
        return

    # First monitor an active trade.
    if state.get("trade"):

        monitor_targets(current_price)

        return

    # No active trade: calculate RSI.
    rsi = calculate_rsi(candles)

    direction = state["bias"]

    logging.info(
        "Bias=%s RSI=%.2f Price=%.2f",
        direction,
        rsi,
        current_price
    )

    # BUY condition.
    if direction == "BUY" and rsi <= BUY_RSI:

        create_signal(
            "BUY",
            current_price,
            rsi
        )

    # SELL condition.
    elif direction == "SELL" and rsi >= SELL_RSI:

        create_signal(
            "SELL",
            current_price,
            rsi
        )


# =====================================
# DAILY BIAS HANDLER
# =====================================

def set_bias(direction):

    old_bias = state.get("bias")

    state["bias"] = direction

    if state.get("trade"):

        # Store the new bias for after the active trade.
        state["pending_rearm"] = True

        save_state()

        send_message(
            f"✅ NEW DAILY BIAS: {direction}\n\n"
            "An existing trade is still active.\n"
            "No new signal until all four targets finish.\n"
            "New bias will be used afterward."
        )

        return

    state["armed"] = True
    state["pending_rearm"] = False

    save_state()

    send_message(
        "🦅 DAILY BIAS UPDATED\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"📊 Bias: {direction}\n"
        "⏱ Timeframe: 5M\n"
        "📈 Indicator: RSI 14\n\n"

        + (
            "🟢 Waiting for RSI 30 or below."
            if direction == "BUY"
            else "🔴 Waiting for RSI 70 or above."
        )
        + "\n\n"
        "✅ Bias is active.\n"
        "🚫 No automatic orders."
    )

    logging.info(
        "Bias changed from %s to %s",
        old_bias,
        direction
    )


# =====================================
# STATUS
# =====================================

def send_status():

    bias = state.get("bias") or "NOT SET"

    trade = state.get("trade")

    if trade:

        completed = trade["next_target"]

        status = (
            f"Active Trade: {trade['direction']}\n"
            f"Entry: {trade['entry']:.2f}\n"
            f"Targets Completed: {completed}/4\n"
        )

    else:

        status = "Active Trade: NONE\n"

    send_message(
        "🦅 XAUUSD BOT STATUS\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"Daily Bias: {bias}\n"
        f"{status}\n"

        "Timeframe: 5M\n"
        "RSI: 14\n"
        "BUY: RSI <= 30\n"
        "SELL: RSI >= 70\n\n"

        "Stop Loss: NONE\n"
        "Orders: DISABLED"
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
            "timeout": 1,
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

    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return

    normalized = " ".join(text.upper().split())

    if normalized == "BIAS BUY":

        set_bias("BUY")

    elif normalized == "BIAS SELL":

        set_bias("SELL")

    elif normalized == "/STATUS":

        send_status()

    elif normalized == "/START" or normalized == "/HELP":

        send_message(
            "🦅 XAUUSD RSI BOT\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            "Daily Bias:\n"
            "Bias Buy\n"
            "Bias Sell\n\n"

            "RSI 5M Settings:\n"
            "RSI Period: 14\n"
            "BUY: RSI <= 30\n"
            "SELL: RSI >= 70\n\n"

            "Targets:\n"
            "40 / 60 / 80 / 100 PIPS\n\n"

            "/status - Bot Status\n\n"

            "Signals Only.\n"
            "No Stop Loss.\n"
            "No Trading Orders."
        )

    else:

        send_message(
            "⚠️ نامەکە نەناسراوە.\n\n"
            "تەنها یەکێک لەم دووانە بنێرە:\n\n"
            "Bias Buy\n"
            "Bias Sell"
        )


# =====================================
# MAIN LOOP
# =====================================

def main():

    global telegram_offset

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        logging.error(
            "Telegram environment variables missing"
        )

        return

    logging.info(
        "XAUUSD RSI 5M SIGNAL BOT STARTING"
    )

    telegram_request(
        "deleteWebhook",
        {
            "drop_pending_updates": "false"
        }
    )

    send_message(
        "🦅 XAUUSD RSI BOT STARTED\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "📊 RSI 5M Monitoring\n"
        "📈 RSI Period: 14\n\n"

        "🟢 BUY: RSI 30 or below\n"
        "🔴 SELL: RSI 70 or above\n\n"

        "🎯 Targets: 40 / 60 / 80 / 100 PIPS\n"
        "🛑 Stop Loss: NONE\n\n"

        "Send your daily bias:\n"
        "Bias Buy\n"
        "or\n"
        "Bias Sell\n\n"

        "🚫 No automatic orders."
    )

    while True:

        try:

            updates = get_updates()

            for update in updates:

                telegram_offset = (
                    update["update_id"] + 1
                )

                message = update.get("message")

                if message:
                    handle_message(message)

            # Check RSI and active targets.
            try:

                monitor_market()

            except Exception as error:

                logging.warning(
                    "Market monitoring error: %s",
                    str(error)
                )

        except Exception:

            logging.exception("Main loop error")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":

    main()
