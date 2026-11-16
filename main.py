import os, time, json, math, logging, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# XAUUSD RSI 5M LIVE - SIGNAL ONLY (NO AUTOMATIC ORDERS)
BAGHDAD = ZoneInfo("Asia/Baghdad")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BASE = "https://biquote.io/api"
OHLC_URL = BASE + "/XAUUSD/ohlc"
TICK_URL = BASE + "/XAUUSD"

RSI_PERIOD = 14
BUY_RSI, SELL_RSI = 30.0, 70.0
BUY_STOP_RSI, SELL_STOP_RSI = 20.0, 80.0
BUY_CLOSE_RSI, SELL_CLOSE_RSI = 30.0, 70.0
PIP_SIZE = 0.10
TARGET_PIPS = [40, 60, 80, 100]
POLL_SECONDS = 5
MAX_TICK_AGE = 120
MAX_CANDLE_AGE = 600
STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
http = requests.Session()
offset = 0


def now_baghdad():
    return datetime.now(BAGHDAD)


def now_utc():
    return datetime.now(timezone.utc)


def number(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def parse_utc(v):
    if not v:
        raise RuntimeError("API timestamp missing")
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise RuntimeError("API timestamp has no timezone")
    return d.astimezone(timezone.utc)


def default_state():
    return {"bias": None, "armed": False, "pending_rearm": False,
            "trade": None, "weekly_stats": {}, "last_weekly_report": None,
            "last_signal_candle": None}


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        result = default_state()
        if isinstance(saved, dict):
            result.update(saved)
        if not isinstance(result.get("weekly_stats"), dict):
            result["weekly_stats"] = {}
        return result
    except Exception:
        logging.exception("State load failed")
        return default_state()


state = load_state()


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        logging.exception("State save failed")


def week_key(dt=None):
    iso = (dt or now_baghdad()).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def empty_stats():
    return {"signals": 0, "buy_signals": 0, "sell_signals": 0,
            "stop_losses": 0, "closed_by_rsi": 0,
            "tp1": 0, "tp2": 0, "tp3": 0, "tp4": 0,
            "closed_trades": 0, "winning_trades": 0,
            "losing_trades": 0, "total_pips": 0.0}


def stats_for(key=None):
    key = key or week_key()
    stats = state["weekly_stats"].get(key)
    if not isinstance(stats, dict):
        stats = empty_stats()
        state["weekly_stats"][key] = stats
    for k, v in empty_stats().items():
        stats.setdefault(k, v)
    return stats


# ---------------- TELEGRAM ----------------
def tg(method, data=None):
    if not TOKEN:
        return None
    try:
        response = http.post(
            f"https://api.telegram.org/bot{TOKEN}/{method}",
            data=data or {}, timeout=15
        )
        if response.status_code != 200:
            logging.error("Telegram HTTP %s: %s", response.status_code, response.text[:200])
            return None
        payload = response.json()
        if not payload.get("ok"):
            logging.error("Telegram API: %s", payload.get("description"))
            return None
        return payload.get("result")
    except Exception:
        logging.exception("Telegram request failed")
        return None


def send(message):
    return tg("sendMessage", {"chat_id": CHAT_ID, "text": message}) is not None


# ---------------- MARKET DATA ----------------
def get_5m_candles():
    # Closed 5-minute candles provide RSI history.
    response = http.get(
        OHLC_URL, params={"interval": "5m", "limit": 300}, timeout=15
    )
    if response.status_code != 200:
        raise RuntimeError(f"OHLC HTTP {response.status_code}: {response.text[:180]}")

    data = response.json()
    if not isinstance(data, dict) or str(data.get("symbol", "")).upper() != "XAUUSD":
        raise RuntimeError("Unexpected OHLC response/symbol")
    if str(data.get("interval", "")).lower() != "5m":
        raise RuntimeError(f"API interval is not 5m: {data.get('interval')}")

    bars = data.get("bars")
    if not isinstance(bars, list):
        raise RuntimeError("OHLC bars list missing")

    valid = []
    for bar in bars:
        if not isinstance(bar, dict) or not isinstance(bar.get("isOpen"), bool):
            continue
        try:
            stamp = parse_utc(bar.get("openTime"))
            o, h, low, close = [float(bar[k]) for k in ("open", "high", "low", "close")]
            if not all(math.isfinite(x) and x > 0 for x in (o, h, low, close)):
                continue
            if h < max(o, close, low) or low > min(o, close, h):
                continue
            valid.append({
                "time": stamp, "open": o, "high": h, "low": low,
                "close": close, "isOpen": bar["isOpen"]
            })
        except (KeyError, TypeError, ValueError, RuntimeError):
            continue

    valid.sort(key=lambda x: x["time"])
    dedup = {bar["time"]: bar for bar in valid}
    valid = sorted(dedup.values(), key=lambda x: x["time"])
    closed = [bar for bar in valid if bar["isOpen"] is False]

    if len(closed) < RSI_PERIOD + 20:
        raise RuntimeError("Not enough closed 5m candles")

    latest = closed[-1]
    age = (now_utc() - (latest["time"] + timedelta(minutes=5))).total_seconds()
    if age < -60:
        raise RuntimeError("Latest closed candle timestamp is in future")
    if age > MAX_CANDLE_AGE:
        raise RuntimeError(f"Closed candle stale ({age:.0f}s)")

    for a, b in zip(closed[-6:], closed[-5:]):
        gap = (b["time"] - a["time"]).total_seconds()
        if gap <= 0 or gap > 900:
            raise RuntimeError(f"Invalid recent candle spacing ({gap:.0f}s)")

    return closed[-250:]


def get_live_quote():
    response = http.get(
        TICK_URL, params={"allowStale": "false"}, timeout=10
    )
    if response.status_code != 200:
        raise RuntimeError(f"Live quote HTTP {response.status_code}: {response.text[:180]}")

    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected live quote response")

    price = number(data.get("mid"))
    if price <= 0:
        raise RuntimeError("Live mid price missing")
    if data.get("stale") is True:
        raise RuntimeError("Live quote marked stale")

    market = str(data.get("marketState", "")).lower()
    if market and market != "open":
        raise RuntimeError(f"Market state is {market}")

    quote_time = parse_utc(data.get("timestamp"))
    age = (now_utc() - quote_time).total_seconds()
    if age < -10 or age > MAX_TICK_AGE:
        raise RuntimeError(f"Live quote timestamp not fresh ({age:.0f}s)")

    api_age = data.get("quoteAgeSeconds")
    if api_age is not None and number(api_age) > MAX_TICK_AGE:
        raise RuntimeError(f"API quote age too high ({api_age})")

    return price, quote_time


# ---------------- LIVE RSI 5M / 14 ----------------
def calculate_rsi_from_closes(closes):
    if len(closes) < RSI_PERIOD + 1:
        raise RuntimeError("Not enough RSI data")

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD

    for i in range(RSI_PERIOD, len(gains)):
        avg_gain = (avg_gain * (RSI_PERIOD - 1) + gains[i]) / RSI_PERIOD
        avg_loss = (avg_loss * (RSI_PERIOD - 1) + losses[i]) / RSI_PERIOD

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_live_rsi(closed_candles, live_price):
    # Append the current quote as the forming 5m candle's live close.
    closes = [float(candle["close"]) for candle in closed_candles]
    closes.append(float(live_price))
    return calculate_rsi_from_closes(closes)


def current_5m_candle_start(quote_time):
    utc_time = quote_time.astimezone(timezone.utc)
    minute = (utc_time.minute // 5) * 5
    return utc_time.replace(minute=minute, second=0, microsecond=0)


# ---------------- TARGETS ----------------
def targets_for(direction, entry):
    targets = []
    for pips in TARGET_PIPS:
        delta = pips * PIP_SIZE
        price = entry + delta if direction == "BUY" else entry - delta
        targets.append({"pips": pips, "price": round(price, 2)})
    return targets


def signed_pips(direction, entry, exit_price):
    move = exit_price - entry if direction == "BUY" else entry - exit_price
    return move / PIP_SIZE


# ---------------- SIGNAL CREATION ----------------
def create_signal(direction, price, rsi, quote_time):
    entry = round(price, 2)
    local_time = quote_time.astimezone(BAGHDAD)
    candle_time = current_5m_candle_start(quote_time)
    key = week_key(local_time)

    trade = {
        "direction": direction,
        "entry": entry,
        "rsi": round(rsi, 2),
        "targets": targets_for(direction, entry),
        "next_target": 0,
        "highest_price": entry,
        "lowest_price": entry,
        "started": local_time.strftime("%Y-%m-%d %H:%M:%S"),
        "week_key": key,
        "signal_candle": candle_time.isoformat(),
        "quote_time": quote_time.isoformat()
    }

    state["trade"] = trade
    state["armed"] = False
    state["pending_rearm"] = False
    state["last_signal_candle"] = candle_time.isoformat()

    stats = stats_for(key)
    stats["signals"] += 1
    stats["buy_signals" if direction == "BUY" else "sell_signals"] += 1
    save_state()

    emoji = "🟢" if direction == "BUY" else "🔴"
    target_lines = "".join(
        f"🎯 تارگێتی {i}: {target['price']:.2f} ({target['pips']} pip)\n"
        for i, target in enumerate(trade["targets"], 1)
    )
    stop_text = (
        f"BUY: RSI {BUY_STOP_RSI} یان خوارتر"
        if direction == "BUY"
        else f"SELL: RSI {SELL_STOP_RSI} یان سەرەوە"
    )

    send(
        "🦅 سیگناڵی RSI بۆ زێڕ XAUUSD\n━━━━━━━━━━━━━━━━━━\n\n"
        f"{emoji} ئاراستە: {direction}\n"
        f"📍 نرخی چوونەژوورەوە: {entry:.2f}\n"
        f"📊 RSI 5M: {rsi:.2f}\n"
        f"🕒 کاتی سیگناڵ: {local_time:%Y-%m-%d %H:%M:%S}\n"
        f"🕒 کندڵی 5M: {candle_time.astimezone(BAGHDAD):%H:%M}\n\n"
        f"{target_lines}\n"
        f"🛑 ستۆپی RSI پێش TP1:\n{stop_text}\n\n"
        "⚠️ سیگناڵی فەورییە؛ هیچ ئۆردەرێک ناکات."
    )

    logging.info(
        "LIVE SIGNAL %s price=%.2f RSI=%.2f quote=%s",
        direction, entry, rsi, quote_time.isoformat()
    )


# ---------------- TRADE MONITORING ----------------
def update_extremes(price):
    trade = state.get("trade")
    if not trade:
        return
    trade["highest_price"] = round(
        max(number(trade.get("highest_price", trade["entry"])), price), 2
    )
    trade["lowest_price"] = round(
        min(number(trade.get("lowest_price", trade["entry"])), price), 2
    )
    save_state()


def record_result(trade, pips, reason):
    stats = stats_for(trade.get("week_key") or week_key())
    reached = int(trade.get("next_target", 0))
    for i in range(1, 5):
        stats[f"tp{i}"] += int(reached >= i)

    if reason == "STOP":
        stats["stop_losses"] += 1
    if reason == "RSI_CLOSE":
        stats["closed_by_rsi"] += 1

    stats["closed_trades"] += 1
    stats["total_pips"] = round(number(stats["total_pips"]) + pips, 1)
    if pips > 0:
        stats["winning_trades"] += 1
    elif pips < 0:
        stats["losing_trades"] += 1
    save_state()


def finish_trade(reason, price, rsi):
    trade = state.get("trade")
    if not trade:
        return

    direction = trade["direction"]
    entry = number(trade["entry"])
    reached = int(trade.get("next_target", 0))

    if reason == "STOP":
        pips = signed_pips(direction, entry, price)
        message = (
            "🛑 ستۆپی RSI چالاک بوو\n━━━━━━━━━━━━━━━━━━\n\n"
            f"ئاراستە: {direction}\nچوونەژوورەوە: {entry:.2f}\n"
            f"نرخی ئێستا: {price:.2f}\nRSI: {rsi:.2f}\n"
            f"ئەنجامی نرخی مامەڵە: {pips:+.1f} pip\n"
        )
    elif reason == "RSI_CLOSE":
        pips = float(TARGET_PIPS[reached - 1])
        message = (
            "⚠️ ئاگادارکردنەوەی داخستنی مامەڵە\n━━━━━━━━━━━━━━━━━━\n\n"
            f"ئاراستە: {direction}\nچوونەژوورەوە: {entry:.2f}\n"
            f"دوا تارگێتی گەیشتوو: {reached}\nRSI: {rsi:.2f}\n"
            f"قازانجی هەژمارکراو: +{pips:.0f} pip\n"
            "ئەگەر پۆزیشنێکت هەیە، بە دەستی دایبخە.\n"
        )
    else:
        pips = 100.0
        message = (
            "🏆 هەموو تارگێتەکان تەواو بوون\n━━━━━━━━━━━━━━━━━━\n\n"
            f"ئاراستە: {direction}\nچوونەژوورەوە: {entry:.2f}\n"
            "ئەنجام: +100 pip\n"
        )

    record_result(trade, pips, reason)
    send(message + "\n⏳ چاوەڕێی بایەسی ڕۆژی نوێ:\nBias Buy\nیان\nBias Sell")

    state["trade"] = None
    if state.get("pending_rearm") and state.get("bias"):
        state["armed"] = True
        state["pending_rearm"] = False
        send("✅ بایەسی نوێ چالاک بوو. چاوەڕێی مەرجی RSI 5M.")
    else:
        state["bias"] = None
        state["armed"] = False
        state["pending_rearm"] = False
    save_state()


def monitor_trade(price, rsi):
    trade = state.get("trade")
    if not trade:
        return

    direction = trade["direction"]
    update_extremes(price)
    reached = int(trade.get("next_target", 0))

    while reached < 4:
        target = trade["targets"][reached]
        hit = price >= target["price"] if direction == "BUY" else price <= target["price"]
        if not hit:
            break

        reached += 1
        trade["next_target"] = reached
        send(
            "🎉 تارگێت گەیشت\n━━━━━━━━━━━━━━━━━━\n\n"
            f"ئاراستە: {direction}\n🎯 تارگێتی {reached}: {target['price']:.2f}\n"
            f"قازانجی کۆکراوە: +{target['pips']} pip\n"
            + ("🏆 هەموو تارگێتەکان تەواو بوون!"
               if reached == 4 else "چاوەڕێی تارگێتی دواتر.")
        )
        save_state()

    if reached >= 4:
        finish_trade("TP4", price, rsi)
        return

    if reached == 0:
        if direction == "BUY" and rsi <= BUY_STOP_RSI:
            finish_trade("STOP", price, rsi)
            return
        if direction == "SELL" and rsi >= SELL_STOP_RSI:
            finish_trade("STOP", price, rsi)
            return
    else:
        if direction == "BUY" and rsi <= BUY_CLOSE_RSI:
            finish_trade("RSI_CLOSE", price, rsi)
            return
        if direction == "SELL" and rsi >= SELL_CLOSE_RSI:
            finish_trade("RSI_CLOSE", price, rsi)
            return


def monitor_market():
    if now_baghdad().weekday() >= 5 or not state.get("bias"):
        return
    if not state.get("armed") and not state.get("trade"):
        return

    candles = get_5m_candles()
    price, quote_time = get_live_quote()
    rsi = calculate_live_rsi(candles, price)
    candle_time = current_5m_candle_start(quote_time)

    logging.info(
        "LIVE DATA quote=%s candle=%s price=%.2f RSI=%.2f",
        quote_time.isoformat(), candle_time.isoformat(), price, rsi
    )

    if state.get("trade"):
        monitor_trade(price, rsi)
        return

    # At most one signal during each 5-minute candle.
    if state.get("last_signal_candle") == candle_time.isoformat():
        return

    if state["bias"] == "BUY" and rsi <= BUY_RSI:
        create_signal("BUY", price, rsi, quote_time)
    elif state["bias"] == "SELL" and rsi >= SELL_RSI:
        create_signal("SELL", price, rsi, quote_time)


# ---------------- BIAS / CANCEL / STATUS ----------------
def set_bias(direction):
    old = state.get("bias")
    state["bias"] = direction

    if state.get("trade"):
        state["pending_rearm"] = True
        save_state()
        send(
            f"✅ بایەسی نوێ دانرا: {direction}\n"
            "مامەڵەی پێشوو هێشتا چالاکە؛ دوای کۆتایی بایەسی نوێ چالاک دەبێت."
        )
        return

    state["armed"] = True
    state["pending_rearm"] = False
    state["last_signal_candle"] = None
    save_state()

    if direction == "BUY":
        rule = (
            "BUY: چاوەڕێی RSI 30 یان خوارتر بە شێوەی لایڤ.\n"
            "ستۆپ پێش TP1: RSI 20 یان خوارتر.\n"
            "دوای TP1 تا TP3: گەڕانەوەی RSI بۆ 30 ئاگاداری داخستن دەدات."
        )
    else:
        rule = (
            "SELL: چاوەڕێی RSI 70 یان سەرەوە بە شێوەی لایڤ.\n"
            "ستۆپ پێش TP1: RSI 80 یان سەرەوە.\n"
            "دوای TP1 تا TP3: گەڕانەوەی RSI بۆ 70 ئاگاداری داخستن دەدات."
        )

    send(
        "🦅 بایەسی ڕۆژانە نوێ کرایەوە\n━━━━━━━━━━━━━━━━━━\n\n"
        f"بایەس: {direction}\nچوارچێوە: 5M | RSI: 14 (لایڤ)\n\n"
        f"{rule}\nتارگێتەکان: 40 / 60 / 80 / 100 pip\n"
        "🚫 هیچ ئۆردەرێکی ئۆتۆماتیکی ناکات."
    )
    logging.info("Bias changed %s -> %s", old, direction)


def cancel_all():
    had_trade = bool(state.get("trade"))
    had_bias = bool(state.get("bias"))
    state.update({
        "bias": None, "armed": False, "pending_rearm": False,
        "trade": None, "last_signal_candle": None
    })
    save_state()

    if had_trade:
        send("🛑 مامەڵە و بایەس هەڵوەشێندرایەوە؛ چاودێری وەستا.")
    elif had_bias:
        send("🛑 بایەسی ڕۆژانە هەڵوەشێندرایەوە.")
    else:
        send("ℹ️ هیچ بایەس یان مامەڵەیەکی چالاک نییە.")


def send_status():
    trade = state.get("trade")
    trade_text = (
        f"مامەڵە: {trade['direction']} لە {number(trade['entry']):.2f}\n"
        f"تارگێتەکان: {trade['next_target']}/4\n"
        if trade else "مامەڵەی چالاک: نییە\n"
    )
    send(
        "🦅 دۆخی بۆتی زێڕ\n━━━━━━━━━━━━━━━━━━\n\n"
        f"بایەس: {state.get('bias') or 'دانەنراوە'}\n{trade_text}"
        "چوارچێوە: 5M لایڤ\nRSI: 14\n"
        "BUY 30 / SELL 70\nستۆپ BUY 20 / SELL 80\n"
        "تارگێتەکان: 40 / 60 / 80 / 100 pip\n"
        "ئۆردەری ئۆتۆماتیکی: ناچالاک\n/weekly\n/cancel"
    )


# ---------------- WEEKLY REPORT ----------------
def send_weekly(key=None, scheduled=False):
    key = key or week_key()
    stats = stats_for(key)

    closed = int(stats["closed_trades"])
    wins = int(stats["winning_trades"])
    losses = int(stats["losing_trades"])
    win_rate = wins / closed * 100 if closed else 0.0
    pips = number(stats["total_pips"])
    emoji = "🟢" if pips > 0 else ("🔴" if pips < 0 else "⚪")

    trade = state.get("trade")
    active = (
        f"{trade['direction']} {trade['entry']:.2f} ({trade['next_target']}/4)"
        if trade and trade.get("week_key") == key else "نییە"
    )

    message = (
        "📊 ڕاپۆرتی هەفتانەی زێڕ XAUUSD\n━━━━━━━━━━━━━━━━━━\n\n"
        f"هەفتە: {key}\nماوە: دووشەممە تا هەینی\n\n"
        f"کۆی سیگناڵ: {stats['signals']}\n"
        f"BUY: {stats['buy_signals']}\nSELL: {stats['sell_signals']}\n\n"
        f"TP1: {stats['tp1']}\nTP2: {stats['tp2']}\n"
        f"TP3: {stats['tp3']}\nTP4: {stats['tp4']}\n\n"
        f"ستۆپی RSI: {stats['stop_losses']}\n"
        f"داخستن بە RSI: {stats['closed_by_rsi']}\n"
        f"مامەڵە داخراوەکان: {closed}\nقازانجدار: {wins}\nزیان: {losses}\n"
        f"ڕێژەی سەرکەوتن: {win_rate:.1f}%\n\n"
        f"{emoji} کۆی ئەنجام: {pips:+.1f} pip\n"
        f"مامەڵەی چالاک: {active}\n\n"
        "⚠️ سپرێد و کرێی بروکەر لە حیسابدا نییە. تەنها سیگناڵە."
    )

    if scheduled:
        message = "🕙 ڕاپۆرتی کۆتایی هەفتە — 10ی شەو\n\n" + message

    send(message)
    if scheduled:
        state["last_weekly_report"] = key
        save_state()


def check_weekly():
    current = now_baghdad()
    # Friday = weekday 4; Baghdad local time.
    if current.weekday() == 4 and current.hour >= 22:
        key = week_key(current)
        if state.get("last_weekly_report") != key:
            send_weekly(key, True)


# ---------------- TELEGRAM COMMANDS ----------------
def get_updates():
    global offset
    result = tg(
        "getUpdates",
        {"offset": offset, "timeout": 1, "allowed_updates": '["message"]'}
    )
    return result if isinstance(result, list) else []


def handle_message(message):
    text = message.get("text", "").strip()
    chat_id = str(message.get("chat", {}).get("id", ""))

    if not text or (CHAT_ID and chat_id != CHAT_ID):
        return

    command = " ".join(text.upper().split())

    if command == "BIAS BUY":
        set_bias("BUY")
    elif command == "BIAS SELL":
        set_bias("SELL")
    elif command == "/CANCEL":
        cancel_all()
    elif command == "/STATUS":
        send_status()
    elif command == "/WEEKLY":
        send_weekly()
    elif command in ("/START", "/HELP"):
        send(
            "🦅 بۆتی سیگناڵی زێڕ XAUUSD\n━━━━━━━━━━━━━━━━━━\n\n"
            "Bias Buy\nBias Sell\n\n"
            "RSI 5M / 14 (لایڤ)\n"
            "BUY: 30 یان خوارتر | SELL: 70 یان سەرەوە\n"
            "ستۆپ پێش TP1: BUY 20 | SELL 80\n"
            "دوای TP1 تا TP3: گەڕانەوەی RSI ئاگاداری داخستن دەدات\n"
            "تارگێتەکان: 40 / 60 / 80 / 100 pip\n\n"
            "/status - دۆخ\n/weekly - ڕاپۆرت\n"
            "/cancel - هەڵوەشاندنەوە\n"
            "ڕاپۆرت: هەینی 10ی شەو\n"
            "🚫 تەنها سیگناڵە؛ هیچ ئۆردەرێک ناکات."
        )
    else:
        send(
            "⚠️ فەرمانەکە نەناسراوە:\n"
            "Bias Buy\nBias Sell\n/status\n/weekly\n/cancel"
        )


# ---------------- MAIN LOOP ----------------
def main():
    global offset

    if not TOKEN or not CHAT_ID:
        logging.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
        return

    logging.info("XAUUSD LIVE RSI bot started")
    tg("deleteWebhook", {"drop_pending_updates": "false"})

    send(
        "🦅 بۆتی سیگناڵی زێڕ چالاک بوو\n"
        "RSI 5M / 14 — لایڤ، نوێکردنەوە هەر 5 چرکە\n"
        "BUY: RSI 30 یان خوارتر\nSELL: RSI 70 یان سەرەوە\n"
        "ستۆپ: BUY 20 / SELL 80\n"
        "تارگێتەکان: 40 / 60 / 80 / 100 pip\n"
        "ڕاپۆرت: هەینی 10ی شەو\n\n"
        "بایەس بنێرە: Bias Buy یان Bias Sell\n"
        "/status /weekly /cancel\n"
        "🚫 هیچ ئۆردەرێکی ئۆتۆماتیکی ناکات."
    )

    while True:
        try:
            for update in get_updates():
                offset = update["update_id"] + 1
                if update.get("message"):
                    handle_message(update["message"])

            check_weekly()

            try:
                monitor_market()
            except Exception as error:
                # Invalid, stale or missing market data => do not issue a signal.
                logging.warning("Market check skipped: %s", error)

        except Exception:
            logging.exception("Main loop error")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
