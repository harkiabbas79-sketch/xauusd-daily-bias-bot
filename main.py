import os, time, json, math, logging, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# XAUUSD RSI 5M - SIGNAL ONLY (no order execution)
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
            s = json.load(f)
        d = default_state()
        if isinstance(s, dict):
            d.update(s)
        if not isinstance(d.get("weekly_stats"), dict):
            d["weekly_stats"] = {}
        return d
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
    i = (dt or now_baghdad()).isocalendar()
    return f"{i.year}-W{i.week:02d}"

def empty_stats():
    return {"signals": 0, "buy_signals": 0, "sell_signals": 0,
            "stop_losses": 0, "closed_by_rsi": 0,
            "tp1": 0, "tp2": 0, "tp3": 0, "tp4": 0,
            "closed_trades": 0, "winning_trades": 0,
            "losing_trades": 0, "total_pips": 0.0}

def stats_for(key=None):
    key = key or week_key()
    s = state["weekly_stats"].get(key)
    if not isinstance(s, dict):
        s = empty_stats()
        state["weekly_stats"][key] = s
    for k, v in empty_stats().items():
        s.setdefault(k, v)
    return s

# ---------------- Telegram ----------------
def tg(method, data=None):
    if not TOKEN:
        return None
    try:
        r = http.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                      data=data or {}, timeout=15)
        if r.status_code != 200:
            logging.error("Telegram HTTP %s: %s", r.status_code, r.text[:200])
            return None
        j = r.json()
        if not j.get("ok"):
            logging.error("Telegram: %s", j.get("description"))
            return None
        return j.get("result")
    except Exception:
        logging.exception("Telegram request failed")
        return None

def send(text):
    return tg("sendMessage", {"chat_id": CHAT_ID, "text": text}) is not None

# ---------------- BiQuote validated market data ----------------
def get_5m_candles():
    # API documented envelope: {symbol, interval, bars}; newest-first.
    r = http.get(OHLC_URL, params={"interval": "5m", "limit": 300}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"OHLC HTTP {r.status_code}: {r.text[:180]}")
    data = r.json()
    if not isinstance(data, dict) or str(data.get("symbol", "")).upper() != "XAUUSD":
        raise RuntimeError("Unexpected OHLC response/symbol")
    if str(data.get("interval", "")).lower() != "5m":
        raise RuntimeError(f"API interval is not 5m: {data.get('interval')}")
    bars = data.get("bars")
    if not isinstance(bars, list):
        raise RuntimeError("OHLC bars list missing")

    valid = []
    for b in bars:
        if not isinstance(b, dict) or not isinstance(b.get("isOpen"), bool):
            continue
        try:
            t = parse_utc(b.get("openTime"))
            o, h, l, c = [float(b[k]) for k in ("open", "high", "low", "close")]
            if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
                continue
            if h < max(o, c, l) or l > min(o, c, h):
                continue
            valid.append({"time": t, "open": o, "high": h, "low": l,
                          "close": c, "isOpen": b["isOpen"]})
        except (KeyError, TypeError, ValueError, RuntimeError):
            continue

    valid.sort(key=lambda x: x["time"])  # old -> new, never reverse blindly
    dedup = {b["time"]: b for b in valid}
    valid = sorted(dedup.values(), key=lambda x: x["time"])
    closed = [b for b in valid if b["isOpen"] is False]
    if len(closed) < RSI_PERIOD + 20:
        raise RuntimeError("Not enough closed 5m candles")

    last = closed[-1]
    age = (now_utc() - (last["time"] + timedelta(minutes=5))).total_seconds()
    if age < -60:
        raise RuntimeError("Latest closed candle timestamp is in future")
    if age > MAX_CANDLE_AGE:
        raise RuntimeError(f"Closed candle stale ({age:.0f}s)")

    # Reject a suspicious recent gap; a gap may occur at a market closure.
    for a, b in zip(closed[-6:], closed[-5:]):
        gap = (b["time"] - a["time"]).total_seconds()
        if gap <= 0 or gap > 900:
            raise RuntimeError(f"Invalid recent candle spacing ({gap:.0f}s)")
    return closed[-250:]

def get_live_quote():
    # mid is the documented FX/CFD price; allowStale=false rejects old quotes.
    r = http.get(TICK_URL, params={"allowStale": "false"}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Live quote HTTP {r.status_code}: {r.text[:180]}")
    d = r.json()
    if not isinstance(d, dict):
        raise RuntimeError("Unexpected live quote response")
    price = number(d.get("mid"))
    if price <= 0:
        raise RuntimeError("Live mid price missing")
    if d.get("stale") is True:
        raise RuntimeError("Live quote marked stale")
    market = str(d.get("marketState", "")).lower()
    if market and market != "open":
        raise RuntimeError(f"Market state is {market}")
    qt = parse_utc(d.get("timestamp"))
    age = (now_utc() - qt).total_seconds()
    if age < -10 or age > MAX_TICK_AGE:
        raise RuntimeError(f"Live quote timestamp not fresh ({age:.0f}s)")
    api_age = d.get("quoteAgeSeconds")
    if api_age is not None and number(api_age) > MAX_TICK_AGE:
        raise RuntimeError(f"API quote age too high ({api_age})")
    return price, qt

# ---------------- RSI Wilder on CLOSED bars ----------------
def calculate_rsi(candles):
    closes = [float(c["close"]) for c in candles]
    if len(closes) < RSI_PERIOD + 1:
        raise RuntimeError("Not enough RSI data")
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    ag = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    al = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
    for i in range(RSI_PERIOD, len(gains)):
        ag = (ag * (RSI_PERIOD - 1) + gains[i]) / RSI_PERIOD
        al = (al * (RSI_PERIOD - 1) + losses[i]) / RSI_PERIOD
    if ag == 0 and al == 0: return 50.0
    if al == 0: return 100.0
    if ag == 0: return 0.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)

def targets_for(direction, entry):
    out = []
    for p in TARGET_PIPS:
        delta = p * PIP_SIZE
        px = entry + delta if direction == "BUY" else entry - delta
        out.append({"pips": p, "price": round(px, 2)})
    return out

def signed_pips(direction, entry, exit_price):
    return ((exit_price-entry) if direction == "BUY" else (entry-exit_price)) / PIP_SIZE

# ---------------- Signal creation ----------------
def create_signal(direction, price, rsi, candle_time, quote_time):
    entry = round(price, 2)
    ts = now_baghdad()
    key = week_key(ts)
    trade = {"direction": direction, "entry": entry, "rsi": round(rsi, 2),
             "targets": targets_for(direction, entry), "next_target": 0,
             "highest_price": entry, "lowest_price": entry,
             "started": ts.strftime("%Y-%m-%d %H:%M:%S"), "week_key": key,
             "signal_candle": candle_time.isoformat(),
             "quote_time": quote_time.isoformat()}
    state["trade"] = trade
    state["armed"] = False
    state["pending_rearm"] = False
    state["last_signal_candle"] = candle_time.isoformat()
    s = stats_for(key)
    s["signals"] += 1
    s["buy_signals" if direction == "BUY" else "sell_signals"] += 1
    save_state()

    emoji = "🟢" if direction == "BUY" else "🔴"
    lines = "".join(f"🎯 تارگێتی {i}: {x['price']:.2f} ({x['pips']} pip)\n"
                    for i, x in enumerate(trade["targets"], 1))
    stop = f"BUY: RSI {BUY_STOP_RSI} یان خوارتر" if direction == "BUY" else f"SELL: RSI {SELL_STOP_RSI} یان سەرەوە"
    send("🦅 سیگناڵی RSI بۆ زێڕ XAUUSD\n━━━━━━━━━━━━━━━━━━\n\n"
         f"{emoji} ئاراستە: {direction}\n📍 نرخی چوونەژوورەوە: {entry:.2f}\n"
         f"📊 RSI 5M: {rsi:.2f}\n"
         f"🕒 کاتی کندڵ: {candle_time.astimezone(BAGHDAD):%Y-%m-%d %H:%M}\n"
         f"🕒 کاتی نرخ: {quote_time.astimezone(BAGHDAD):%H:%M:%S}\n\n"
         f"{lines}\n🛑 ستۆپی RSI پێش TP1:\n{stop}\n\n"
         "⚠️ تەنها سیگناڵە؛ هیچ ئۆردەرێک ناکات.")
    logging.info("SIGNAL %s %.2f RSI %.2f candle %s", direction, entry, rsi, candle_time)

# ---------------- Trade and report ----------------
def update_extremes(price):
    t = state.get("trade")
    if not t: return
    t["highest_price"] = round(max(number(t.get("highest_price", t["entry"])), price), 2)
    t["lowest_price"] = round(min(number(t.get("lowest_price", t["entry"])), price), 2)
    save_state()

def record_result(t, pips, reason):
    s = stats_for(t.get("week_key") or week_key())
    n = int(t.get("next_target", 0))
    for i in range(1, 5):
        s[f"tp{i}"] += int(n >= i)
    if reason == "STOP": s["stop_losses"] += 1
    if reason == "RSI_CLOSE": s["closed_by_rsi"] += 1
    s["closed_trades"] += 1
    s["total_pips"] = round(number(s["total_pips"]) + pips, 1)
    if pips > 0: s["winning_trades"] += 1
    elif pips < 0: s["losing_trades"] += 1
    save_state()

def finish_trade(reason, price, rsi):
    t = state.get("trade")
    if not t: return
    direction, entry = t["direction"], number(t["entry"])
    n = int(t.get("next_target", 0))
    if reason == "STOP":
        pips = signed_pips(direction, entry, price)
        msg = ("🛑 ستۆپی RSI چالاک بوو\n━━━━━━━━━━━━━━━━━━\n\n"
               f"ئاراستە: {direction}\nچوونەژوورەوە: {entry:.2f}\n"
               f"نرخی ئێستا: {price:.2f}\nRSI: {rsi:.2f}\n"
               f"ئەنجامی نرخی مامەڵە: {pips:+.1f} pip\n")
    elif reason == "RSI_CLOSE":
        pips = float(TARGET_PIPS[n-1])
        msg = ("⚠️ ئاگادارکردنەوەی داخستنی مامەڵە\n━━━━━━━━━━━━━━━━━━\n\n"
               f"ئاراستە: {direction}\nچوونەژوورەوە: {entry:.2f}\n"
               f"دوا تارگێتی گەیشتوو: {n}\nRSI: {rsi:.2f}\n"
               f"قازانجی هەژمارکراو: +{pips:.0f} pip\n"
               "ئەگەر پۆزیشنێکت هەیە، بە دەستی دایبخە.\n")
    else:
        pips = 100.0
        msg = ("🏆 هەموو تارگێتەکان تەواو بوون\n━━━━━━━━━━━━━━━━━━\n\n"
               f"ئاراستە: {direction}\nچوونەژوورەوە: {entry:.2f}\n"
               "ئەنجام: +100 pip\n")
    record_result(t, pips, reason)
    send(msg + "\n⏳ چاوەڕێی بایەسی ڕۆژی نوێ:\nBias Buy\nیان\nBias Sell")
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
    t = state.get("trade")
    if not t: return
    direction = t["direction"]
    update_extremes(price)
    n = int(t.get("next_target", 0))
    while n < 4:
        target = t["targets"][n]
        hit = price >= target["price"] if direction == "BUY" else price <= target["price"]
        if not hit: break
        n += 1
        t["next_target"] = n
        send("🎉 تارگێت گەیشت\n━━━━━━━━━━━━━━━━━━\n\n"
             f"ئاراستە: {direction}\n🎯 تارگێتی {n}: {target['price']:.2f}\n"
             f"قازانجی کۆکراوە: +{target['pips']} pip\n"
             + ("🏆 هەموو تارگێتەکان تەواو بوون!" if n == 4 else "چاوەڕێی تارگێتی دواتر."))
        save_state()
    if n >= 4:
        finish_trade("TP4", price, rsi); return
    if n == 0:
        if direction == "BUY" and rsi <= BUY_STOP_RSI: finish_trade("STOP", price, rsi); return
        if direction == "SELL" and rsi >= SELL_STOP_RSI: finish_trade("STOP", price, rsi); return
    else:
        if direction == "BUY" and rsi <= BUY_CLOSE_RSI: finish_trade("RSI_CLOSE", price, rsi); return
        if direction == "SELL" and rsi >= SELL_CLOSE_RSI: finish_trade("RSI_CLOSE", price, rsi); return

def monitor_market():
    if now_baghdad().weekday() >= 5 or not state.get("bias"): return
    if not state.get("armed") and not state.get("trade"): return
    candles = get_5m_candles()
    price, quote_time = get_live_quote()
    rsi = calculate_rsi(candles)
    candle_time = candles[-1]["time"]
    logging.info("DATA OK candle=%s quote=%s price=%.2f RSI=%.2f",
                 candle_time, quote_time, price, rsi)
    if state.get("trade"):
        monitor_trade(price, rsi); return
    if state.get("last_signal_candle") == candle_time.isoformat(): return
    if state["bias"] == "BUY" and rsi <= BUY_RSI:
        create_signal("BUY", price, rsi, candle_time, quote_time)
    elif state["bias"] == "SELL" and rsi >= SELL_RSI:
        create_signal("SELL", price, rsi, candle_time, quote_time)

# ---------------- Bias, cancel, status ----------------
def set_bias(direction):
    old = state.get("bias")
    state["bias"] = direction
    if state.get("trade"):
        state["pending_rearm"] = True
        save_state()
        send(f"✅ بایەسی نوێ دانرا: {direction}\nمامەڵەی پێشوو هێشتا چالاکە؛ دوای کۆتایی بایەسی نوێ چالاک دەبێت.")
        return
    state["armed"] = True
    state["pending_rearm"] = False
    state["last_signal_candle"] = None
    save_state()
    if direction == "BUY":
        rule = "BUY: چاوەڕێی RSI 30 یان خوارتر.\nستۆپ پێش TP1: RSI 20 یان خوارتر.\nدوای TP1 تا TP3: گەڕانەوەی RSI بۆ 30 ئاگاداری داخستن دەدات."
    else:
        rule = "SELL: چاوەڕێی RSI 70 یان سەرەوە.\nستۆپ پێش TP1: RSI 80 یان سەرەوە.\nدوای TP1 تا TP3: گەڕانەوەی RSI بۆ 70 ئاگاداری داخستن دەدات."
    send("🦅 بایەسی ڕۆژانە نوێ کرایەوە\n━━━━━━━━━━━━━━━━━━\n\n"
         f"بایەس: {direction}\nچوارچێوە: 5M | RSI: 14\n\n{rule}\n"
         "تارگێتەکان: 40 / 60 / 80 / 100 pip\n🚫 هیچ ئۆردەرێکی ئۆتۆماتیکی ناکات.")
    logging.info("Bias changed %s -> %s", old, direction)

def cancel_all():
    had = bool(state.get("trade"))
    had_bias = bool(state.get("bias"))
    state.update({"bias": None, "armed": False, "pending_rearm": False,
                  "trade": None, "last_signal_candle": None})
    save_state()
    if had: send("🛑 مامەڵە و بایەس هەڵوەشێندرایەوە؛ چاودێری وەستا.")
    elif had_bias: send("🛑 بایەسی ڕۆژانە هەڵوەشێندرایەوە.")
    else: send("ℹ️ هیچ بایەس یان مامەڵەیەکی چالاک نییە.")

def send_status():
    t = state.get("trade")
    trade_text = (f"مامەڵە: {t['direction']} لە {number(t['entry']):.2f}\n"
                  f"تارگێتەکان: {t['next_target']}/4\n") if t else "مامەڵەی چالاک: نییە\n"
    send("🦅 دۆخی بۆتی زێڕ\n━━━━━━━━━━━━━━━━━━\n\n"
         f"بایەس: {state.get('bias') or 'دانەنراوە'}\n{trade_text}"
         "چوارچێوە: 5M کندڵی داخراو\nRSI: 14\n"
         "BUY 30 / SELL 70\nستۆپ BUY 20 / SELL 80\n"
         "تارگێتەکان: 40 / 60 / 80 / 100 pip\n"
         "ئۆردەری ئۆتۆماتیکی: ناچالاک\n/weekly\n/cancel")

# ---------------- Weekly report ----------------
def send_weekly(key=None, scheduled=False):
    key = key or week_key()
    s = stats_for(key)
    closed, wins, losses = int(s["closed_trades"]), int(s["winning_trades"]), int(s["losing_trades"])
    rate = wins / closed * 100 if closed else 0.0
    pips = number(s["total_pips"])
    emoji = "🟢" if pips > 0 else ("🔴" if pips < 0 else "⚪")
    t = state.get("trade")
    active = f"{t['direction']} {t['entry']:.2f} ({t['next_target']}/4)" if t and t.get("week_key") == key else "نییە"
    msg = ("📊 ڕاپۆرتی هەفتانەی زێڕ XAUUSD\n━━━━━━━━━━━━━━━━━━\n\n"
           f"هەفتە: {key}\nماوە: دووشەممە تا هەینی\n\n"
           f"کۆی سیگناڵ: {s['signals']}\nBUY: {s['buy_signals']}\nSELL: {s['sell_signals']}\n\n"
           f"TP1: {s['tp1']}\nTP2: {s['tp2']}\nTP3: {s['tp3']}\nTP4: {s['tp4']}\n\n"
           f"ستۆپی RSI: {s['stop_losses']}\nداخستن بە RSI: {s['closed_by_rsi']}\n"
           f"مامەڵە داخراوەکان: {closed}\nقازانجدار: {wins}\nزیان: {losses}\n"
           f"ڕێژەی سەرکەوتن: {rate:.1f}%\n\n{emoji} کۆی ئەنجام: {pips:+.1f} pip\n"
           f"مامەڵەی چالاک: {active}\n\n"
           "⚠️ سپرێد و کرێی بروکەر لە حیسابدا نییە. تەنها سیگناڵە.")
    if scheduled: msg = "🕙 ڕاپۆرتی کۆتایی هەفتە — 10ی شەو\n\n" + msg
    send(msg)
    if scheduled:
        state["last_weekly_report"] = key
        save_state()

def check_weekly():
    n = now_baghdad()
    if n.weekday() == 4 and n.hour >= 22:
        k = week_key(n)
        if state.get("last_weekly_report") != k:
            send_weekly(k, True)

# ---------------- Telegram commands ----------------
def get_updates():
    global offset
    result = tg("getUpdates", {"offset": offset, "timeout": 1,
                               "allowed_updates": '["message"]'})
    return result if isinstance(result, list) else []

def handle_message(m):
    txt = m.get("text", "").strip()
    cid = str(m.get("chat", {}).get("id", ""))
    if not txt or (CHAT_ID and cid != CHAT_ID): return
    cmd = " ".join(txt.upper().split())
    if cmd == "BIAS BUY": set_bias("BUY")
    elif cmd == "BIAS SELL": set_bias("SELL")
    elif cmd == "/CANCEL": cancel_all()
    elif cmd == "/STATUS": send_status()
    elif cmd == "/WEEKLY": send_weekly()
    elif cmd in ("/START", "/HELP"):
        send("🦅 بۆتی سیگناڵی زێڕ XAUUSD\n━━━━━━━━━━━━━━━━━━\n\n"
             "Bias Buy\nBias Sell\n\nRSI 5M / 14 (کندڵی داخراو)\n"
             "BUY: 30 یان خوارتر | SELL: 70 یان سەرەوە\n"
             "ستۆپ پێش TP1: BUY 20 | SELL 80\n"
             "دوای TP1 تا TP3: گەڕانەوەی RSI ئاگاداری داخستن دەدات\n"
             "تارگێتەکان: 40 / 60 / 80 / 100 pip\n\n"
             "/status - دۆخ\n/weekly - ڕاپۆرت\n/cancel - هەڵوەشاندنەوە\n"
             "ڕاپۆرت: هەینی 10ی شەو\n🚫 تەنها سیگناڵە؛ هیچ ئۆردەرێک ناکات.")
    else:
        send("⚠️ فەرمانەکە نەناسراوە:\nBias Buy\nBias Sell\n/status\n/weekly\n/cancel")

# ---------------- Main loop ----------------
def main():
    global offset
    if not TOKEN or not CHAT_ID:
        logging.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
        return
    logging.info("XAUUSD RSI bot started")
    tg("deleteWebhook", {"drop_pending_updates": "false"})
    send("🦅 بۆتی سیگناڵی زێڕ چالاک بوو\n"
         "RSI 5M / 14 — کندڵی داخراو\nBUY: RSI 30 یان خوارتر\n"
         "SELL: RSI 70 یان سەرەوە\nستۆپ: BUY 20 / SELL 80\n"
         "تارگێتەکان: 40 / 60 / 80 / 100 pip\n"
         "ڕاپۆرت: هەینی 10ی شەو\n\n"
         "بایەس بنێرە: Bias Buy یان Bias Sell\n"
         "/status /weekly /cancel\n🚫 هیچ ئۆردەرێکی ئۆتۆماتیکی ناکات.")
    while True:
        try:
            for u in get_updates():
                offset = u["update_id"] + 1
                if u.get("message"): handle_message(u["message"])
            check_weekly()
            try:
                monitor_market()
            except Exception as e:
                # Fail closed: invalid/stale/missing data means NO signal.
                logging.warning("Market check skipped: %s", e)
        except Exception:
            logging.exception("Main loop error")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
