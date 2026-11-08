import os
import time
import json
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

# ==========================================
# XAUUSD DAILY BIAS + 5 LEVELS BOT
# SIGNAL ONLY — NO ORDERS
# ==========================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DATA_URL = "https://biquote.io/api/XAUUSD/ohlc"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

BAGHDAD = ZoneInfo("Asia/Baghdad")

POLL_SECONDS = 15
DAILY_HOUR = 7

TP_DISTANCE = 10.0
LEVEL_STEP = 10.0
MAX_TP_WINS = 3

STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "XAUUSD-Daily-Bias-Bot"
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
            "last_daily_date": None,
            "telegram_offset": 0,
            "plan": None
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
        logging.exception("Telegram updates failed")

    return []


# ==========================================
# MARKET DATA
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


def fetch_closed_candles(interval, limit=100):

    response = session.get(
        DATA_URL,
        params={
            "interval": interval,
            "limit": limit
        },
        timeout=20
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

        # Only confirmed closed candles.

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

    if not candles:
        raise ValueError(
            f"No closed candles found for {interval}"
        )

    return candles


def fetch_daily_candles():

    candles = fetch_closed_candles("1d", 100)

    if len(candles) < 2:
        raise ValueError("Need two closed daily candles.")

    return candles


# ==========================================
# DAILY BIAS CALCULATION
# ==========================================

def analyze_daily():

    candles = fetch_daily_candles()

    candle1 = candles[-2]
    candle2 = candles[-1]

    # DECISION

    if candle2["close"] > candle1["high"]:

        decision = "BUY"

    elif candle2["close"] < candle1["low"]:

        decision = "SELL"

    else:

        decision = "NEUTRAL"

    # EXPANSION

    range1 = candle1["high"] - candle1["low"]

    range2 = candle2["high"] - candle2["low"]

    if (
        range2 > range1
        and candle2["close"] > candle2["open"]
    ):

        expansion = "BULLISH"

    elif (
        range2 > range1
        and candle2["close"] < candle2["open"]
    ):

        expansion = "BEARISH"

    else:

        expansion = "NEUTRAL"

    # CONTRACTING

    contracting = (
        candle2["high"] < candle1["high"]
        and candle2["low"] > candle1["low"]
    )

    # FINAL BIAS

    if (
        decision == "BUY"
        and expansion == "BULLISH"
    ):

        bias = "BUY"

    elif (
        decision == "SELL"
        and expansion == "BEARISH"
    ):

        bias = "SELL"

    else:

        bias = "NEUTRAL"

    buy_votes = (
        int(decision == "BUY")
        + int(expansion == "BULLISH")
    )

    sell_votes = (
        int(decision == "SELL")
        + int(expansion == "BEARISH")
    )

    return {
        "candle1": candle1,
        "candle2": candle2,
        "range1": range1,
        "range2": range2,
        "decision": decision,
        "expansion": expansion,
        "contracting": contracting,
        "bias": bias,
        "buy_votes": buy_votes,
        "sell_votes": sell_votes
    }


# ==========================================
# DAILY PLAN CREATION
# ==========================================

def create_daily_plan(result):

    bias = result["bias"]

    if bias not in ("BUY", "SELL"):
        return None

    # Latest CLOSED 5M candle.

    candles = fetch_closed_candles("5m", 100)

    reference_candle = candles[-1]

    reference = reference_candle["close"]

    if bias == "BUY":
        direction = 1
        direction_text = "کڕین 🟢"
        emoji = "🟢"
    else:
        direction = -1
        direction_text = "فرۆشتن 🔴"
        emoji = "🔴"

    # Main trade + four additional levels.

    entries = [
        reference,
        reference - 20.0,
        reference - 10.0,
        reference + 10.0,
        reference + 20.0
    ]

    trades = []

    for index, entry in enumerate(entries):

        target = entry + (direction * TP_DISTANCE)

        trades.append({
            "number": index,
            "entry": round(entry, 2),
            "tp": round(target, 2),
            "status": "WAITING",
            "activated_time": None,
            "tp_time": None
        })

    now = datetime.now(BAGHDAD)

    plan = {
        "date": now.strftime("%Y-%m-%d"),
        "bias": bias,
        "reference": round(reference, 2),
        "direction": direction,
        "direction_text": direction_text,
        "emoji": emoji,
        "trades": trades,
        "tp_wins": 0,
        "finished": False,
        "last_5m_time": reference_candle["time"],
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "decision": result["decision"],
        "expansion": result["expansion"]
    }

    return plan


# ==========================================
# PLAN MESSAGE
# ==========================================

def make_plan_message(plan):

    trades = plan["trades"]

    main = trades[0]

    extra = trades[1:]

    message = (
        "🦅🏆 XAUUSD | پلانی نوێ\n\n"
        f"📊 ئاراستە: {plan['direction_text']}\n"
        f"🎯 نرخی بایەس: {plan['reference']:.2f}\n\n"
        "⭐ مامەڵەی سەرەکی:\n"
        f"Entry: {main['entry']:.2f}\n"
        f"TP: {main['tp']:.2f}\n\n"
        "📍 چوار ئاستی زیادە:\n"
    )

    for index, trade in enumerate(extra, 1):

        message += (
            f"{index}. {trade['entry']:.2f}"
            f" → TP {trade['tp']:.2f}\n"
        )

    message += (
        "\n💰 هەر TP = 100 پیپ\n"
        "🏆 کاتێک 3 مامەڵە TP بکەن، "
        "پلانی ئەم ڕۆژە کۆتایی دێت.\n\n"
        "📊 Daily Decision: "
        f"{plan['decision']}\n"
        "📈 Expansion: "
        f"{plan['expansion']}\n\n"
        "⚠️ SIGNAL ONLY — NO ORDERS"
    )

    return message


# ==========================================
# TRADE MONITORING
# ==========================================

def process_5m_candle(candle):

    plan = state.get("plan")

    if not plan or plan.get("finished"):
        return

    candle_time = candle["time"]

    events = []

    direction = plan["direction"]

    for trade in plan["trades"]:

        status = trade["status"]

        entry = trade["entry"]

        target = trade["tp"]

        # WAITING -> ACTIVE

        if status == "WAITING":

            touched = (
                candle["low"] <= entry
                and candle["high"] >= entry
            )

            if touched:

                trade["status"] = "ACTIVE"

                trade["activated_time"] = candle_time

                events.append(
                    f"🟡 Entry {trade['number']}: "
                    f"{entry:.2f} ACTIVE"
                )

        # ACTIVE -> TP

        elif status == "ACTIVE":

            # TP must occur on a later closed candle.

            if candle_time == trade["activated_time"]:
                continue

            if direction == 1:

                tp_hit = candle["high"] >= target

            else:

                tp_hit = candle["low"] <= target

            if tp_hit:

                trade["status"] = "TP"

                trade["tp_time"] = candle_time

                plan["tp_wins"] += 1

                events.append(
                    f"🏆 TP HIT | Entry: {entry:.2f} "
                    f"| TP: {target:.2f}"
                )

    # Three successful trades close the daily plan.

    if plan["tp_wins"] >= MAX_TP_WINS:

        plan["finished"] = True

        for trade in plan["trades"]:

            if trade["status"] in ("WAITING", "ACTIVE"):

                trade["status"] = "CANCELLED"

        events.append(
            "🏆 3 مامەڵە TP بوون.\n"
            "🔒 پلانی ئەم ڕۆژە داخرا."
        )

    plan["last_5m_time"] = candle_time

    save_state()

    if events:

        message = (
            "🦅 XAUUSD | نوێکردنەوەی پلان\n\n"
            + "\n".join(events)
            + f"\n\n🏆 TP: {plan['tp_wins']}/3"
            + "\n⚠️ SIGNAL ONLY — NO ORDERS"
        )

        send_message(message)

        logging.info("Plan updated: %s", events)


def monitor_plan():

    plan = state.get("plan")

    if not plan or plan.get("finished"):
        return

    try:

        candles = fetch_closed_candles("5m", 100)

        last_time = plan.get("last_5m_time")

        new_candles = [
            candle
            for candle in candles
            if time_key(candle["time"]) > time_key(last_time)
        ]

        for candle in new_candles:

            if plan.get("finished"):
                break

            process_5m_candle(candle)

    except Exception:

        logging.exception("5M monitoring failed")


# ==========================================
# DAILY ANALYSIS
# ==========================================

def run_daily_analysis():

    try:

        result = analyze_daily()

        bias = result["bias"]

        if bias == "NEUTRAL":

            message = (
                "🦅 XAUUSD DAILY BIAS\n\n"
                "🎯 DAILY BIAS: NEUTRAL\n\n"
                f"Decision: {result['decision']}\n"
                f"Expansion: {result['expansion']}\n"
                f"Contracting: "
                f"{'YES' if result['contracting'] else 'NO'}\n\n"
                f"BUY votes: {result['buy_votes']}/2\n"
                f"SELL votes: {result['sell_votes']}/2\n\n"
                "⚪ هیچ پلانی بۆ ئەم ڕۆژە دروست نەکرا.\n\n"
                "⚠️ SIGNAL ONLY — NO ORDERS"
            )

            return send_message(message)

        # Create plan for BUY or SELL.

        plan = create_daily_plan(result)

        if not plan:
            return False

        state["plan"] = plan

        save_state()

        message = make_plan_message(plan)

        if send_message(message):

            logging.info(
                "New daily plan created: %s",
                bias
            )

            return True

        return False

    except Exception as error:

        logging.exception("Daily analysis failed")

        send_message(
            "⚠️ XAUUSD DAILY ANALYSIS ERROR\n\n"
            f"{str(error)}\n\n"
            "No new plan was created."
        )

        return False


# ==========================================
# TELEGRAM STATUS
# ==========================================

def get_status_message():

    now = datetime.now(BAGHDAD)

    plan = state.get("plan")

    message = (
        "🟡 XAUUSD BOT STATUS\n\n"
        "Mode: DAILY BIAS + 5 LEVELS\n"
        "Data: BiQuote\n"
        "Daily TF: 1D\n"
        "Monitoring TF: 5M\n"
        "Schedule: 07:00 Baghdad\n"
        f"Baghdad: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Last daily date: {state.get('last_daily_date')}\n"
    )

    if not plan:

        message += "\n📊 No active daily plan."

    else:

        message += (
            f"\n📊 Bias: {plan['bias']}"
            f"\n🎯 Reference: {plan['reference']:.2f}"
            f"\n🏆 TP: {plan['tp_wins']}/3"
            f"\n🔒 Finished: {plan['finished']}\n"
        )

        for trade in plan["trades"]:

            message += (
                f"\nLevel {trade['number']}: "
                f"{trade['entry']:.2f}"
                f" | {trade['status']}"
            )

    message += "\n\n⚠️ SIGNAL ONLY — NO ORDERS"

    return message


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

        text = message.get("text", "").strip()

        if not text:
            continue

        if str(chat.get("id")) != str(CHAT_ID):
            continue

        if text == "/start":

            send_message(
                "🦅 XAUUSD DAILY BIAS BOT\n\n"
                "/daily - Analyze and create plan\n"
                "/status - Bot status\n\n"
                "Daily: 07:00 Baghdad\n"
                "Monitoring: 5M\n"
                "SIGNAL ONLY — NO ORDERS"
            )

        elif text == "/daily":

            today = datetime.now(BAGHDAD).strftime("%Y-%m-%d")

            current_plan = state.get("plan")

            if (
                current_plan
                and current_plan.get("date") == today
            ):

                send_message(
                    "⚠️ پلانی ئەمڕۆ پێشتر دروست کراوە.\n\n"
                    "بۆ بینینی دۆخەکە /status بنێرە."
                )

            else:

                send_message("⏳ Daily analysis starting...")

                if run_daily_analysis():

                    state["last_daily_date"] = today

                    save_state()

        elif text == "/status":

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
        now.hour >= DAILY_HOUR
        and state.get("last_daily_date") != today
    ):

        logging.info("Starting scheduled daily analysis.")

        if run_daily_analysis():

            state["last_daily_date"] = today

            save_state()


# ==========================================
# MAIN
# ==========================================

def main():

    logging.info(
        "XAUUSD Daily Bias Bot started — SIGNAL ONLY."
    )

    if not BOT_TOKEN or not CHAT_ID:

        logging.error(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )

    send_message(
        "🦅 XAUUSD BOT STARTED\n\n"
        "Mode: DAILY BIAS + 5 LEVELS\n"
        "Schedule: 07:00 Baghdad\n"
        "Monitoring: 5M\n\n"
        "SIGNAL ONLY — NO ORDERS"
    )

    while True:

        try:

            handle_commands()

            check_daily_schedule()

            monitor_plan()

        except Exception:

            logging.exception("Main loop error")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
