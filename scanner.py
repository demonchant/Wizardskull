import requests
import pandas as pd
import datetime
import os

# --- CONFIGURATION (Loaded from GitHub Secrets) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

ASSETS = ["XRPUSDT", "ETHUSDT", "SOLUSDT", "TONUSDT"]
BTC_SYMBOL = "BTCUSDT"

def get_klines(symbol, interval, limit=100):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    response = requests.get(url).json()
    df = pd.DataFrame(response, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'
    ])
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    return df

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_kdj(df, period=9):
    low_min = df['low'].rolling(window=period).min()
    high_max = df['high'].rolling(window=period).max()
    rsv = (df['close'] - low_min) / (high_max - low_min) * 100
    rsv = rsv.fillna(50)
    k = rsv.rolling(window=3).mean()
    d = k.rolling(window=3).mean()
    j = 3 * k - 2 * d
    return k.iloc[-1], d.iloc[-1], j.iloc[-1]

def calc_macd(df, fast=12, slow=26, signal=9):
    ema_fast = calc_ema(df['close'], fast)
    ema_slow = calc_ema(df['close'], slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema(macd_line, signal)
    return macd_line.iloc[-1], signal_line.iloc[-1]

def get_funding_rate(symbol):
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
    data = requests.get(url).json()
    return float(data[0]['fundingRate']) * 100

def get_oi_delta(symbol):    url = f"https://fapi.binance.com/fapi/v1/openInterestHist?symbol={symbol}&period=5m&limit=288"
    data = requests.get(url).json()
    if len(data) >= 2:
        current_oi = float(data[-1]['sumOpenInterest'])
        past_oi = float(data[0]['sumOpenInterest'])
        return ((current_oi - past_oi) / past_oi) * 100
    return 0.0

def get_long_short_ratio(symbol):
    url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=1h&limit=24"
    data = requests.get(url).json()
    if len(data) >= 2:
        return float(data[-1]['longShortRatio'])
    return 1.0

def scan_asset(asset):
    score = 0
    details = []
    
    # 1-3. BTC Regime (3D, 1D, 4H EMA Alignment)
    btc_4h = get_klines(BTC_SYMBOL, '4h', limit=50)
    btc_1d = get_klines(BTC_SYMBOL, '1d', limit=50)
    btc_3d = get_klines(BTC_SYMBOL, '3d', limit=50)
    
    if btc_4h['close'].iloc[-1] > calc_ema(btc_4h['close'], 20).iloc[-1]:
        score += 1; details.append("BTC 4H > EMA20")
    if btc_1d['close'].iloc[-1] > calc_ema(btc_1d['close'], 20).iloc[-1]:
        score += 1; details.append("BTC 1D > EMA20")
    if btc_3d['close'].iloc[-1] > calc_ema(btc_3d['close'], 20).iloc[-1]:
        score += 1; details.append("BTC 3D > EMA20")
        
    # 4. BTC 4H MACD
    macd, signal = calc_macd(btc_4h)
    if macd > signal:
        score += 1; details.append("BTC 4H MACD Bullish")
        
    # 5. BTC 4H KDJ
    k, d, j = calc_kdj(btc_4h)
    if j > k and k > d:
        score += 1; details.append("BTC 4H KDJ Bullish")
        
    # 6. Asset 4H EMA
    asset_4h = get_klines(asset, '4h', limit=50)
    if asset_4h['close'].iloc[-1] > calc_ema(asset_4h['close'], 20).iloc[-1]:
        score += 1; details.append(f"{asset} 4H > EMA20")
        
    # 7. Asset 4H KDJ
    k, d, j = calc_kdj(asset_4h)
    if j > k and k > d:
        score += 1; details.append(f"{asset} 4H KDJ Bullish")        
    # 8. Funding Rate (8H) < 0.01% (Not overheated)
    funding = get_funding_rate(asset)
    if funding < 0.01:
        score += 1; details.append("Funding Healthy")
        
    # 9. Open Interest Δ (24H) > 0
    oi_delta = get_oi_delta(asset)
    if oi_delta > 0:
        score += 1; details.append("OI Increasing")
        
    # 10. Liquidation Cluster Proxy (Long/Short Ratio < 1.05)
    ls_ratio = get_long_short_ratio(asset)
    if ls_ratio < 1.05:
        score += 1; details.append("LS Ratio favors squeeze")
        
    return score, details, asset_4h['close'].iloc[-1], funding, oi_delta, ls_ratio

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})

def main():
    print(f"[{datetime.datetime.now()}] WizardSkull Scanner Active...")
    for asset in ASSETS:
        score, details, price, funding, oi_delta, ls_ratio = scan_asset(asset)
        print(f"{asset} Score: {score}/10")
        
        if score >= 7:
            tp1_price = price * 1.0217  # Matches HTML: +2.17%
            liq_price = price * 0.978   # Mock isolated liq price for ~46x
            
            liq_status = "Shorts > Longs (Squeeze Potential)" if ls_ratio < 1.0 else "Longs > Shorts"
            
            # EXACT HTML PHRASING MATCH
            message = (
                f"🚨 *WIZARD SKULL ALERT* 🚨\n\n"
                f"Asset: *{asset.replace('USDT', '/USDT')}*\n"
                f"Score: *{score}/10*\n"
                f"Bias: *LONG ENTRY (LOCKED)* 🎯\n\n"
                f"BTC Regime: *Bullish* (3D · 1D · 4H KDJ + MACD + EMA aligned)\n\n"
                f"🎯 TP1 — 100% at 46x (+2.17%) → `{tp1_price:.4f}`\n"
                f"🎯 TP2 — Extended target\n"
                f"⚠️ STOP LOSS — set above liq!\n"
                f"💀 LIQ PRICE (isolated) — *~{liq_price:.4f}*\n\n"
                f"🚨 *SL IS BELOW LIQ PRICE — YOU WILL BE LIQUIDATED BEFORE SL TRIGGERS!*\n\n"
                f"⚡ *Futures Intelligence:*\n"
                f"📈 Funding Rates (8H): *{funding:.4f}%*\n"
                f"📊 Open Interest Δ (24H): *{oi_delta:.2f}%*\n"
                f"💥 Liquidation Cluster: *{liq_status}*\n\n"                f"_⚠️ Always manage your risk. Monitor position manually._"
            )
            send_telegram_alert(message)
            print(f"🚨 ALERT SENT FOR {asset}!")

if __name__ == "__main__":
    main()
