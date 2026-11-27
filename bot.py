"""Sanal (paper trading) kripto botu. Gercek para kullanmaz.
Her calistiginda: acik pozisyonu kontrol eder, yoksa coinleri tarar,
sanal alim/satim yapar ve Telegram'a mesaj atar. Durum state.json'da tutulur."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

COINS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD",
         "DOGE-USD", "LINK-USD", "AVAX-USD", "LTC-USD", "DOT-USD"]
START_CASH = 20.0
FEE = 0.005          # islem basina komisyon (%0.5). Borsana gore degistir.
SLIPPAGE = 0.001     # kayma payi (%0.1)
RSI_BUY = 35         # RSI bunun altindaysa alim adayi
MIN_STOP_PCT = 0.015 # stop mesafesi en az %1.5
RR = 2.0             # hedef = stop mesafesinin 2 kati
MAX_HOLD_HOURS = 24
SUMMARY_HOUR_UTC = 6
STATE_FILE = "state.json"

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def send(text):
    print(text)
    if not TOKEN or not CHAT_ID:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", data, timeout=20)
    except Exception as e:
        print("Telegram hatasi:", type(e).__name__)


def candles(symbol):
    url = f"https://api.exchange.coinbase.com/products/{symbol}/candles?granularity=3600"
    req = urllib.request.Request(url, headers={"User-Agent": "paper-bot"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    # [time, low, high, open, close, volume], yeniden eskiye gelir
    return sorted(data, key=lambda x: x[0])


def rsi(closes, n=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(cs, n=14):
    trs = []
    for i in range(1, len(cs)):
        h, l, pc = cs[i][2], cs[i][1], cs[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def save(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


def equity(s, prices):
    p = s["position"]
    if not p:
        return s["cash"]
    return s["cash"] + p["qty"] * prices.get(p["symbol"], p["entry"])


def close_position(s, price, reason, now):
    p = s["position"]
    proceeds = p["qty"] * price * (1 - FEE)
    pnl = proceeds - p["cost"]
    s["cash"] += proceeds
    s["trades"].append({"symbol": p["symbol"], "pnl": round(pnl, 4),
                        "reason": reason, "time": now})
    s["position"] = None
    send(f"SANAL SATIM {p['symbol']}\n{reason}\n"
         f"Cikis: {price:.4f} (giris {p['entry']:.4f})\n"
         f"Net K/Z: {pnl:+.2f}$ ({pnl / p['cost'] * 100:+.1f}%)\n"
         f"Kasa: {s['cash']:.2f}$")


def check_position(s, now):
    p = s["position"]
    cs = candles(p["symbol"])
    price = cs[-1][4]
    for c in cs:
        if c[0] <= p["time"]:
            continue
        if c[1] <= p["stop"]:
            close_position(s, p["stop"] * (1 - SLIPPAGE), "Stop tetiklendi", now)
            return price
        if c[2] >= p["target"]:
            close_position(s, p["target"], "Hedefe ulasti", now)
            return price
    if now - p["time"] >= MAX_HOLD_HOURS * 3600:
        close_position(s, price * (1 - SLIPPAGE), "Sure doldu", now)
    return price


def scan():
    best = None
    for sym in COINS:
        try:
            cs = candles(sym)
        except Exception as e:
            print(sym, "veri hatasi:", type(e).__name__)
            continue
        if len(cs) < 60:
            continue
        closed = cs[:-1]
        closes = [c[4] for c in closed]
        r = rsi(closes)
        bounce = closes[-1] > closes[-2]
        if r >= RSI_BUY or not bounce:
            continue
        price = cs[-1][4]
        a = atr(closed)
        stop_dist = max(1.5 * a, MIN_STOP_PCT * price)
        vols = [c[5] for c in closed[-20:]]
        vol_ratio = closed[-1][5] / (sum(vols) / len(vols)) if sum(vols) else 1
        score = (RSI_BUY - r) + vol_ratio
        if best is None or score > best["score"]:
            best = {"symbol": sym, "price": price, "rsi": r, "score": score,
                    "stop": price - stop_dist,
                    "target": price + RR * stop_dist, "vol": vol_ratio}
    return best


def main():
    now = int(datetime.now(timezone.utc).timestamp())
    s = load()
    if s is None:
        s = {"cash": START_CASH, "position": None, "trades": [],
             "last_summary": ""}
        send(f"Bot basladi. Sanal kasa: {START_CASH:.0f}$. "
             f"Her saat basi sinyal taranacak.")

    prices = {}
    if s["position"]:
        try:
            prices[s["position"]["symbol"]] = check_position(s, now)
        except Exception as e:
            print("Pozisyon kontrol hatasi:", type(e).__name__)

    if not s["position"]:
        sig = scan()
        if sig:
            entry = sig["price"] * (1 + SLIPPAGE)
            cost = s["cash"]
            qty = cost * (1 - FEE) / entry
            s["cash"] = 0.0
            s["position"] = {"symbol": sig["symbol"], "qty": qty,
                             "entry": entry, "stop": sig["stop"],
                             "target": sig["target"], "cost": cost,
                             "time": now}
            prices[sig["symbol"]] = sig["price"]
            send(f"SANAL ALIM {sig['symbol']}\n"
                 f"Giris: {entry:.4f}\nStop: {sig['stop']:.4f}\n"
                 f"Hedef: {sig['target']:.4f}\n"
                 f"Gerekce: RSI {sig['rsi']:.0f} (asiri satim), "
                 f"son mum toparlanma gosteriyor, hacim {sig['vol']:.1f}x\n"
                 f"Kullanilan: {cost:.2f}$")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if (datetime.now(timezone.utc).hour >= SUMMARY_HOUR_UTC
            and s.get("last_summary") != today):
        t = s["trades"]
        wins = sum(1 for x in t if x["pnl"] > 0)
        rate = f"{wins}/{len(t)}" if t else "-"
        pos = s["position"]["symbol"] if s["position"] else "yok"
        send(f"GUNLUK OZET\nToplam deger: {equity(s, prices):.2f}$ "
             f"(baslangic {START_CASH:.0f}$)\nIslem sayisi: {len(t)}, "
             f"kazanan: {rate}\nAcik pozisyon: {pos}")
        s["last_summary"] = today

    save(s)


if __name__ == "__main__":
    main()
