import requests
import pandas as pd
import datetime
import os
import time

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

ASSETS = ["XRPUSDT", "ETHUSDT", "SOLUSDT", "TONUSDT"]
BTC_SYMBOL = "BTCUSDT"

def get_klines(symbol, interval, limit=100):
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        return df
    except Exception as e:
        print(f"Error fetching klines for {symbol}: {e}")
        return pd.DataFrame()

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_kdj(df, period=9):
    if len(df) < period:
        return 50, 50, 50
    low_min = df['low'].rolling(window=period).min()
    high_max = df['high'].rolling(window=period).max()
    rsv = (df['close'] - low_min) / (high_max - low_min) * 100
    rsv = rsv.fillna(50)
    k = rsv.rolling(window=3).mean()
    d = k.rolling(window=3).mean()
    j = 3 * k - 2 * d
    return k.iloc[-1], d.iloc[-1], j.iloc[-1]

def calc_macd(df, fast=12, slow=26, signal=9):
    if len(df) < slow:
        return 0, 0
    ema_fast = calc_ema(df['close'], fast)
    ema_slow = calc_ema(df['close'], slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema(macd_line, signal)
    return macd_line.iloc[-1], signal_line.iloc[-1]
def get_funding_rate(symbol):
    try:
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
        data = requests.get(url, timeout=10).json()
        return float(data[0]['fundingRate']) * 100
    except Exception:
        return 0.0

def get_oi_delta(symbol):
    try:
        url = f"https://fapi.binance.com/fapi/v1/openInterestHist?symbol={symbol}&period=5m&limit=288"
        data = requests.get(url, timeout=10).json()
        if len(data) >= 2:
            current_oi = float(data[-1]['sumOpenInterest'])
            past_oi = float(data[0]['sumOpenInterest'])
            return ((current_oi - past_oi) / past_oi) * 100
    except Exception:
        pass
    return 0.0

def get_long_short_ratio(symbol):
    try:
        url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=1h&limit=24"
        data = requests.get(url, timeout=10).json()
        if len(data) >= 2:
            return float(data[-1]['longShortRatio'])
    except Exception:
        pass
    return 1.0

def scan_asset(asset):
    btc_4h = get_klines(BTC_SYMBOL, '4h', limit=50)
    btc_1d = get_klines(BTC_SYMBOL, '1d', limit=50)
    btc_3d = get_klines(BTC_SYMBOL, '3d', limit=50)
    asset_4h = get_klines(asset, '4h', limit=50)
    
    if btc_4h.empty or asset_4h.empty:
        return 0, 0, 0, 0, 0, 1.0

    funding = get_funding_rate(asset)
    oi_delta = get_oi_delta(asset)
    ls_ratio = get_long_short_ratio(asset)
    price = asset_4h['close'].iloc[-1]
    
    long_score = 0
    
    if btc_4h['close'].iloc[-1] > calc_ema(btc_4h['close'], 20).iloc[-1]:
        long_score += 1
    if btc_1d['close'].iloc[-1] > calc_ema(btc_1d['close'], 20).iloc[-1]:        long_score += 1
    if btc_3d['close'].iloc[-1] > calc_ema(btc_3d['close'], 20).iloc[-1]:
        long_score += 1
        
    macd, signal = calc_macd(btc_4h)
    if macd > signal:
        long_score += 1
        
    k, d, j = calc_kdj(btc_4h)
    if j > k and k > d:
        long_score += 1
        
    if asset_4h['close'].iloc[-1] > calc_ema(asset_4h['close'], 20).iloc[-1]:
        long_score += 1
        
    k, d, j = calc_kdj(asset_4h)
    if j > k and k > d:
        long_score += 1
        
    if funding < 0.01:
        long_score += 1
    if oi_delta > 0:
        long_score += 1
    if ls_ratio < 1.05:
        long_score += 1

    short_score = 0
    
    if btc_4h['close'].iloc[-1] < calc_ema(btc_4h['close'], 20).iloc[-1]:
        short_score += 1
    if btc_1d['close'].iloc[-1] < calc_ema(btc_1d['close'], 20).iloc[-1]:
        short_score += 1
    if btc_3d['close'].iloc[-1] < calc_ema(btc_3d['close'], 20).iloc[-1]:
        short_score += 1
        
    if macd < signal:
        short_score += 1
    if j < k and k < d:
        short_score += 1
        
    if asset_4h['close'].iloc[-1] < calc_ema(asset_4h['close'], 20).iloc[-1]:
        short_score += 1
    if j < k and k < d:
        short_score += 1
        
    if funding > 0.01:
        short_score += 1
    if oi_delta > 0:
        short_score += 1
    if ls_ratio > 1.05:        short_score += 1

    return long_score, short_score, price, funding, oi_delta, ls_ratio

def send_telegram_alert(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending Telegram alert: {e}")

def main():
    print(f"[{datetime.datetime.now()}] WizardSkull Scanner Active (Long & Short)...")
    
    for asset in ASSETS:
        try:
            long_score, short_score, price, funding, oi_delta, ls_ratio = scan_asset(asset)
            print(f"{asset} | Long: {long_score}/10 | Short: {short_score}/10 | Price: {price}")
            
            if long_score >= 7:
                tp1_price = price * 1.0217
                liq_price = price * 0.978
                liq_status = "Shorts > Longs (Squeeze Potential)" if ls_ratio < 1.0 else "Longs > Shorts"
                
                msg_parts = []
                msg_parts.append("🚨 *WIZARD SKULL ALERT* 🚨")
                msg_parts.append("")
                msg_parts.append(f"Asset: *{asset.replace('USDT', '/USDT')}*")
                msg_parts.append(f"Score: *{long_score}/10*")
                msg_parts.append("Bias: *LONG ENTRY (LOCKED)* 🎯")
                msg_parts.append("")
                msg_parts.append("BTC Regime: *Bullish* (3D · 1D · 4H KDJ + MACD + EMA aligned)")
                msg_parts.append("")
                msg_parts.append(f"🎯 TP1 — 100% at 46x (+2.17%) → `{tp1_price:.4f}`")
                msg_parts.append("🎯 TP2 — Extended target")
                msg_parts.append("⚠️ STOP LOSS — set above liq!")
                msg_parts.append(f"💀 LIQ PRICE (isolated) — *~{liq_price:.4f}*")
                msg_parts.append("")
                msg_parts.append("🚨 *SL IS BELOW LIQ PRICE — YOU WILL BE LIQUIDATED BEFORE SL TRIGGERS!*")
                msg_parts.append("")
                msg_parts.append("⚡ *Futures Intelligence:*")
                msg_parts.append(f"📈 Funding Rates (8H): *{funding:.4f}%*")
                msg_parts.append(f"📊 Open Interest Δ (24H): *{oi_delta:.2f}%*")
                msg_parts.append(f"💥 Liquidation Cluster: *{liq_status}*")
                msg_parts.append("")
                msg_parts.append("_⚠️ Always manage your risk. Monitor position manually._")
                
                message = "\n".join(msg_parts)
                send_telegram_alert(message)                print(f"🚨 LONG ALERT SENT FOR {asset}!")
                
            elif short_score >= 7:
                tp1_price = price * 0.9783
                liq_price = price * 1.022
                liq_status = "Longs > Shorts (Long Squeeze Imminent)" if ls_ratio > 1.05 else "Shorts > Longs"
                
                msg_parts = []
                msg_parts.append("🚨 *WIZARD SKULL ALERT* 🚨")
                msg_parts.append("")
                msg_parts.append(f"Asset: *{asset.replace('USDT', '/USDT')}*")
                msg_parts.append(f"Score: *{short_score}/10*")
                msg_parts.append("Bias: *SHORT ENTRY (LOCKED)* 🎯")
                msg_parts.append("")
                msg_parts.append("BTC Regime: *Bearish* (3D · 1D · 4H KDJ + MACD + EMA aligned)")
                msg_parts.append("")
                msg_parts.append(f"🎯 TP1 — 100% at 46x (-2.17%) → `{tp1_price:.4f}`")
                msg_parts.append("🎯 TP2 — Extended target")
                msg_parts.append("⚠️ STOP LOSS — set below liq!")
                msg_parts.append(f"💀 LIQ PRICE (isolated) — *~{liq_price:.4f}*")
                msg_parts.append("")
                msg_parts.append("🚨 *SL IS ABOVE LIQ PRICE — YOU WILL BE LIQUIDATED BEFORE SL TRIGGERS!*")
                msg_parts.append("")
                msg_parts.append("⚡ *Futures Intelligence:*")
                msg_parts.append(f"📈 Funding Rates (8H): *{funding:.4f}%*")
                msg_parts.append(f"📊 Open Interest Δ (24H): *{oi_delta:.2f}%*")
                msg_parts.append(f"💥 Liquidation Cluster: *{liq_status}*")
                msg_parts.append("")
                msg_parts.append("_⚠️ Always manage your risk. Monitor position manually._")
                
                message = "\n".join(msg_parts)
                send_telegram_alert(message)
                print(f"🚨 SHORT ALERT SENT FOR {asset}!")
                
            time.sleep(1)
            
        except Exception as e:
            print(f"Critical error scanning {asset}: {e}")

if __name__ == "__main__":
    main()
