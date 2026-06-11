"""
WizardSkull Futures Scanner
===========================
Multi-asset futures scanner: XRP, ETH, SOL, TON
BTC regime detection + 10-point scoring engine
KDJ · MACD · EMA · Volume · Funding · OI · Liquidations
Sends Telegram alerts when score >= 7/10

Usage:
  pip install requests pandas
  Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as env vars or edit below
  python wizardskull_scanner.py
"""

import requests
import pandas as pd
import datetime
import os
import time

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

ASSETS = ["XRPUSDT", "ETHUSDT", "SOLUSDT", "TONUSDT"]
BTC_SYMBOL = "BTCUSDT"

LEVERAGE     = 46
TP1_PCT      = 2.174   # 100% at 46x
TP2_PCT      = 4.5
SL_PCT       = 2.174
ENTRY_THRESH = 7       # score out of 10 required to alert

SPOT_BASE    = "https://api.binance.com/api/v3"
FUTURES_BASE = "https://fapi.binance.com/fapi/v1"
FUTURES_DATA = "https://fapi.binance.com/futures/data"

# ══════════════════════════════════════════════════════════
# BINANCE API HELPERS
# ══════════════════════════════════════════════════════════

def get_klines(symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
    """Fetch OHLCV klines from Binance spot API."""
    try:
        url = f"{SPOT_BASE}/klines"
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        r.raise_for_status()
        cols = ["timestamp","open","high","low","close","volume",
                "close_time","qav","num_trades","taker_base","taker_quote","ignore"]
        df = pd.DataFrame(r.json(), columns=cols)
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        print(f"  [klines error] {symbol} {interval}: {e}")
        return pd.DataFrame()


def get_ticker(symbol: str) -> dict:
    """Fetch 24h ticker data."""
    try:
        r = requests.get(f"{SPOT_BASE}/ticker/24hr", params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [ticker error] {symbol}: {e}")
        return {}


def get_funding_rate(symbol: str) -> dict | None:
    """Latest funding rate for a futures symbol."""
    try:
        r = requests.get(f"{FUTURES_BASE}/fundingRate", params={"symbol": symbol, "limit": 1}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        print(f"  [funding error] {symbol}: {e}")
        return None


def get_open_interest(symbol: str) -> dict | None:
    """Current open interest."""
    try:
        r = requests.get(f"{FUTURES_BASE}/openInterest", params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [OI error] {symbol}: {e}")
        return None


def get_oi_hist(symbol: str, period: str = "1h", limit: int = 24) -> list:
    """Historical open interest (for delta calculation)."""
    try:
        r = requests.get(
            f"{FUTURES_BASE}/openInterestHist",
            params={"symbol": symbol, "period": period, "limit": limit},
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [OI hist error] {symbol}: {e}")
        return []


def get_forced_orders(symbol: str) -> list:
    """Recent liquidation orders (last 24h)."""
    try:
        since = int((datetime.datetime.utcnow() - datetime.timedelta(hours=24)).timestamp() * 1000)
        r = requests.get(
            f"{FUTURES_BASE}/allForceOrders",
            params={"symbol": symbol, "startTime": since, "limit": 1000},
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [liq error] {symbol}: {e}")
        return []


def get_long_short_ratio(symbol: str, period: str = "1h", limit: int = 1) -> dict | None:
    """Global long/short account ratio."""
    try:
        r = requests.get(
            f"{FUTURES_DATA}/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        return data[-1] if data else None
    except Exception as e:
        print(f"  [L/S ratio error] {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════
# INDICATOR CALCULATIONS
# ══════════════════════════════════════════════════════════

def calc_kdj(df: pd.DataFrame, period: int = 9) -> tuple[float, float, float]:
    """
    Calculate KDJ (Stochastic variant).
    Returns (K, D, J) for the last candle.
    """
    if df.empty or len(df) < period:
        return 50.0, 50.0, 50.0

    low_min  = df["low"].rolling(window=period, min_periods=1).min()
    high_max = df["high"].rolling(window=period, min_periods=1).max()
    denom    = high_max - low_min
    rsv      = ((df["close"] - low_min) / denom.replace(0, 1)) * 100

    K, D = 50.0, 50.0
    Ks, Ds, Js = [], [], []
    for r in rsv:
        K = K * (2/3) + r * (1/3)
        D = D * (2/3) + K * (1/3)
        Ks.append(K)
        Ds.append(D)
        Js.append(3 * K - 2 * D)

    return Ks[-1], Ds[-1], Js[-1]


def calc_kdj_series(df: pd.DataFrame, period: int = 9) -> tuple[list, list, list]:
    """Returns full K, D, J series (for cross detection)."""
    if df.empty or len(df) < period:
        return [50.0], [50.0], [50.0]

    low_min  = df["low"].rolling(window=period, min_periods=1).min()
    high_max = df["high"].rolling(window=period, min_periods=1).max()
    denom    = high_max - low_min
    rsv      = ((df["close"] - low_min) / denom.replace(0, 1)) * 100

    K, D = 50.0, 50.0
    Ks, Ds, Js = [], [], []
    for r in rsv:
        K = K * (2/3) + r * (1/3)
        D = D * (2/3) + K * (1/3)
        Ks.append(K)
        Ds.append(D)
        Js.append(3 * K - 2 * D)

    return Ks, Ds, Js


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def calc_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float]:
    """
    Returns (dif, dea, histogram) for the last candle.
    dif  = EMA(fast) - EMA(slow)
    dea  = EMA(dif, signal)
    hist = dif - dea
    """
    if df.empty or len(df) < slow:
        return 0.0, 0.0, 0.0
    closes   = df["close"]
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    dif      = ema_fast - ema_slow
    dea      = calc_ema(dif, signal)
    hist     = dif - dea
    return float(dif.iloc[-1]), float(dea.iloc[-1]), float(hist.iloc[-1])


def calc_ema_last(df: pd.DataFrame, period: int) -> float:
    """Last value of EMA."""
    if df.empty:
        return 0.0
    return float(calc_ema(df["close"], period).iloc[-1])


def analyze_volume(df: pd.DataFrame) -> dict:
    """Compare latest 4H volume vs 20-period average."""
    if df.empty or len(df) < 2:
        return {"avg": 0, "last": 0, "ratio": 1.0, "high": False}
    vols = df["volume"].values
    avg  = float(vols[-20:].mean())
    last = float(vols[-1])
    return {"avg": avg, "last": last, "ratio": last / avg if avg > 0 else 1.0, "high": last > avg * 1.5}


def detect_cross(Ks: list, Ds: list, Js: list) -> str:
    """Detect golden or death cross on last two candles."""
    if len(Ks) < 2:
        return "none"
    prevJ, curJ = Js[-2], Js[-1]
    prevK, curK = Ks[-2], Ks[-1]
    prevD, curD = Ds[-2], Ds[-1]
    if (prevJ < prevK and curJ >= curK) or (prevK < prevD and curK >= curD):
        return "golden"
    if (prevJ > prevK and curJ <= curK) or (prevK > prevD and curK <= curD):
        return "death"
    return "none"


# ══════════════════════════════════════════════════════════
# BTC REGIME DETECTION
# ══════════════════════════════════════════════════════════

def determine_btc_regime(btc_data: dict) -> dict:
    """
    Votes across 3D, 1D, 4H using KDJ + MACD + EMA.
    Returns regime: 'bull' | 'bear' | 'sideways'
    """
    tfs    = ["3D", "1D", "4H"]
    votes  = 0
    tf_votes = {}

    for tf in tfs:
        d        = btc_data[tf]
        tf_score = 0

        # KDJ signal
        if   d["J"] < 40: tf_score += 1
        elif d["J"] > 60: tf_score -= 1

        # MACD histogram
        if   d["macd_hist"] > 0: tf_score += 1
        elif d["macd_hist"] < 0: tf_score -= 1

        # Price vs EMA50
        if d["price"] and d["ema50"]:
            if   d["price"] > d["ema50"]: tf_score += 1
            elif d["price"] < d["ema50"]: tf_score -= 1

        tf_votes[tf] = "bull" if tf_score > 0 else ("bear" if tf_score < 0 else "neutral")
        votes += tf_score

    # Extra weight for 3D
    j3d = btc_data["3D"]["J"]
    if j3d < 35: votes += 1
    if j3d > 65: votes -= 1

    if   votes >=  3: regime = "bull"
    elif votes <= -3: regime = "bear"
    else:             regime = "sideways"

    return {"regime": regime, "votes": votes, "tf_votes": tf_votes}


# ══════════════════════════════════════════════════════════
# ASSET BIAS
# ══════════════════════════════════════════════════════════

def get_asset_bias(asset_3d_j: float, btc_3d_j: float, regime: str) -> str:
    """Determine trade direction based on regime + 3D KDJ."""
    if regime == "bull":
        return "long"
    if regime == "bear":
        return "short"
    # Sideways: use asset 3D J
    if asset_3d_j < 40: return "long"
    if asset_3d_j > 60: return "short"
    return "neutral"


# ══════════════════════════════════════════════════════════
# FUTURES DATA ANALYSIS
# ══════════════════════════════════════════════════════════

def analyze_futures(symbol: str) -> dict:
    """
    Fetch and synthesize futures intelligence:
    Funding rate, Open Interest delta, Liquidations, L/S ratio.
    Returns futures score (-3 to +3) and bias.
    """
    print(f"    Fetching futures data for {symbol}...")

    fr_data   = get_funding_rate(symbol)
    oi_data   = get_open_interest(symbol)
    oi_hist   = get_oi_hist(symbol, period="1h", limit=24)
    liq_data  = get_forced_orders(symbol)
    ls_data   = get_long_short_ratio(symbol)

    # Funding rate
    funding_rate     = float(fr_data["lastFundingRate"]) * 100 if fr_data else None
    next_funding_ms  = int(fr_data["nextFundingTime"]) if fr_data else None
    next_funding_str = ""
    if next_funding_ms:
        next_funding_str = datetime.datetime.utcfromtimestamp(next_funding_ms / 1000).strftime("%H:%M UTC")

    # Open interest + delta
    oi_current = float(oi_data["openInterest"]) if oi_data else None
    oi_delta   = None
    if len(oi_hist) >= 2:
        oldest = float(oi_hist[0]["sumOpenInterest"])
        newest = float(oi_hist[-1]["sumOpenInterest"])
        oi_delta = ((newest - oldest) / oldest) * 100 if oldest > 0 else None

    # Liquidation clusters
    long_liq_total  = 0.0
    short_liq_total = 0.0
    for liq in liq_data:
        qty = float(liq.get("origQty", 0)) * float(liq.get("price", 0))
        if liq.get("side") == "BUY":   short_liq_total += qty   # short was liquidated
        else:                           long_liq_total  += qty   # long was liquidated

    # L/S ratio
    ls_ratio = float(ls_data["longShortRatio"]) if ls_data else None

    # Futures score synthesis
    futures_score = 0

    if funding_rate is not None:
        if   funding_rate < -0.01: futures_score += 2   # strong negative = long favored
        elif funding_rate < 0:     futures_score += 1
        elif funding_rate > 0.05:  futures_score -= 2   # overheated longs
        elif funding_rate > 0.01:  futures_score -= 1

    if oi_delta is not None:
        if oi_delta >  5: futures_score += 1   # rising OI = conviction
        if oi_delta < -5: futures_score -= 1   # falling OI = weakening

    if ls_ratio is not None:
        if ls_ratio < 0.8: futures_score += 1  # too many shorts = squeeze potential
        if ls_ratio > 1.5: futures_score -= 1  # crowd long = fade

    total_liq = long_liq_total + short_liq_total
    if total_liq > 0:
        short_liq_pct = short_liq_total / total_liq
        if short_liq_pct > 0.65: futures_score += 1  # shorts being wrecked = uptrend
        if short_liq_pct < 0.35: futures_score -= 1  # longs being wrecked = downtrend

    futures_bias = "neutral"
    if futures_score >=  2: futures_bias = "bull"
    if futures_score <= -2: futures_bias = "bear"

    return {
        "funding_rate":     funding_rate,
        "next_funding_str": next_funding_str,
        "oi_current":       oi_current,
        "oi_delta":         oi_delta,
        "long_liq_total":   long_liq_total,
        "short_liq_total":  short_liq_total,
        "ls_ratio":         ls_ratio,
        "futures_score":    futures_score,
        "futures_bias":     futures_bias,
    }


# ══════════════════════════════════════════════════════════
# 10-POINT SCORING ENGINE
# ══════════════════════════════════════════════════════════

def score_signal(asset_data: dict, btc_data: dict, bias: str, futures_data: dict, regime: str) -> dict:
    """
    Score a setup from 0 to 10.
    7 technical points + 3 futures points.
    """
    score = 0
    chips = []

    def add(label: str, condition: bool, warn: bool = False, futures: bool = False):
        nonlocal score
        if condition:
            score += 1
        chips.append({"label": label, "pass": condition, "warn": warn, "futures": futures})

    # ── TECHNICAL (7 points) ──────────────────────────────

    # 1. 3D KDJ
    a3j = asset_data["3D"]["J"]
    add("3D KDJ", a3j < 40 if bias == "long" else a3j > 60)

    # 2. 1D KDJ
    a1j = asset_data["1D"]["J"]
    add("1D KDJ", a1j < 45 if bias == "long" else a1j > 55)

    # 3. 4H KDJ entry zone
    a4j = asset_data["4H"]["J"]
    add("4H KDJ", a4j < 35 if bias == "long" else a4j > 65)

    # 4. 4H MACD histogram
    macd4 = asset_data["4H"]["macd_hist"]
    add("4H MACD", macd4 > -0.0005 if bias == "long" else macd4 < 0.0005)

    # 5. Price vs EMA20 on 4H (Bollinger mid proxy)
    price  = asset_data["price"]
    ema20  = asset_data["4H"]["ema20"]
    near   = abs(price - ema20) / ema20 < 0.005 if ema20 else False
    add("4H EMA", price < ema20 if bias == "long" else price > ema20, warn=near)

    # 6. BTC regime alignment
    regime_aligned = (regime == "bull" and bias == "long") or (regime == "bear" and bias == "short")
    sideways_ok    = regime == "sideways"
    add("BTC Regime", regime_aligned or sideways_ok, warn=sideways_ok)

    # 7. Volume spike
    add("Volume", asset_data["4H"]["vol_high"])

    # ── FUTURES (3 points) ───────────────────────────────

    fr    = futures_data.get("funding_rate")
    oi_d  = futures_data.get("oi_delta")
    ll    = futures_data.get("long_liq_total", 0)
    sl_   = futures_data.get("short_liq_total", 0)
    total = ll + sl_

    # 8. Funding rate
    fr_pass = (fr < 0) if bias == "long" else (fr > 0) if fr is not None else False
    fr_warn = (fr is not None) and ((fr < 0.01) if bias == "long" else (fr > -0.01))
    add("Funding", fr_pass, warn=fr_warn, futures=True)

    # 9. OI delta expanding
    add("OI Delta", oi_d is not None and oi_d > 2, warn=(oi_d is not None and oi_d > 0), futures=True)

    # 10. Liquidation cluster alignment
    liq_pass = False
    if total > 0:
        slp = sl_ / total
        liq_pass = (slp > 0.55) if bias == "long" else (slp < 0.45)
    add("Liq Cluster", liq_pass, futures=True)

    return {"score": score, "chips": chips}


# ══════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] No token/chat_id configured — printing to console instead.")
        print(message)
        return
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("  [Telegram] Alert sent ✓")
    except Exception as e:
        print(f"  [Telegram error] {e}")


def build_alert_message(asset: str, bias: str, score: int, price: float,
                         chips: list, futures_data: dict, btc_regime: str,
                         btc_3d_j: float, btc_1d_j: float, btc_4h_j: float) -> str:
    """Build the full Telegram alert message."""
    direction_label = "LONG ENTRY 🚀" if bias == "long" else "SHORT ENTRY 🔻"
    emoji           = "🚨" if bias == "long" else "⚠️"
    tp1  = price * (1 + TP1_PCT / 100) if bias == "long" else price * (1 - TP1_PCT / 100)
    tp2  = price * (1 + TP2_PCT / 100) if bias == "long" else price * (1 - TP2_PCT / 100)
    sl   = price * (1 - SL_PCT  / 100) if bias == "long" else price * (1 + SL_PCT  / 100)
    liq  = price * (1 - 2.2    / 100) if bias == "long" else price * (1 + 2.2    / 100)

    # Chips summary
    pass_chips = [c["label"] for c in chips if c["pass"]]
    fail_chips = [c["label"] for c in chips if not c["pass"]]

    fr  = futures_data.get("funding_rate")
    oid = futures_data.get("oi_delta")
    lsr = futures_data.get("ls_ratio")
    nfs = futures_data.get("next_funding_str", "—")

    lines = [
        f"{emoji} *WIZARDSKULL ALERT* {emoji}",
        "",
        f"Asset: *{asset.replace('USDT', '/USDT')}*",
        f"Score: *{score}/10*",
        f"Direction: *{direction_label}*",
        f"BTC Regime: *{btc_regime.upper()}*",
        "",
        f"BTC 3D J: `{btc_3d_j:.1f}` · 1D J: `{btc_1d_j:.1f}` · 4H J: `{btc_4h_j:.1f}`",
        "",
        f"💰 Entry: `{price:.4f}`",
        f"🎯 TP1 — 100% at {LEVERAGE}x → `{tp1:.4f}`",
        f"🎯 TP2 — Extended → `{tp2:.4f}`",
        f"🛑 Stop Loss → `{sl:.4f}`",
        f"💀 Approx Liq (isolated {LEVERAGE}x) → `{liq:.4f}`",
        "",
        "✅ *Confirmed signals:* " + ", ".join(pass_chips) if pass_chips else "",
        "❌ *Missing:* " + ", ".join(fail_chips)           if fail_chips else "",
        "",
        "⚡ *Futures Intelligence:*",
        f"📈 Funding Rate: `{fr:+.4f}%`"                  if fr is not None else "📈 Funding: N/A",
        f"📊 OI Delta 24H: `{oid:+.2f}%`"                 if oid is not None else "📊 OI Delta: N/A",
        f"⚖️ L/S Ratio: `{lsr:.2f}`"                      if lsr is not None else "⚖️ L/S Ratio: N/A",
        f"⏰ Next Funding: `{nfs}`"                        if nfs else "",
        "",
        "_⚠️ Always manage your risk. Never trade without a stop loss._",
    ]
    return "\n".join(l for l in lines if l != "")


# ══════════════════════════════════════════════════════════
# ASSET DATA FETCH
# ══════════════════════════════════════════════════════════

def fetch_asset_data(symbol: str) -> dict | None:
    """Fetch all klines and compute indicators for one asset."""
    print(f"  Fetching {symbol}...")
    try:
        k3d  = get_klines(symbol, "3d",  limit=80)
        k1d  = get_klines(symbol, "1d",  limit=120)
        k4h  = get_klines(symbol, "4h",  limit=120)
        k1h  = get_klines(symbol, "1h",  limit=100)
        k15m = get_klines(symbol, "15m", limit=100)
        ticker = get_ticker(symbol)

        if k4h.empty or not ticker:
            return None

        price = float(ticker.get("lastPrice", 0))

        def tf_block(df: pd.DataFrame) -> dict:
            K, D, J     = calc_kdj(df)
            Ks, Ds, Js  = calc_kdj_series(df)
            _, _, hist  = calc_macd(df)
            ema20       = calc_ema_last(df, 20)
            ema21       = calc_ema_last(df, 21)
            ema50       = calc_ema_last(df, 50)
            cross       = detect_cross(Ks, Ds, Js)
            return {
                "K": K, "D": D, "J": J,
                "macd_hist": hist,
                "ema20": ema20, "ema21": ema21, "ema50": ema50,
                "cross": cross,
                "price": price,
            }

        vol4h = analyze_volume(k4h)

        return {
            "symbol": symbol,
            "price": price,
            "3D":  tf_block(k3d),
            "1D":  tf_block(k1d),
            "4H":  {**tf_block(k4h), "vol_high": vol4h["high"], "vol_ratio": vol4h["ratio"]},
            "1H":  tf_block(k1h),
            "15M": tf_block(k15m),
        }
    except Exception as e:
        print(f"  [asset fetch error] {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ══════════════════════════════════════════════════════════

def run_scan() -> None:
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"  WizardSkull Futures Scanner  |  {now}")
    print(f"{'='*60}")

    # ── 1. Fetch BTC data for regime detection ──────────────
    print("\n[BTC] Fetching regime data...")
    btc_k3d = get_klines(BTC_SYMBOL, "3d",  limit=80)
    btc_k1d = get_klines(BTC_SYMBOL, "1d",  limit=120)
    btc_k4h = get_klines(BTC_SYMBOL, "4h",  limit=120)
    btc_k1h = get_klines(BTC_SYMBOL, "1h",  limit=100)
    btc_ticker = get_ticker(BTC_SYMBOL)

    btc_price = float(btc_ticker.get("lastPrice", 0))

    def btc_tf(df: pd.DataFrame) -> dict:
        K, D, J      = calc_kdj(df)
        _, _, hist   = calc_macd(df)
        ema50        = calc_ema_last(df, 50)
        Ks, Ds, Js   = calc_kdj_series(df)
        cross        = detect_cross(Ks, Ds, Js)
        return {"K": K, "D": D, "J": J, "macd_hist": hist, "ema50": ema50, "cross": cross, "price": btc_price}

    btc_data = {
        "3D": btc_tf(btc_k3d),
        "1D": btc_tf(btc_k1d),
        "4H": btc_tf(btc_k4h),
        "1H": btc_tf(btc_k1h),
    }

    regime_result = determine_btc_regime(btc_data)
    regime        = regime_result["regime"]
    votes         = regime_result["votes"]
    tf_votes      = regime_result["tf_votes"]

    btc_3d_j = btc_data["3D"]["J"]
    btc_1d_j = btc_data["1D"]["J"]
    btc_4h_j = btc_data["4H"]["J"]

    print(f"\n  BTC Regime: {regime.upper()} (votes: {votes:+d})")
    print(f"  BTC J values — 3D: {btc_3d_j:.1f}  |  1D: {btc_1d_j:.1f}  |  4H: {btc_4h_j:.1f}")
    print(f"  TF votes: " + "  ".join(f"{tf}:{v}" for tf, v in tf_votes.items()))

    # ── 2. Scan each asset ───────────────────────────────────
    print(f"\n[ASSETS] Scanning {len(ASSETS)} assets...")

    for asset in ASSETS:
        print(f"\n  ── {asset} ──────────────────────────")
        asset_data = fetch_asset_data(asset)
        if not asset_data:
            print(f"  Skipping {asset} — no data")
            continue

        price = asset_data["price"]
        a3j   = asset_data["3D"]["J"]
        a1j   = asset_data["1D"]["J"]
        a4j   = asset_data["4H"]["J"]

        print(f"  Price: {price:.4f}")
        print(f"  KDJ — 3D J:{a3j:.1f}  1D J:{a1j:.1f}  4H J:{a4j:.1f}")

        # KDJ cross alerts
        for tf in ["3D", "1D", "4H", "1H"]:
            c = asset_data[tf].get("cross", "none")
            if c == "golden":
                print(f"  ⚡ {tf} GOLDEN CROSS")
            elif c == "death":
                print(f"  ☠  {tf} DEATH CROSS")

        bias = get_asset_bias(a3j, btc_3d_j, regime)
        print(f"  Bias: {bias.upper()}")

        if bias == "neutral":
            print(f"  Score: N/A (neutral bias — 3D J in 40-60 range)")
            continue

        # ── Fetch futures data
        futures_data = analyze_futures(asset)
        print(f"  Funding: {futures_data['funding_rate']:+.4f}%  |  OI Delta: {futures_data['oi_delta']:+.1f}%  |  L/S: {futures_data['ls_ratio']:.2f}" if futures_data['funding_rate'] is not None and futures_data['oi_delta'] is not None and futures_data['ls_ratio'] is not None else f"  Futures: partial data")

        # ── Score
        result = score_signal(asset_data, btc_data, bias, futures_data, regime)
        score  = result["score"]
        chips  = result["chips"]

        pass_labels = [c["label"] for c in chips if c["pass"]]
        fail_labels = [c["label"] for c in chips if not c["pass"]]

        print(f"  Score: {score}/10  |  ✅ {pass_labels}  |  ❌ {fail_labels}")

        # ── Fire alert if score >= threshold
        if score >= ENTRY_THRESH:
            print(f"\n  🚨 SIGNAL FIRED — {bias.upper()} {asset} @ {price:.4f}  Score: {score}/10")
            msg = build_alert_message(
                asset       = asset,
                bias        = bias,
                score       = score,
                price       = price,
                chips       = chips,
                futures_data= futures_data,
                btc_regime  = regime,
                btc_3d_j    = btc_3d_j,
                btc_1d_j    = btc_1d_j,
                btc_4h_j    = btc_4h_j,
            )
            send_telegram(msg)
        else:
            needed = ENTRY_THRESH - score
            print(f"  No signal — need {needed} more point(s) to trigger (threshold {ENTRY_THRESH}/10)")

        time.sleep(1)  # avoid rate limiting between assets

    print(f"\n{'='*60}")
    print(f"  Scan complete  |  {datetime.datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"{'='*60}\n")


# ══════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_scan()
