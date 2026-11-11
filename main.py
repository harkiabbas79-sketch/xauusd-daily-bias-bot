import os
import time
import json
import math
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

# =========================================================
# ڕێکخستنە سەرەکییەکان
# =========================================================

BAGHDAD = ZoneInfo("Asia/Baghdad")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BIQUOTE_URL = "https://biquote.io/api/XAUUSD/ohlc"

TIMEFRAME = "5m"
RSI_PERIOD = 14

BUY_RSI = 30
SELL_RSI = 70

# بەپێی سیستەمی پێشووت: 10 pip = $1.00
PIP_SIZE = 0.10

# تارگێتەکان بە pip ـی کۆکراوە لە نرخی چوونەژوورەوە
TARGET_PIPS = [40, 60, 80, 100]

# ستۆپی RSI پێش TP1
BUY_STOP_RSI = 20
SELL_STOP_RSI = 80

# دوای TP1 تا TP3، گەڕانەوەی RSI ئاگادارکردنەوەی داخستن دەدات
BUY_CLOSE_RSI = 30
SELL_CLOSE_RSI = 70

POLL_SECONDS = 2
STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
telegram_offset = 0


# =========================================================
# دۆخی بۆت و ڕاپۆرتی هەفتانە
# =========================================================

def default_state():
    return {
        "bias": None,
        "armed": False,
        "pending_rearm": False,
        "trade": None,
        "weekly_stats": {},
        "last_weekly_report": None
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)

        result = default_state()
        if isinstance(saved, dict):
            result.update(saved)

        if not isinstance(result.get("weekly_stats"), dict):
            result["weekly_stats"] = {}

        return result

    except Exception:
        logging.exception("نەتوانرا دۆخی بۆت بخوێندرێتەوە")
        return default_state()


state = load_state()


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=4, ensure_ascii=False)

    except Exception:
        logging.exception("نەتوانرا دۆخی بۆت پاشەکەوت بکرێت")


def now_baghdad():
    return datetime.now(BAGHDAD)


def week_key(dt=None):
    dt = dt or now_baghdad()
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def empty_week_stats():
    return {
        "signals": 0,
        "buy_signals": 0,
        "sell_signals": 0,
        "stop_losses": 0,
        "closed_by_rsi": 0,
        "tp1": 0,
        "tp2": 0,
        "tp3": 0,
        "tp4": 0,
        "closed_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "total_pips": 0.0
    }


def get_week_stats(key=None):
    key = key or week_key()

    stats = state["weekly_stats"].get(key)

    if not isinstance(stats, dict):
        stats = empty_week_stats()
        state["weekly_stats"][key] = stats

    defaults = empty_week_stats()
    for name, value in defaults.items():
        stats.setdefault(name, value)

    return stats


# =========================================================
# تێلەگرام
# =========================================================

def telegram_request(method, data=None):
    if not TELEGRAM_TOKEN:
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"

    try:
        response = session.post(
            url,
            data=data or {},
            timeout=10
        )

        if response.status_code != 200:
            logging.error(
                "هەڵەی تێلەگرام: %s %s",
                response.status_code,
                response.text[:300]
            )
            return None

        result = response.json()

        if not result.get("ok"):
            logging.error(
                "هەڵەی تێلەگرام: %s",
                result.get("description")
            )
            return None

        return result.get("result")

    except Exception:
        logging.exception("پەیوەندی تێلەگرام سەرکەوتوو نەبوو")
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


# =========================================================
# یارمەتیدەرە ژمارەییەکان
# =========================================================

def number(value):
    try:
        result = float(value)

        if not math.isfinite(result):
            return 0.0

        return result

    except (TypeError, ValueError):
        return 0.0


def signed_pips(direction, entry, exit_price):
    if direction == "BUY":
        return (exit_price - entry) / PIP_SIZE

    return (entry - exit_price) / PIP_SIZE


# =========================================================
# داتای کندڵی 5 خولەکی BiQuote
# =========================================================

def extract_candles(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("bars", "data", "candles", "ohlc", "result"):
            value = data.get(key)

            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                for nested_key in ("bars", "candles", "data"):
                    nested = value.get(nested_key)

                    if isinstance(nested, list):
                        return nested

        raise RuntimeError(
            "هیچ لیستی کندڵ نەدۆزرایەوە. "
            f"کلیلەکان: {list(data.keys())}"
        )

    raise RuntimeError(
        f"جۆری وەڵامی BiQuote نادیارە: {type(data).__name__}"
    )


def sort_candles(candles):
    valid = []

    for candle in candles:
        if not isinstance(candle, dict):
            continue

        close = number(candle.get("close"))

        if close <= 0:
            continue

        valid.append(candle)

    if not valid:
        raise RuntimeError("هیچ کندڵێکی دروست نەدۆزرایەوە")

    try:
        valid.sort(
            key=lambda candle: datetime.fromisoformat(
                str(candle["openTime"]).replace("Z", "+00:00")
            )
        )

    except (KeyError, ValueError, TypeError):
        logging.warning(
            "کاتی کندڵەکان بەردەست نییە؛ ڕیزبەندی API بە پێچەوانە دەکرێت"
        )
        valid.reverse()

    return valid


def get_5m_candles():
    response = session.get(
        BIQUOTE_URL,
        params={
            "interval": TIMEFRAME,
            "limit": 200
        },
        timeout=15
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"هەڵەی BiQuote HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            "وەڵامی BiQuote JSON نییە: " + response.text[:300]
        )

    candles = sort_candles(extract_candles(data))

    if len(candles) < RSI_PERIOD + 1:
        raise RuntimeError(
            f"ژمارەی کندڵەکان بەس نییە: {len(candles)}"
        )

    return candles


# =========================================================
# هەژمارکردنی RSI 14
# =========================================================

def calculate_rsi(candles):
    closes = [
        number(candle.get("close"))
        for candle in candles
    ]

    if len(closes) < RSI_PERIOD + 1:
        raise RuntimeError("داتای پێویستی RSI بەس نییە")

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(change, 0) for change in changes]
    losses = [abs(min(change, 0)) for change in changes]

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


# =========================================================
# تارگێتەکان
# =========================================================

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


# =========================================================
# دروستکردنی سیگناڵ
# =========================================================

def create_signal(direction, entry, rsi):
    entry = round(entry, 2)
    targets = calculate_targets(direction, entry)

    started_at = now_baghdad()

    state["trade"] = {
        "direction": direction,
        "entry": entry,
        "rsi": round(rsi, 2),
        "targets": targets,
        "next_target": 0,
        "highest_price": entry,
        "lowest_price": entry,
        "started": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "week_key": week_key(started_at)
    }

    state["armed"] = False
    state["pending_rearm"] = False

    stats = get_week_stats(week_key(started_at))
    stats["signals"] += 1

    if direction == "BUY":
        stats["buy_signals"] += 1
    else:
        stats["sell_signals"] += 1

    save_state()

    emoji = "🟢" if direction == "BUY" else "🔴"

    target_text = ""
    for i, target in enumerate(targets, 1):
        target_text += (
            f"🎯 تارگێتی {i}: {target['price']:.2f} "
            f"({target['pips']} pip)\n"
        )

    message = (
        "🦅 سیگناڵی RSI بۆ زێڕ XAUUSD\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{emoji} ئاراستە: {direction}\n"
        f"📍 نرخی چوونەژوورەوە: {entry:.2f}\n"
        f"📊 RSI 5M: {rsi:.2f}\n\n"
        f"{target_text}\n"
        "🛑 ستۆپی RSI پێش تارگێتی یەکەم:\n"
        + (
            f"BUY: RSI {BUY_STOP_RSI} یان خوارتر\n"
            if direction == "BUY"
            else f"SELL: RSI {SELL_STOP_RSI} یان سەرەوە\n"
        )
        + "\n"
        "⚠️ تەنها سیگناڵە؛ هیچ ئۆردەرێک ناکات."
    )

    send_message(message)

    logging.info(
        "سیگناڵی %s لە %.2f دروستکرا، RSI=%.2f",
        direction,
        entry,
        rsi
    )


# =========================================================
# چاودێری بەرزترین و نزمترین نرخ
# =========================================================

def update_price_extremes(current_price):
    trade = state.get("trade")

    if not trade:
        return

    highest = number(trade.get("highest_price", trade["entry"]))
    lowest = number(trade.get("lowest_price", trade["entry"]))

    if current_price > highest:
        trade["highest_price"] = round(current_price, 2)

    if current_price < lowest:
        trade["lowest_price"] = round(current_price, 2)

    save_state()


# =========================================================
# تۆمارکردنی ئەنجامی مامەڵە لە ڕاپۆرتی هەفتانە
# =========================================================

def record_trade_result(trade, result_pips, reason):
    key = trade.get("week_key") or week_key()
    stats = get_week_stats(key)

    targets_reached = int(trade.get("next_target", 0))

    stats["tp1"] += 1 if targets_reached >= 1 else 0
    stats["tp2"] += 1 if targets_reached >= 2 else 0
    stats["tp3"] += 1 if targets_reached >= 3 else 0
    stats["tp4"] += 1 if targets_reached >= 4 else 0

    if reason == "STOP":
        stats["stop_losses"] += 1

    if reason == "RSI_CLOSE":
        stats["closed_by_rsi"] += 1

    stats["closed_trades"] += 1
    stats["total_pips"] = round(
        number(stats["total_pips"]) + result_pips,
        1
    )

    if result_pips > 0:
        stats["winning_trades"] += 1
    elif result_pips < 0:
        stats["losing_trades"] += 1

    save_state()


# =========================================================
# کۆتایی مامەڵە و چاوەڕوانی بایەسی نوێ
# =========================================================

def finish_trade(reason, current_price, rsi):
    trade = state.get("trade")

    if not trade:
        return

    direction = trade["direction"]
    entry = number(trade["entry"])
    target_count = int(trade.get("next_target", 0))

    if reason == "STOP":
        result_pips = signed_pips(direction, entry, current_price)

        message = (
            "🛑 ستۆپی RSI چالاک بوو\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 ئاراستە: {direction}\n"
            f"📍 نرخی چوونەژوورەوە: {entry:.2f}\n"
            f"📍 نرخی ستۆپ: {current_price:.2f}\n"
            f"📊 RSI: {rsi:.2f}\n"
            f"📉 ئەنجامی نرخی مامەڵە: {result_pips:+.1f} pip\n\n"
            "❌ پێش تارگێتی یەکەم ستۆپی RSI ڕوویدا.\n"
        )

    elif reason == "RSI_CLOSE":
        result_pips = float(TARGET_PIPS[target_count - 1])

        message = (
            "⚠️ ئاگادارکردنەوەی داخستنی مامەڵە\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 ئاراستە: {direction}\n"
            f"📍 نرخی چوونەژوورەوە: {entry:.2f}\n"
            f"🎯 دوا تارگێتی گەیشتوو: {target_count}\n"
            f"📊 RSI ئێستا: {rsi:.2f}\n"
            f"💰 قازانجی هەژمارکراو: +{result_pips:.0f} pip\n\n"
            "🔔 تکایە ئەگەر پۆزیشنێکت هەیە، بیخە.\n"
        )

    else:  # TP4
        result_pips = 100.0

        message = (
            "🏆 هەموو تارگێتەکان تەواو بوون\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 ئاراستە: {direction}\n"
            f"📍 نرخی چوونەژوورەوە: {entry:.2f}\n"
            f"🎯 تارگێتی چوارەم: {trade['targets'][3]['price']:.2f}\n"
            "💰 ئەنجام: +100 pip\n\n"
            "✅ پلانی مامەڵە تەواو بوو.\n"
        )

    record_trade_result(trade, result_pips, reason)

    message += (
        "\n⏳ چاوەڕێی بایەسی ڕۆژی نوێ:\n"
        "Bias Buy\n"
        "یان\n"
        "Bias Sell"
    )

    send_message(message)

    logging.info(
        "مامەڵە کۆتایی هات: reason=%s result=%.1f pips",
        reason,
        result_pips
    )

    state["trade"] = None

    # ئەگەر لە کاتی مامەڵەدا بایەسێکی نوێ نێردرابێت،
    # دوای کۆتایی مامەڵە ئەو بایەسە چالاک دەبێت.
    if state.get("pending_rearm") and state.get("bias"):
        state["armed"] = True
        state["pending_rearm"] = False

        send_message(
            "✅ بایەسی نوێ چالاک بوو.\n"
            "⏳ چاوەڕێی مەرجی RSI 5M."
        )

    else:
        state["bias"] = None
        state["armed"] = False
        state["pending_rearm"] = False

    save_state()


# =========================================================
# چاودێری تارگێت و ستۆپی RSI
# =========================================================

def monitor_active_trade(current_price, rsi):
    trade = state.get("trade")

    if not trade:
        return

    direction = trade["direction"]
    targets = trade["targets"]
    next_target = int(trade.get("next_target", 0))

    update_price_extremes(current_price)

    # یەکەم جار دەبینین نرخ گەیشتووەتە تارگێت یان نا.
    while next_target < len(targets):
        target = targets[next_target]
        target_price = number(target["price"])

        if direction == "BUY":
            reached = current_price >= target_price
        else:
            reached = current_price <= target_price

        if not reached:
            break

        target_number = next_target + 1

        message = (
            "🎉 تارگێت گەیشت\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 ئاراستە: {direction}\n"
            f"📍 نرخی چوونەژوورەوە: {trade['entry']:.2f}\n"
            f"🎯 تارگێتی {target_number}: {target_price:.2f}\n"
            f"💰 قازانجی کۆکراوە: +{target['pips']} pip\n\n"
        )

        if target_number < 4:
            message += (
                "⏳ چاوەڕێی تارگێتی دواتر.\n"
                "🚫 سیگناڵی نوێ نانێردرێت."
            )
        else:
            message += "🏆 هەموو تارگێتەکان تەواو بوون!"

        send_message(message)

        next_target += 1
        trade["next_target"] = next_target
        save_state()

    # TP4: مامەڵە تەواوە و چاوەڕێی بایەسی نوێ.
    if next_target >= 4:
        finish_trade("TP4", current_price, rsi)
        return

    # پێش TP1: ستۆپی RSI.
    if next_target == 0:
        if direction == "BUY" and rsi <= BUY_STOP_RSI:
            finish_trade("STOP", current_price, rsi)
            return

        if direction == "SELL" and rsi >= SELL_STOP_RSI:
            finish_trade("STOP", current_price, rsi)
            return

    # دوای TP1، TP2 یان TP3: گەڕانەوەی RSI ئاگادارکردنەوەی داخستن دەدات.
    elif 1 <= next_target <= 3:
        if direction == "BUY" and rsi <= BUY_CLOSE_RSI:
            finish_trade("RSI_CLOSE", current_price, rsi)
            return

        if direction == "SELL" and rsi >= SELL_CLOSE_RSI:
            finish_trade("RSI_CLOSE", current_price, rsi)
            return


# =========================================================
# چاودێری بازاڕ
# =========================================================

def monitor_market():
    # بۆت لە شەممە و یەکشەممە سیگناڵی بازاڕ نانێرێت.
    if now_baghdad().weekday() >= 5:
        return

    if not state.get("bias"):
        return

    if not state.get("armed") and not state.get("trade"):
        return

    candles = get_5m_candles()
    latest = candles[-1]

    current_price = number(latest.get("close"))

    if current_price <= 0:
        raise RuntimeError("نرخی XAUUSD دروست نییە")

    rsi = calculate_rsi(candles)

    # ئەگەر مامەڵەیەک چالاکە، تەنها ئەو مامەڵەیە چاودێری دەکرێت.
    if state.get("trade"):
        monitor_active_trade(current_price, rsi)
        return

    direction = state["bias"]

    logging.info(
        "Bias=%s RSI=%.2f Price=%.2f Candles=%s",
        direction,
        rsi,
        current_price,
        len(candles)
    )

    if direction == "BUY" and rsi <= BUY_RSI:
        create_signal("BUY", current_price, rsi)

    elif direction == "SELL" and rsi >= SELL_RSI:
        create_signal("SELL", current_price, rsi)


# =========================================================
# دانانی بایەسی ڕۆژانە
# =========================================================

def set_bias(direction):
    old_bias = state.get("bias")
    state["bias"] = direction

    if state.get("trade"):
        state["pending_rearm"] = True
        save_state()

        send_message(
            f"✅ بایەسی نوێ دانرا: {direction}\n\n"
            "مامەڵەی پێشوو هێشتا چالاکە.\n"
            "تا کۆتایی ئەو مامەڵەیە سیگناڵی نوێ نانێردرێت.\n"
            "دوای کۆتایی، بایەسی نوێ چالاک دەبێت."
        )
        return

    state["armed"] = True
    state["pending_rearm"] = False

    save_state()

    message = (
        "🦅 بایەسی ڕۆژانە نوێ کرایەوە\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 بایەس: {direction}\n"
        "⏱ چوارچێوە: 5M\n"
        "📈 RSI: 14\n\n"
    )

    if direction == "BUY":
        message += (
            "🟢 چاوەڕێی RSI 30 یان خوارتر.\n"
            "🛑 ستۆپ پێش TP1: RSI 20 یان خوارتر.\n"
            "🔔 دوای TP1 تا TP3: گەڕانەوەی RSI بۆ 30 داخستن ئاگادار دەکاتەوە."
        )
    else:
        message += (
            "🔴 چاوەڕێی RSI 70 یان سەرەوە.\n"
            "🛑 ستۆپ پێش TP1: RSI 80 یان سەرەوە.\n"
            "🔔 دوای TP1 تا TP3: گەڕانەوەی RSI بۆ 70 داخستن ئاگادار دەکاتەوە."
        )

    message += (
        "\n\n🎯 تارگێتەکان: 40 / 60 / 80 / 100 pip\n"
        "🚫 هیچ ئۆردەرێکی ئۆتۆماتیکی ناکات."
    )

    send_message(message)

    logging.info("Bias گۆڕا لە %s بۆ %s", old_bias, direction)


# =========================================================
# هەڵوەشاندنەوەی بایەس و مامەڵە
# =========================================================

def cancel_everything():
    had_bias = bool(state.get("bias"))
    had_trade = bool(state.get("trade"))

    state["bias"] = None
    state["armed"] = False
    state["pending_rearm"] = False
    state["trade"] = None

    save_state()

    if had_trade:
        send_message(
            "🛑 مامەڵە هەڵوەشێندرایەوە\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "چاودێری تارگێتەکان وەستا.\n"
            "هیچ ئەنجامێک بۆ ئەم مامەڵەیە تۆمار ناکرێت.\n"
            "بایەسی ڕۆژانەش هەڵوەشێندرایەوە."
        )

    elif had_bias:
        send_message(
            "🛑 بایەسی ڕۆژانە هەڵوەشێندرایەوە.\n"
            "⏳ چاوەڕێی بایەسی نوێ."
        )

    else:
        send_message("ℹ️ هیچ بایەس یان مامەڵەیەکی چالاک نییە.")

    logging.info("بایەس و مامەڵە هەڵوەشێندرایەوە")


# =========================================================
# دۆخی ئێستای بۆت
# =========================================================

def send_status():
    bias = state.get("bias") or "دانەنراوە"
    trade = state.get("trade")

    if trade:
        direction = trade["direction"]
        entry = number(trade["entry"])
        completed = int(trade.get("next_target", 0))

        trade_text = (
            f"مامەڵەی چالاک: {direction}\n"
            f"نرخی چوونەژوورەوە: {entry:.2f}\n"
            f"تارگێتە تەواوبووەکان: {completed}/4\n"
        )
    else:
        trade_text = "مامەڵەی چالاک: نییە\n"

    send_message(
        "🦅 دۆخی بۆتی زێڕ\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"بایەسی ڕۆژانە: {bias}\n"
        f"{trade_text}\n"
        "چوارچێوە: 5M\n"
        "RSI: 14\n"
        "BUY: 30 یان خوارتر\n"
        "SELL: 70 یان سەرەوە\n\n"
        "ستۆپی BUY: RSI 20 پێش TP1\n"
        "ستۆپی SELL: RSI 80 پێش TP1\n\n"
        "تارگێتەکان: 40 / 60 / 80 / 100 pip\n"
        "ئۆردەری ئۆتۆماتیکی: ناچالاک\n\n"
        "/weekly - ڕاپۆرتی هەفتانە\n"
        "/cancel - هەڵوەشاندنەوەی هەموو شتێک"
    )


# =========================================================
# ڕاپۆرتی هەفتانە: دووشەممە تا هەینی
# =========================================================

def send_weekly_report(key=None, scheduled=False):
    key = key or week_key()
    stats = get_week_stats(key)

    closed = int(stats["closed_trades"])
    wins = int(stats["winning_trades"])
    losses = int(stats["losing_trades"])

    if closed > 0:
        win_rate = (wins / closed) * 100
    else:
        win_rate = 0.0

    total_pips = number(stats["total_pips"])
    result_emoji = "🟢" if total_pips > 0 else (
        "🔴" if total_pips < 0 else "⚪"
    )

    active_trade_text = "نییە"
    trade = state.get("trade")
    if trade and trade.get("week_key") == key:
        active_trade_text = (
            f"{trade['direction']} لە {number(trade['entry']):.2f} "
            f"(تارگێتە تەواوبووەکان: {trade['next_target']}/4)"
        )

    report = (
        "📊 ڕاپۆرتی هەفتانەی زێڕ XAUUSD\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 هەفتە: {key}\n"
        "🗓 ماوە: دووشەممە تا هەینی\n\n"
        "📌 ژمارەی سیگناڵەکان\n"
        f"🔢 کۆی سیگناڵ: {stats['signals']}\n"
        f"🟢 BUY: {stats['buy_signals']}\n"
        f"🔴 SELL: {stats['sell_signals']}\n\n"
        "🎯 تارگێتە گەیشتووەکان\n"
        f"TP1: {stats['tp1']}\n"
        f"TP2: {stats['tp2']}\n"
        f"TP3: {stats['tp3']}\n"
        f"TP4: {stats['tp4']}\n\n"
        "🛑 کۆتایی مامەڵەکان\n"
        f"ستۆپی RSI پێش TP1: {stats['stop_losses']}\n"
        f"داخستن بەهۆی گەڕانەوەی RSI: {stats['closed_by_rsi']}\n"
        f"مامەڵە داخراوەکان: {closed}\n"
        f"مامەڵە قازانجدارەکان: {wins}\n"
        f"مامەڵە زیانەکان: {losses}\n"
        f"ڕێژەی سەرکەوتن: {win_rate:.1f}%\n\n"
        f"{result_emoji} کۆی ئەنجام: {total_pips:+.1f} pip\n\n"
        f"⏳ مامەڵەی هێشتا چالاک: {active_trade_text}\n\n"
        "⚠️ ئەم ڕاپۆرتە بەپێی نرخی سیگناڵ و تارگێتەکانە؛ "
        "کرێی بروکەر و سپرێد لە حیسابدا نییە.\n"
        "🚫 بۆت تەنها سیگناڵ و ئاگادارکردنەوە دەنێرێت."
    )

    if scheduled:
        report = "🕙 ڕاپۆرتی کۆتایی هەفتە — کاتژمێر 10ی شەو\n\n" + report

    send_message(report)

    if scheduled:
        state["last_weekly_report"] = key
        save_state()


def check_scheduled_weekly_report():
    now = now_baghdad()

    # 4 = هەینی؛ ڕاپۆرت لە 22:00 یان دوای ئەو کاتە دەنێردرێت.
    if now.weekday() != 4:
        return

    if now.hour < 22:
        return

    key = week_key(now)

    if state.get("last_weekly_report") == key:
        return

    send_weekly_report(key, scheduled=True)


# =========================================================
# وەرگرتنی نامەکانی تێلەگرام
# =========================================================

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


# =========================================================
# مامەڵەکردن لەگەڵ فەرمانەکانی بەکارهێنەر
# =========================================================

def handle_message(message):
    text = message.get("text", "").strip()

    chat_id = str(message.get("chat", {}).get("id", ""))

    if not text:
        return

    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return

    normalized = " ".join(text.upper().split())

    if normalized == "BIAS BUY":
        set_bias("BUY")

    elif normalized == "BIAS SELL":
        set_bias("SELL")

    elif normalized == "/CANCEL":
        cancel_everything()

    elif normalized == "/STATUS":
        send_status()

    elif normalized == "/WEEKLY":
        send_weekly_report()

    elif normalized in ("/START", "/HELP"):
        send_message(
            "🦅 بۆتی سیگناڵی زێڕ XAUUSD\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📌 دانانی بایەس:\n"
            "Bias Buy\n"
            "Bias Sell\n\n"
            "📊 RSI 5M / 14\n"
            "BUY: RSI 30 یان خوارتر\n"
            "SELL: RSI 70 یان سەرەوە\n\n"
            "🛑 ستۆپی پێش TP1:\n"
            "BUY: RSI 20 یان خوارتر\n"
            "SELL: RSI 80 یان سەرەوە\n\n"
            "🔔 دوای TP1 تا TP3:\n"
            "BUY: گەڕانەوەی RSI بۆ 30\n"
            "SELL: گەڕانەوەی RSI بۆ 70\n\n"
            "🎯 تارگێتەکان:\n"
            "40 / 60 / 80 / 100 pip\n\n"
            "📊 فەرمانەکان:\n"
            "/status - دۆخی بۆت\n"
            "/weekly - ڕاپۆرتی هەفتانە\n"
            "/cancel - هەڵوەشاندنەوە\n\n"
            "🕙 ڕاپۆرتی هەفتانە: هەینی 10ی شەو\n"
            "🗓 چاودێری بازاڕ: دووشەممە تا هەینی\n"
            "🚫 تەنها سیگناڵە؛ هیچ ئۆردەرێک ناکات."
        )

    else:
        send_message(
            "⚠️ فەرمانەکە نەناسراوە.\n\n"
            "ئەم فەرمانانە بەکاربهێنە:\n"
            "Bias Buy\n"
            "Bias Sell\n"
            "/status\n"
            "/weekly\n"
            "/cancel"
        )


# =========================================================
# دەستپێکردنی بۆت
# =========================================================

def main():
    global telegram_offset

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error(
            "TELEGRAM_BOT_TOKEN یان TELEGRAM_CHAT_ID دانەنراوە"
        )
        return

    logging.info("بۆتی XAUUSD RSI 5M دەست پێدەکات")

    telegram_request(
        "deleteWebhook",
        {"drop_pending_updates": "false"}
    )

    send_message(
        "🦅 بۆتی سیگناڵی زێڕ چالاک بوو\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📊 چاودێری RSI 5M / 14\n"
        "🟢 BUY: RSI 30 یان خوارتر\n"
        "🔴 SELL: RSI 70 یان سەرەوە\n\n"
        "🛑 ستۆپی پێش TP1:\n"
        "BUY: RSI 20 یان خوارتر\n"
        "SELL: RSI 80 یان سەرەوە\n\n"
        "🎯 تارگێتەکان: 40 / 60 / 80 / 100 pip\n"
        "🕙 ڕاپۆرتی هەفتانە: هەینی 10ی شەو\n"
        "🗓 چاودێری بازاڕ: دووشەممە تا هەینی\n\n"
        "تکایە بایەسی ڕۆژانە بنێرە:\n"
        "Bias Buy\n"
        "یان\n"
        "Bias Sell\n\n"
        "/status - دۆخی بۆت\n"
        "/weekly - ڕاپۆرتی هەفتانە\n"
        "/cancel - هەڵوەشاندنەوە\n\n"
        "🚫 هیچ ئۆردەرێکی ئۆتۆماتیکی ناکات."
    )

    while True:
        try:
            updates = get_updates()

            for update in updates:
                telegram_offset = update["update_id"] + 1

                message = update.get("message")
                if message:
                    handle_message(message)

            # ڕاپۆرتی خۆکار هەینی کاتژمێر 22:00
            check_scheduled_weekly_report()

            # چاودێری بازاڕ لە دووشەممە تا هەینی
            try:
                monitor_market()
            except Exception as error:
                logging.warning(
                    "هەڵە لە چاودێری بازاڕ: %s",
                    str(error)
                )

        except Exception:
            logging.exception("هەڵە لە بازنەی سەرەکی بۆت")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
