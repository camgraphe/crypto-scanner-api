import os, json, time, math, requests
from datetime import datetime, timezone
from statistics import mean
from pathlib import Path
import sys
import threading
from collections import deque, defaultdict

# --- Config rapides ---
QUOTE = "USDC"               # on scanne les paires *USDC (fallback auto USDT pour OI/Funding)
MAX_SYMBOLS = 100            # jusqu'à 100 alts
LIQ_FLOOR = 10_000_000       # seuil de liquidité 24h en quote
KLINE_INTERVAL = "1h"        # cadence d'analyse
KLINE_LIMIT = 200            # bougies récupérées (pour EMA200)
ORDERBOOK_DEPTH = 50         # profondeur carnet pour l'imbalance
EQUITY_USDC = 2905           # capital dispo (info dans le snapshot)
TIMEOUT = 10                 # timeout HTTP (sec)
RETRIES = 3                  # retries par requête
SLEEP_BETWEEN = 0.08         # petite pause entre appels

# Sortie avancée
TOP_K = 20                   # nombre d'alts compressées envoyées au GPT
WRITE_MINIFIED = True        # écrit snapshot_min.json compressé

MTF_TOP_N = 30              # on calcule des triggers 15m/5m pour les 30 meilleurs
FETCH_15M = True

# Dérivés (OI/Funding) — optionnels (souvent lents via fapi)
DERIV_MODE = "off"   # "off" (rapide), ou "top" (on enrichit le top N après ranking)
DERIV_TOP_N = 15
DERIV_TIMEOUT = 5
DERIV_RETRIES = 1
FETCH_5M = True

# --- Streaming temps réel (WebSocket Binance) ---
STREAM_TOP_N = 12           # nombre d'actifs à suivre en temps réel (souvent = MTF_TOP_N)
STREAM_WRITE_EVERY = 15     # réécriture snapshot_min.json toutes les X secondes
STREAM_DURATION_SEC = 300   # durée par défaut du mode --stream (en secondes)
WS_BASE = "wss://stream.binance.com:9443/stream?streams="

BASE = "https://api.binance.com"
FBASE = "https://fapi.binance.com"
HDRS = {"User-Agent": "crypto-scanner/1.1 (+github.com/you)"}

# ---------- utils HTTP ----------
def get_json(url, params=None, timeout=None):
    for i in range(RETRIES):
        try:
            r = requests.get(url, params=params, headers=HDRS, timeout=(timeout or TIMEOUT))
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == RETRIES - 1:
                raise
            time.sleep(0.5 * (i + 1))
    return None

# ---------- indicateurs de base ----------
def ema(series, period):
    if not series:
        return None
    k = 2 / (period + 1)
    e = series[0]
    for v in series[1:]:
        e = v * k + e * (1 - k)
    return e

def rsi(series, period=14):
    if len(series) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(series)):
        d = series[i] - series[i - 1]
        gains.append(max(d, 0))
        losses.append(abs(min(d, 0)))
    avg_gain = mean(gains[-period:])
    avg_loss = mean(losses[-period:]) or 1e-9
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return mean(trs[-period:])

# ---------- utilitaires patterns & niveaux ----------
def highest_high(highs, n):
    return max(highs[-n:]) if len(highs) >= n else max(highs)

def lowest_low(lows, n):
    return min(lows[-n:]) if len(lows) >= n else min(lows)

def detect_candles(opens, highs, lows, closes):
    """Retourne quelques patterns simples sur la dernière bougie (et la précédente)."""
    if len(closes) < 2:
        return {"eng_bull": False, "hammer": False, "shooting": False, "doji": False}
    o1, h1, l1, c1 = opens[-2], highs[-2], lows[-2], closes[-2]
    o2, h2, l2, c2 = opens[-1], highs[-1], lows[-1], closes[-1]

    body1, body2 = abs(c1 - o1), abs(c2 - o2)
    rng2 = max(h2 - l2, 1e-9)

    # Bullish engulfing (simple)
    eng_bull = (c1 < o1) and (c2 > o2) and (c2 >= o1) and (o2 <= c1) and (body2 > body1 * 1.05)

    # Hammer: petite mèche haute, grande mèche basse (≥ 2x corps)
    lower = (o2 - l2) if o2 > c2 else (c2 - l2)
    upper = (h2 - o2) if o2 > c2 else (h2 - c2)
    hammer = (lower >= 2 * body2) and (upper <= 0.3 * body2)

    # Shooting star: opposé (mèche haute grande, mèche basse petite)
    shooting = (upper >= 2 * body2) and (lower <= 0.3 * body2)

    # Doji: corps très petit vs range
    doji = (body2 <= 0.1 * rng2)

    return {"eng_bull": eng_bull, "hammer": hammer, "shooting": shooting, "doji": doji}

def detect_vcp(atrp_series):
    """Very simple VCP: contraction sur 3 fenêtres récentes (décroissance de l'ATR%)."""
    if not atrp_series or len(atrp_series) < 60:
        return False
    # Moyennes par blocs (5 bougies) sur les 60 dernières
    blocks = []
    window = atrp_series[-60:]
    for i in range(0, 60, 5):
        seg = window[i:i+5]
        if len(seg) == 5:
            blocks.append(mean(seg))
    if len(blocks) < 6:
        return False
    # On regarde si les 3 derniers blocs diminuent
    return blocks[-3] > blocks[-2] > blocks[-1]

# ---------- API brutes ----------
def klines(symbol, interval="1h", limit=200):
    time.sleep(SLEEP_BETWEEN)
    return get_json(BASE + "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})

def orderbook_imbalance(symbol, depth=50):
    time.sleep(SLEEP_BETWEEN)
    ob = get_json(BASE + "/api/v3/depth", {"symbol": symbol, "limit": depth})
    if not ob or "bids" not in ob or "asks" not in ob:
        return None
    bids = sum(float(b[0]) * float(b[1]) for b in ob["bids"])
    asks = sum(float(a[0]) * float(a[1]) for a in ob["asks"])
    tot = bids + asks or 1e-9
    return (bids - asks) / tot


# ---------- MTF helpers ----------
def klines_safe(symbol, interval, limit):
    """Wrapper avec gestion simple des erreurs/retards."""
    try:
        return klines(symbol, interval, limit)
    except Exception:
        return None


def avwap_from_5m(kl_5m):
    """VWAP ancré au début de la journée UTC, approximé via 5m klines.
    Utilise TP = (H+L+C)/3 et le volume en quote (champ 7) comme poids.
    """
    if not kl_5m or len(kl_5m) == 0:
        return None
    # Trouver le début de la journée UTC de la dernière bougie
    last_ts_ms = int(kl_5m[-1][0])
    from datetime import datetime, timezone
    last_dt = datetime.fromtimestamp(last_ts_ms/1000, tz=timezone.utc)
    day_start = last_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_ms = int(day_start.timestamp()*1000)

    num = 0.0
    den = 0.0
    for b in kl_5m:
        ts = int(b[0])
        if ts < day_start_ms:
            continue
        h = float(b[2]); l = float(b[3]); c = float(b[4])
        tp = (h + l + c) / 3.0
        qv = float(b[7]) if len(b) > 7 else float(b[5])
        num += tp * qv
        den += qv
    if den <= 0:
        return None
    return num / den

def futures_oi_series(symbol_spot_like, limit=12):
    if DERIV_MODE == "off":
        return []
    fsym = symbol_spot_like.replace("USDC", "USDT")
    try:
        time.sleep(SLEEP_BETWEEN)
        data = get_json(FBASE + "/futures/data/openInterestHist",
                        {"symbol": fsym, "period": "5m", "limit": limit},
                        timeout=DERIV_TIMEOUT)
        return data or []
    except Exception:
        return []

def funding_rate_latest(symbol_spot_like):
    if DERIV_MODE == "off":
        return None
    fsym = symbol_spot_like.replace("USDC", "USDT")
    try:
        time.sleep(SLEEP_BETWEEN)
        data = get_json(FBASE + "/fapi/v1/fundingRate", {"symbol": fsym, "limit": 1}, timeout=DERIV_TIMEOUT)
        if data:
            return float(data[-1].get("fundingRate", 0.0))
    except Exception:
        return None
    return None

# ---------- quantiles/normalisations ----------
FUND_THRESH = [-0.10, -0.05, -0.02, -0.01, -0.005, 0.005, 0.01, 0.02, 0.05, 0.10]

def funding_to_quantile(f):
    if f is None:
        return None
    # map funding to pseudo-décile (0..9)
    for i, th in enumerate(FUND_THRESH):
        if f < th:
            return max(0, i - 1)
    return 9


# ---------- WebSocket helpers (temps réel) ----------
try:
    import websocket  # websocket-client
except Exception:  # import échoué → on gèrera à l'exécution
    websocket = None


def _sym_to_stream(sym: str) -> str:
    # ex: "SOLUSDC" -> "solusdc"
    return sym.lower()


def _build_combined_url(symbols, suffix: str) -> str:
    # suffix ex: "@kline_5m", "@kline_15m", "@bookTicker"
    streams = [f"{_sym_to_stream(s)}{suffix}" for s in symbols]
    return WS_BASE + "/".join(streams)


class LiveState:
    """État partagé entre threads WS et la boucle d'écriture."""
    def __init__(self, symbols):
        self.lock = threading.Lock()
        self.k5 = {s: deque(maxlen=288) for s in symbols}   # (close, high, low, qvol, ts_ms, is_closed)
        self.k15 = {s: deque(maxlen=120) for s in symbols}  # closes
        self.bt = {s: None for s in symbols}                # (bidQty, askQty)
        self.last_triggers = []

    def update_k5(self, sym, close, high, low, qvol, ts_ms, is_closed):
        with self.lock:
            self.k5[sym].append((close, high, low, qvol, ts_ms, is_closed))

    def update_k15(self, sym, close):
        with self.lock:
            self.k15[sym].append(close)

    def update_bt(self, sym, bid_qty, ask_qty):
        with self.lock:
            self.bt[sym] = (bid_qty, ask_qty)


def _ema(series, period):
    if not series:
        return None
    k = 2/(period+1)
    e = series[0]
    for v in series[1:]:
        e = v*k + e*(1-k)
    return e


def _compute_triggers_from_state(state: LiveState, symbols):
    out = []
    now_ms = int(time.time()*1000)
    with state.lock:
        for s in symbols:
            k5 = list(state.k5.get(s) or [])
            k15 = list(state.k15.get(s) or [])
            bt = state.bt.get(s)
            if not k5 or not k15:
                continue
            # 5m features
            closes5 = [x[0] for x in k5]
            highs5  = [x[1] for x in k5]
            lows5   = [x[2] for x in k5]
            qv5     = [x[3] for x in k5]
            price5 = closes5[-1]
            # RVOL5
            base5 = mean(qv5[-21:-1]) if len(qv5) >= 21 else (mean(qv5[:-1]) if len(qv5)>1 else qv5[-1])
            rvol5 = (qv5[-1]/(base5 or 1e-9)) if base5 else 1.0
            # ATR%5
            trs=[]
            for i in range(1, len(closes5)):
                trs.append(max(highs5[i]-lows5[i], abs(highs5[i]-closes5[i-1]), abs(lows5[i]-closes5[i-1])))
            atr5 = mean(trs[-14:]) if len(trs) >= 14 else (mean(trs) if trs else None)
            atrp5 = (atr5/(price5 or 1e-9)*100) if atr5 else None
            # HH20 5m
            try:
                hh20_5m = price5 >= max(highs5[-20:]) * 0.999
            except Exception:
                hh20_5m = None
            # AVWAP jour approximé via 5m
            vwap_num=vwap_den=0.0
            for (c,h,l,qv,ts,closed) in k5:
                # jour UTC pour le dernier point
                last_ts = k5[-1][4]
                day_start = (last_ts//86400000)*86400000
                if ts < day_start:
                    continue
                tp = (h+l+c)/3.0
                vwap_num += tp*qv
                vwap_den += qv
            p_vs_vwap = ((price5-(vwap_num/vwap_den))/(vwap_num/vwap_den)*100) if vwap_den>0 else None
            # 15m momentum
            ema9 = _compute_triggers_from_state._ema_cache.get((s,'m15','ema9'))
            ema21= _compute_triggers_from_state._ema_cache.get((s,'m15','ema21'))
            if ema9 is None or ema21 is None or len(k15) < 22:
                ema9 = _ema(k15[-60:], 9)
                ema21= _ema(k15[-90:], 21)
                _compute_triggers_from_state._ema_cache[(s,'m15','ema9')] = ema9
                _compute_triggers_from_state._ema_cache[(s,'m15','ema21')] = ema21
            mtf15_bull = (k15[-1] > (ema9 or 0) > (ema21 or 0)) if (ema9 and ema21) else None
            # OB approx via bookTicker
            ob_i = None
            if bt and (bt[0]+bt[1])>0:
                ob_i = (bt[0]-bt[1])/(bt[0]+bt[1])
            # Signal
            parts=[]
            if mtf15_bull: parts.append("15m-mom↑")
            if hh20_5m: parts.append("5m-HH20")
            if rvol5 and rvol5>=1.2: parts.append("5m-RVOL≥1.2")
            if (p_vs_vwap is not None) and p_vs_vwap>=0: parts.append(">VWAP")
            if (ob_i is not None) and ob_i>0.05: parts.append("OB+")
            signal = " ".join(parts) if parts else None
            out.append({
                "s": s,
                "p": round(price5, 6),
                "sig": signal,
                "rv5": round(rvol5, 2) if rvol5 is not None else None,
                "atrp5": round(atrp5, 2) if atrp5 is not None else None,
                "pvswap": round(p_vs_vwap, 2) if p_vs_vwap is not None else None,
                "ob": round(ob_i, 3) if ob_i is not None else None,
                "m15": bool(mtf15_bull) if mtf15_bull is not None else None
            })
    return out

# cache simple pour éviter de recalculer les EMA trop souvent
_compute_triggers_from_state._ema_cache = {}


def _ws_thread(url, onmsg):
    def _run():
        ws = websocket.WebSocketApp(url, on_message=lambda w, m: onmsg(m))
        ws.run_forever(ping_interval=20, ping_timeout=10)
    th = threading.Thread(target=_run, daemon=True)
    th.start()
    return th


def run_stream_mode(snapshot, ranking, duration_sec=STREAM_DURATION_SEC):
    if websocket is None:
        print("[STREAM] Le module 'websocket-client' n'est pas installé. Faites: pip install websocket-client", flush=True)
        return
    symbols = [r["symbol"] for r in ranking[:STREAM_TOP_N]]
    print(f"[STREAM] Lancement pour top {len(symbols)}: {', '.join(symbols)}", flush=True)

    state = LiveState(symbols)

    # Handlers de messages
    def on_kline_5m(msg):
        try:
            data = json.loads(msg).get('data', {})
            k = data.get('k', {})
            s = data.get('s') or k.get('s')
            if not s or s not in state.k5: return
            close = float(k.get('c'))
            high  = float(k.get('h'))
            low   = float(k.get('l'))
            qv    = float(k.get('q', 0.0))
            ts    = int(k.get('T'))
            closed= bool(k.get('x'))
            state.update_k5(s, close, high, low, qv, ts, closed)
        except Exception:
            return

    def on_kline_15m(msg):
        try:
            data = json.loads(msg).get('data', {})
            k = data.get('k', {})
            s = data.get('s') or k.get('s')
            if not s or s not in state.k15: return
            close = float(k.get('c'))
            state.update_k15(s, close)
        except Exception:
            return

    def on_book_ticker(msg):
        try:
            data = json.loads(msg).get('data', {})
            s = data.get('s')
            if not s or s not in state.bt: return
            bid_q = float(data.get('B', 0.0))
            ask_q = float(data.get('A', 0.0))
            state.update_bt(s, bid_q, ask_q)
        except Exception:
            return

    # Threads WS (3 connexions combinées)
    url5  = _build_combined_url(symbols, "@kline_5m")
    url15 = _build_combined_url(symbols, "@kline_15m")
    urlbt = _build_combined_url(symbols, "@bookTicker")
    t1 = _ws_thread(url5, on_kline_5m)
    t2 = _ws_thread(url15, on_kline_15m)
    t3 = _ws_thread(urlbt, on_book_ticker)

    print("[STREAM] Connexions ouvertes. Écriture périodique des triggers…", flush=True)
    t_end = time.time() + duration_sec
    try:
        while time.time() < t_end:
            time.sleep(STREAM_WRITE_EVERY)
            tr = _compute_triggers_from_state(state, symbols)
            state.last_triggers = tr
            # Réécrire un snapshot_min.json en réutilisant le ranking initial
            write_minified(ranking, snapshot["btc"], snapshot["macro"], snapshot["risk_factor"], triggers=tr)
            print(f"[STREAM] triggers={len(tr)} écrits → snapshot_min.json", flush=True)
    except KeyboardInterrupt:
        print("\n[STREAM] Arrêt demandé (Ctrl+C).", flush=True)
    finally:
        print("[STREAM] Fin du mode temps réel.", flush=True)


# ---------- MTF triggers ----------
def compute_mtf_triggers(ranked_alts, top_n=30):
    """Pour les N meilleurs (par score H1), calcule des signaux 15m/5m réactifs.
    Retourne une liste de dicts compacts pour le snapshot_min.
    """
    out = []
    count = 0
    for a in ranked_alts[:top_n]:
        sym = a["symbol"]
        p = a.get("price")
        ob_i = a.get("orderbook_imbalance")
        sc = a.get("score")

        # 15m
        mtf15_bull = None
        if FETCH_15M:
            k15 = klines_safe(sym, "15m", 120)
            if k15 and len(k15) >= 30:
                closes15 = [float(x[4]) for x in k15]
                ema9 = ema(closes15, 9)
                ema21 = ema(closes15, 21)
                mtf15_bull = (closes15[-1] > ema9 > ema21) if (ema9 and ema21) else None

        # 5m
        hh20_5m = None; rvol5 = None; atrp5 = None; p_vs_vwap = None
        if FETCH_5M:
            k5 = klines_safe(sym, "5m", 288)  # ~24h
            if k5 and len(k5) >= 30:
                highs5 = [float(x[2]) for x in k5]
                lows5  = [float(x[3]) for x in k5]
                closes5= [float(x[4]) for x in k5]
                vols5  = [float(x[7]) if len(x)>7 else float(x[5]) for x in k5]
                price5 = closes5[-1]
                # HH20 5m
                try:
                    hh20_5m = price5 >= max(highs5[-20:]) * 0.999
                except Exception:
                    hh20_5m = None
                # RVOL 5m
                base5 = mean(vols5[-21:-1]) if len(vols5) >= 21 else (mean(vols5[:-1]) if len(vols5)>1 else vols5[-1])
                rvol5 = (vols5[-1] / (base5 or 1e-9)) if base5 else 1.0
                # ATR% 5m
                def _atr(seq_h, seq_l, seq_c, per=14):
                    if len(seq_c) < per+1: return None
                    trs=[]
                    for i in range(1,len(seq_c)):
                        tr = max(seq_h[i]-seq_l[i], abs(seq_h[i]-seq_c[i-1]), abs(seq_l[i]-seq_c[i-1]))
                        trs.append(tr)
                    from statistics import mean as _m
                    return _m(trs[-per:])
                atr5 = _atr(highs5, lows5, closes5, 14)
                atrp5 = (atr5/(price5 or 1e-9)*100) if atr5 else None
                # AVWAP 5m (ancré jour UTC)
                vwap = avwap_from_5m(k5)
                p_vs_vwap = ((price5 - vwap)/vwap*100) if (vwap and price5) else None

        # Construire un libellé de signal simple
        sig_parts = []
        if mtf15_bull: sig_parts.append("15m-mom↑")
        if hh20_5m: sig_parts.append("5m-HH20")
        if rvol5 and rvol5 >= 1.2: sig_parts.append("5m-RVOL≥1.2")
        if (p_vs_vwap is not None) and (p_vs_vwap >= 0): sig_parts.append(">VWAP")
        if (ob_i is not None) and (ob_i > 0.05): sig_parts.append("OB+")
        signal = " ".join(sig_parts) if sig_parts else None

        out.append({
            "s": sym,
            "p": round(p or 0, 6),
            "sig": signal,
            "rv5": round(rvol5, 2) if rvol5 is not None else None,
            "atrp5": round(atrp5, 2) if atrp5 is not None else None,
            "pvswap": round(p_vs_vwap, 2) if p_vs_vwap is not None else None,
            "ob": round(ob_i, 3) if ob_i is not None else None,
            "sc": sc,
            "m15": bool(mtf15_bull) if mtf15_bull is not None else None
        })
        count += 1
    return out

# ---------- macro loader ----------
# ---------- macro loader ----------
def load_macro_events(path="macro.json"):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

# ---------- social & market loaders ----------
KNOWN_QUOTES = ["USDC","USDT","BUSD","USD","EUR","BTC","ETH"]

def base_from_symbol(sym: str) -> str:
    for q in KNOWN_QUOTES:
        if sym.endswith(q):
            return sym[: -len(q)]
    return sym

def load_social_signals(path="social.json"):
    """Charge des signaux sociaux optionnels.
    Format attendu (exemple):
    {
      "SOL": {"tw_sent": 0.42, "tw_vol_z": 1.8, "rd_sent": 0.2, "rd_vol_z": 0.9, "news_spike": true, "google_trend": 78},
      "LINK": {"tw_sent": -0.1, "tw_vol_z": 0.3}
    }
    """
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def load_macro_market(path="macro_market.json"):
    """Charge des proxys macro/risque (optionnels).
    Exemple:
    {"dxy": 103.4, "vix": 16.1, "us10y": 4.18, "btc_dominance": 52.5, "dxy_chg1d": 0.35, "btc_dom_chg7d": 0.9}
    """
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

# ---------- sélection symboles ----------
def get_top_symbols(quote="USDC", max_symbols=100):
    tickers = get_json(BASE + "/api/v3/ticker/24hr")
    if not tickers:
        raise RuntimeError("Impossible de récupérer /ticker/24hr")
    alts = [t for t in tickers if t.get("symbol", "").endswith(quote)]
    def qvol(t):
        try:
            return float(t.get("quoteVolume", "0"))
        except Exception:
            return 0.0
    alts = sorted(alts, key=qvol, reverse=True)[:max_symbols]
    major = [t for t in alts if qvol(t) >= LIQ_FLOOR]
    minor = [t for t in alts if qvol(t) < LIQ_FLOOR]
    combined = (major + minor)[:max_symbols]
    qvol_map = {t["symbol"]: float(t.get("quoteVolume", 0.0)) for t in combined}
    symbols = [t["symbol"] for t in combined]
    return symbols, qvol_map

# ---------- payload par alt + scoring ----------
def one_alt_payload(sym, qvol_map, compute_deriv=False):
    k = klines(sym, KLINE_INTERVAL, KLINE_LIMIT)
    if not k or len(k) < 50:
        raise RuntimeError(f"Klines insuffisants pour {sym}")
    opens  = [float(x[1]) for x in k]
    highs  = [float(x[2]) for x in k]
    lows   = [float(x[3]) for x in k]
    closes = [float(x[4]) for x in k]
    vols   = [float(x[7]) if len(x) > 7 else float(x[5]) for x in k]

    price = closes[-1]
    ema20v = ema(closes, 20)
    ema50v = ema(closes, 50)
    ema200v = ema(closes, 200) if len(closes) >= 200 else ema(closes, 200)
    rsi1h = rsi(closes, 14)
    atr1h = atr(highs, lows, closes, 14)
    atrp_series = [ (atr(highs[:i+1], lows[:i+1], closes[:i+1], 14) or 0) / (closes[i] or 1e-9) * 100 for i in range(len(closes)) ]
    atrp = (atr1h or 0) / (price or 1e-9) * 100

    base_vol = mean(vols[-21:-1]) if len(vols) >= 21 else (mean(vols[:-1]) if len(vols) > 1 else vols[-1])
    last_vol = vols[-1]
    rvol = (last_vol / (base_vol or 1e-9)) if base_vol else 1.0

    trend = "up" if (ema20v and ema50v and ema200v and ema20v > ema50v > ema200v and price > ema20v) else \
            ("down" if (ema20v and ema50v and ema200v and ema20v < ema50v < ema200v and price < ema20v) else "side")

    ob_imb = orderbook_imbalance(sym)

    # Dérivés (optionnels)
    oi = None; oi_d1h = None; funding = None; funding_q = None
    if compute_deriv:
        oi_hist = futures_oi_series(sym, limit=12)
        try:
            if oi_hist:
                vals = [float(x.get("sumOpenInterestValue", 0.0)) for x in oi_hist]
                oi = vals[-1]
                if len(vals) >= 12:
                    oi_d1h = vals[-1] - vals[0]
        except Exception:
            pass
        funding = funding_rate_latest(sym)
        funding_q = funding_to_quantile(funding) if funding is not None else None

    # Breakouts / ranges
    hh20 = price >= highest_high(highs, 20) * 0.999  # buffer 0.1%
    hh50 = price >= highest_high(highs, 50) * 0.999
    ll20 = price <= lowest_low(lows, 20) * 1.001
    rng50 = (highest_high(highs, 50) - lowest_low(lows, 50)) / (atr1h or 1e-9)

    # Patterns & VCP
    pats = detect_candles(opens, highs, lows, closes)
    vcp = detect_vcp(atrp_series)

    qv = float(qvol_map.get(sym, 0.0))
    liq_ok = (qv >= LIQ_FLOOR)

    # Scoring additif simple (0..6)
    score = 0.0
    if trend == "up":
        score += 1.0
    if rsi1h is not None:
        if rsi1h >= 70: score += 0.8
        elif rsi1h >= 60: score += 0.5
    if rvol is not None:
        if rvol >= 1.2: score += 0.5
        elif rvol >= 0.8: score += 0.3
    if hh20: score += 0.5
    if hh50: score += 0.5
    if liq_ok: score += 0.3
    if ob_imb is not None:
        if ob_imb > 0.15: score += 0.35
        elif ob_imb > 0.05: score += 0.2
        elif ob_imb < -0.10: score -= 0.3
    if funding_q is not None:
        if funding_q <= 2: score += 0.25  # squeeze long potentiel
        elif funding_q >= 8: score -= 0.25
    if oi_d1h and oi_d1h > 0: score += 0.2
    if pats.get("eng_bull"): score += 0.2
    if pats.get("hammer"): score += 0.15
    if pats.get("shooting"): score -= 0.2

    return {
        "symbol": sym,
        "quoteVol24h": qv,
        "price": round(price, 8),
        "ema20_1h": round(ema20v, 8) if ema20v else None,
        "ema50_1h": round(ema50v, 8) if ema50v else None,
        "ema200_1h": round(ema200v, 8) if ema200v else None,
        "rsi_1h": round(rsi1h, 2) if rsi1h is not None else None,
        "atr_1h": round(atr1h, 8) if atr1h is not None else None,
        "atrp_1h": round(atrp, 3),
        "rvol_1h": round(rvol, 2) if rvol is not None else None,
        "trend_1h": trend,
        "orderbook_imbalance": round(ob_imb, 4) if ob_imb is not None else None,
        "oi": round(oi, 2) if oi is not None else None,
        "oi_d1h": round(oi_d1h, 2) if oi_d1h is not None else None,
        "funding": round(funding, 6) if funding is not None else None,
        "funding_q": funding_q,
        "hh20": bool(hh20),
        "hh50": bool(hh50),
        "ll20": bool(ll20),
        "rng50_atr": round(rng50, 2) if rng50 is not None else None,
        "vcp": bool(vcp),
        "patterns": pats,
        "liquidity_ok": liq_ok,
        "score": round(score, 2)
    }

# ---------- BTC & macro ----------
def btc_payload():
    k = klines("BTC" + QUOTE, KLINE_INTERVAL, 240)
    if not k or len(k) < 50:
        raise RuntimeError("Klines BTC insuffisants")
    closes = [float(x[4]) for x in k]
    highs  = [float(x[2]) for x in k]
    lows   = [float(x[3]) for x in k]

    price = closes[-1]
    ema20v = ema(closes, 20)
    ema50v = ema(closes, 50)
    ema200v = ema(closes, 200) if len(closes) >= 200 else ema(closes, 200)
    rsi1h = rsi(closes, 14)

    ph, pl, pc = highs[-2], lows[-2], closes[-2]
    pivot = (ph + pl + pc) / 3
    structure = "HL" if price > (ema50v or price) else "LH"

    return {
        "price": round(price, 8),
        "pivot_h1": round(pivot, 8),
        "ema20": round(ema20v, 8) if ema20v else None,
        "ema50": round(ema50v, 8) if ema50v else None,
        "ema200": round(ema200v, 8) if ema200v else None,
        "rsi_h1": round(rsi1h, 2) if rsi1h is not None else None,
        "structure_h1": structure,
        "volume_h1": float(k[-1][7]) if len(k[-1]) > 7 else float(k[-1][5])
    }

# ---------- risk/macro factor ----------
def compute_risk_factor(btc, macro_list, market=None):
    rf = 1.0
    # Macro imminent (<24h) → prudence
    if macro_list:
        now = datetime.now(timezone.utc)
        for ev in macro_list:
            try:
                t = datetime.fromisoformat(ev.get("date").replace("Z", "+00:00"))
                if 0 <= (t - now).total_seconds() <= 24*3600 and ev.get("priority", "").lower() == "high":
                    rf *= 0.7
                    break
            except Exception:
                continue
    # BTC risk-on/off selon structure
    if btc.get("structure_h1") == "LH":
        rf *= 0.9
    # Ajustements via proxys marché (optionnels)
    if isinstance(market, dict) and market:
        dxy = market.get("dxy"); vix = market.get("vix"); dom = market.get("btc_dominance")
        dxy_chg = market.get("dxy_chg1d"); dom_chg = market.get("btc_dom_chg7d")
        try:
            if vix is not None and vix >= 20:
                rf *= 0.85
            if dxy_chg is not None and dxy_chg > 0.3:
                rf *= 0.9
            if dom_chg is not None and dom_chg > 1.0:
                rf *= 0.9
        except Exception:
            pass
    return round(rf, 3)

# ---------- build snapshot ----------
def build_snapshot():
    symbols, qvol_map = get_top_symbols(quote=QUOTE, max_symbols=MAX_SYMBOLS)
    print(f"[START] Universe={len(symbols)} (QUOTE={QUOTE})", flush=True)

    # BTC + macro d'abord (peut lever si absence réseau)
    btc = btc_payload()
    macro = load_macro_events()
    market = load_macro_market()
    rf = compute_risk_factor(btc, macro, market)
    print("[START] BTC/macro chargés", flush=True)

    alts = []
    for i, s in enumerate(symbols, 1):
        try:
            payload = one_alt_payload(s, qvol_map, compute_deriv=False)
            alts.append(payload)
            if i % 10 == 0:
                print(f"[H1] {i}/{len(symbols)} symbols traités…", flush=True)
        except Exception:
            continue

    # Intégration sociale (optionnelle) + petit boost/penalité capés
    social = load_social_signals()
    soc_used = False
    if isinstance(social, dict) and social:
        for a in alts:
            try:
                base = base_from_symbol(a["symbol"])  # e.g., SOL from SOLUSDC
                sig = social.get(base)
                if not isinstance(sig, dict):
                    continue
                soc_used = True
                tw = sig.get("tw_sent"); rd = sig.get("rd_sent")
                twz = sig.get("tw_vol_z"); rdz = sig.get("rd_vol_z")
                news = bool(sig.get("news_spike", False))
                gt = sig.get("google_trend")
                delta = 0.0
                if news:
                    delta += 0.2
                if isinstance(tw, (int, float)):
                    if tw > 0.3: delta += 0.15
                    elif tw < -0.3: delta -= 0.15
                if isinstance(rd, (int, float)):
                    if rd > 0.3: delta += 0.1
                    elif rd < -0.3: delta -= 0.1
                if isinstance(twz, (int, float)) and twz >= 1.0:
                    delta += 0.1
                if isinstance(rdz, (int, float)) and rdz >= 1.0:
                    delta += 0.05
                if isinstance(gt, (int, float)) and gt >= 75:
                    delta += 0.1
                # cap final
                if delta > 0.5: delta = 0.5
                if delta < -0.5: delta = -0.5
                a["social"] = {"tw": tw, "rd": rd, "twz": twz, "rdz": rdz, "news": news, "gt": gt, "soc": round(delta,2)}
                a["score"] = round(min(max((a.get("score") or 0) + delta, 0), 6), 2)
            except Exception:
                continue

    # Breadth marché (part des alts au-dessus de l'EMA50 1h)
    try:
        total = len(alts) or 1
        above50 = sum(1 for a in alts if a.get("ema50_1h") and a.get("price") and a["price"] > a["ema50_1h"])
        breadth50 = round(above50 / total * 100, 1)
    except Exception:
        breadth50 = None

    # ranking complet (desc score)
    ranking = sorted(alts, key=lambda x: (x.get("score") or 0), reverse=True)

    snap = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "equity_usdc": EQUITY_USDC,
        "btc": btc,
        "alts": alts,
        "macro": macro,
        "market": market,
        "breadth50": breadth50,
        "risk_factor": rf,
        "ranking": [{
            "symbol": r["symbol"], "score": r["score"], "liq": r["liquidity_ok"], "rvol": r.get("rvol_1h"),
            "fundQ": r.get("funding_q"), "soc": (r.get("social", {}) or {}).get("soc")
        } for r in ranking[:TOP_K]]
    }
    return snap, ranking

# ---------- packer minifié (éco-tokens) ----------
def write_minified(top_list, btc, macro, rf, triggers=None):
    mini = {
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "eq": EQUITY_USDC,
        "btc": {"st": btc.get("structure_h1"), "r": btc.get("rsi_h1"), "e50": btc.get("ema50")},
        "rf": rf,
        "br50": None,
        "mk": None,
        "mc": [{"t": ev.get("date"), "n": ev.get("event"), "p": ev.get("priority")} for ev in (macro or []) if ev.get("priority") == "high"],
        "top": []
    }
    for a in top_list[:TOP_K]:
        mini["top"].append({
            "s": a["symbol"],
            "p": round(a["price"], 6),
            "mom": {"r": a.get("rsi_1h"), "t": a.get("trend_1h")},
            "vol": {"a": a.get("atrp_1h"), "rv": a.get("rvol_1h")},
            "ob": {"i": a.get("orderbook_imbalance")},
            "der": {"oi": a.get("oi_d1h"), "fq": a.get("funding_q")},
            "brk": {"h20": a.get("hh20"), "h50": a.get("hh50")},
            "pat": {"eb": a.get("patterns", {}).get("eng_bull"), "ha": a.get("patterns", {}).get("hammer")},
            "liq": a.get("liquidity_ok"),
            "so": (a.get("social", {}) or {}).get("soc"),
            "sc": a.get("score")
        })
    if triggers:
        mini["tr"] = triggers
    # Propager breadth et un résumé des proxys macro si disponibles via variables globales
    try:
        # On lit le snapshot complet si présent pour récupérer breadth50 et market
        full = json.loads(Path("snapshot.json").read_text(encoding="utf-8"))
        mini["br50"] = full.get("breadth50")
        mk = full.get("market")
        if isinstance(mk, dict):
            mini["mk"] = {k: mk.get(k) for k in ("dxy","vix","btc_dominance","dxy_chg1d","btc_dom_chg7d") if k in mk}
    except Exception:
        pass
    Path("snapshot_min.json").write_text(json.dumps(mini, separators=(",", ":")), encoding="utf-8")

if __name__ == "__main__":
    # Mode streaming temps réel: `python src/collector.py --stream [durée_sec]`
    if len(sys.argv) >= 2 and sys.argv[1].lstrip("-–—") == "stream":
        try:
            dur = int(sys.argv[2]) if len(sys.argv) >= 3 else STREAM_DURATION_SEC
        except Exception:
            dur = STREAM_DURATION_SEC
        print("[START] Build snapshot initial (contexte + ranking)…", flush=True)
        snapshot, ranking = build_snapshot()
        Path("snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        write_minified(ranking, snapshot["btc"], snapshot["macro"], snapshot["risk_factor"], triggers=snapshot.get("triggers"))
        print(f"[START] Snapshot initial écrit. Lancement du stream ({dur}s)…", flush=True)
        run_stream_mode(snapshot, ranking, duration_sec=dur)
    else:
        # Mode batch (comme avant)
        snapshot, ranking = build_snapshot()
        triggers = compute_mtf_triggers(ranking, top_n=MTF_TOP_N)
        snapshot["triggers"] = triggers
        Path("snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        if WRITE_MINIFIED:
            write_minified(ranking, snapshot["btc"], snapshot["macro"], snapshot["risk_factor"], triggers=triggers)
        print(f"snapshot.json écrit. Alts incluses: {len(snapshot.get('alts', []))}. Top minifié: {min(len(ranking), TOP_K)} → snapshot_min.json")