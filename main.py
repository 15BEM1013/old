import ccxt
import time
import threading
import requests
from flask import Flask
from datetime import datetime
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import queue
import json
import os

# === PROXY ===
proxies = {
    "http": "http://sgkgjbve:x9swvp7b0epc@207.244.217.165:6712",
    "https": "http://sgkgjbve:x9swvp7b0epc@207.244.217.165:6712"
}
os.environ['HTTP_PROXY'] = proxies['http']
os.environ['HTTPS_PROXY'] = proxies['https']

# === CONFIG ===
BOT_TOKEN = '8123921112:AAGW9aXd--HzyQjgqMNKjyMagKG9_opOfU0'
CHAT_ID = '655537138'
TIMEFRAME = '15m'
MIN_BIG_BODY_PCT = 1.0
MAX_SMALL_BODY_PCT = 1.0
MIN_LOWER_WICK_PCT = 20.0
MAX_WORKERS = 10
BATCH_DELAY = 2.5
NUM_CHUNKS = 4
CAPITAL = 10.0
SL_PCT = 1.5 / 100
TP_SL_CHECK_INTERVAL = 30
TRADE_FILE = 'open_trades.json'
CLOSED_TRADE_FILE = 'closed_trades.json'

# === TIME ZONE ===
def get_ist_time():
    return datetime.now(pytz.timezone('Asia/Kolkata'))

# === Load & Save Trades ===
def save_trades():
    with open(TRADE_FILE, 'w') as f:
        json.dump(open_trades, f, default=str)

def load_trades():
    global open_trades
    if os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, 'r') as f:
            open_trades = json.load(f)
    else:
        open_trades = {}

def save_closed_trades(closed_trade):
    trades = []
    if os.path.exists(CLOSED_TRADE_FILE):
        with open(CLOSED_TRADE_FILE, 'r') as f:
            trades = json.load(f)
    trades.append(closed_trade)
    with open(CLOSED_TRADE_FILE, 'w') as f:
        json.dump(trades, f, default=str)

def load_closed_trades():
    if os.path.exists(CLOSED_TRADE_FILE):
        with open(CLOSED_TRADE_FILE, 'r') as f:
            return json.load(f)
    return []

# === Telegram ===
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        res = requests.post(url, data={'chat_id': CHAT_ID, 'text': msg}, timeout=10)
        return res.json().get("result", {}).get("message_id")
    except:
        return None

def edit_telegram_message(msg_id, new_text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        requests.post(url, data={'chat_id': CHAT_ID, 'message_id': msg_id, 'text': new_text})
    except:
        pass

# === Candles and EMA ===
def is_bullish(c): return c[4] > c[1]
def is_bearish(c): return c[4] < c[1]
def body_pct(c): return abs(c[4] - c[1]) / c[1] * 100
def lower_wick_pct(c): return (c[1] - c[3]) / (c[1] - c[4]) * 100 if is_bearish(c) else 0

def calculate_ema(candles, period):
    closes = [c[4] for c in candles]
    if len(closes) < period: return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for close in closes[period:]:
        ema = (close - ema) * k + ema
    return ema

# === Pattern Logic ===
def detect_rising_three(candles):
    c2, c1, c0 = candles[-4], candles[-3], candles[-2]
    avg_vol = sum(c[5] for c in candles[-6:-1]) / 5
    return (
        is_bullish(c2) and body_pct(c2) >= MIN_BIG_BODY_PCT and c2[5] > avg_vol and
        is_bearish(c1) and body_pct(c1) <= MAX_SMALL_BODY_PCT and lower_wick_pct(c1) >= MIN_LOWER_WICK_PCT and
        is_bearish(c0) and body_pct(c0) <= MAX_SMALL_BODY_PCT and lower_wick_pct(c0) >= MIN_LOWER_WICK_PCT and
        c1[5] > c0[5]
    )

def detect_falling_three(candles):
    c2, c1, c0 = candles[-4], candles[-3], candles[-2]
    avg_vol = sum(c[5] for c in candles[-6:-1]) / 5
    return (
        is_bearish(c2) and body_pct(c2) >= MIN_BIG_BODY_PCT and c2[5] > avg_vol and
        is_bullish(c1) and body_pct(c1) <= MAX_SMALL_BODY_PCT and
        is_bullish(c0) and body_pct(c0) <= MAX_SMALL_BODY_PCT and
        c1[5] > c0[5]
    )

# === Exchange Setup ===
exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
    'proxies': proxies
})

sent_signals = {}
open_trades = {}
closed_trades = []
app = Flask(__name__)

# === Trade Logic ===
def process_symbol(symbol, alert_queue):
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=30)
        if len(candles) < 25: return

        ema21 = calculate_ema(candles, 21)
        ema9 = calculate_ema(candles, 9)
        entry = candles[-2][4]
        tp = candles[-4][4]
        sl = entry * (1 - SL_PCT) if detect_rising_three(candles) else entry * (1 + SL_PCT)

        if detect_rising_three(candles):
            msg = f"{symbol} - RISING PATTERN\nentry - {entry}\ntp - {tp}\nsl - {sl:.4f}"
            alert_queue.put((symbol, msg, 'buy', entry, tp, sl))
        elif detect_falling_three(candles):
            msg = f"{symbol} - FALLING PATTERN\nentry - {entry}\ntp - {tp}\nsl - {sl:.4f}"
            alert_queue.put((symbol, msg, 'sell', entry, tp, sl))
    except Exception as e:
        print(f"{symbol} error: {e}")

def check_tp_sl_loop():
    while True:
        try:
            for sym, trade in list(open_trades.items()):
                last = exchange.fetch_ticker(sym)['last']
                hit = None
                if trade['side'] == 'buy':
                    if last >= trade['tp']: hit = "✅ TP hit"
                    elif last <= trade['sl']: hit = "❌ SL hit"
                else:
                    if last <= trade['tp']: hit = "✅ TP hit"
                    elif last >= trade['sl']: hit = "❌ SL hit"

                if hit:
                    pnl_pct = (trade['tp'] - trade['entry']) / trade['entry'] * 100 if "TP" in hit else \
                              (trade['sl'] - trade['entry']) / trade['entry'] * 100
                    profit = CAPITAL * pnl_pct / 100
                    edit_telegram_message(trade['msg_id'], f"{trade['msg']}\n{hit}\nP&L: ${profit:.2f}")
                    save_closed_trades({'symbol': sym, 'pnl': profit, 'pnl_pct': pnl_pct})
                    del open_trades[sym]
                    save_trades()
            time.sleep(TP_SL_CHECK_INTERVAL)
        except:
            time.sleep(5)

def run_scan():
    symbols = [s for s in exchange.load_markets() if 'USDT' in s and '/'.join(s.split('/')) in exchange.symbols]
    chunks = [symbols[i::NUM_CHUNKS] for i in range(NUM_CHUNKS)]
    alert_queue = queue.Queue()

    def alert_sender():
        while True:
            try:
                sym, msg, side, entry, tp, sl = alert_queue.get(timeout=3)
                msg_id = send_telegram(msg)
                open_trades[sym] = {'msg': msg, 'msg_id': msg_id, 'side': side, 'entry': entry, 'tp': tp, 'sl': sl}
                save_trades()
            except queue.Empty:
                continue

    threading.Thread(target=alert_sender, daemon=True).start()

    while True:
        for chunk in chunks:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                for symbol in chunk:
                    executor.submit(process_symbol, symbol, alert_queue)
            time.sleep(BATCH_DELAY)

# === Web & Runner ===
@app.route('/')
def home(): return "✅ Bot Running with Proxy and Strategy!"

def run_bot():
    load_trades()
    send_telegram("🚀 Bot started")
    threading.Thread(target=check_tp_sl_loop, daemon=True).start()
    threading.Thread(target=run_scan, daemon=True).start()
    app.run(host='0.0.0.0', port=8080)

if __name__ == "__main__":
    run_bot()
