#!/usr/bin/env python3
"""
Forex Signal Bot - Runs once to check for signals and exits.
Designed to be triggered by Render Cron Job every 6 minutes.
"""

import os
import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from twelvedata import TDClient
from telegram import Bot
from telegram.error import TelegramError

# ========== CONFIGURATION (from environment variables) ==========
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")

# Validate required environment variables
if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TWELVEDATA_API_KEY]):
    logging.error("Missing required environment variables!")
    logging.error("Required: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TWELVEDATA_API_KEY")
    exit(1)

PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY"]
PAIR_SYMBOLS = {
    "EUR/USD": "EUR/USD",
    "GBP/USD": "GBP/USD",
    "USD/JPY": "USD/JPY"
}

TIMEFRAME = "15min"
FAST_MA = 20
SLOW_MA = 50
RSI_PERIOD = 14
ADX_PERIOD = 14
ADX_THRESHOLD = 25
HIGHER_TF = "1h"

# Pullback settings
PULLBACK_MA = 20
PULLBACK_RSI_MIN_BUY = 40
PULLBACK_RSI_MAX_SELL = 60

# Target position size in USD
TARGET_NOTIONAL = 15000.0

# News avoidance (simplified static list)
HIGH_IMPACT_NEWS_TIMES = [
    "08:30", "13:15", "07:00", "04:00", "22:30"
]
NEWS_BLACKOUT_MINUTES = 30

# ========== SETUP LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== TWELVE DATA CLIENT ==========
td = TDClient(apikey=TWELVEDATA_API_KEY)

def fetch_historical_data(symbol, interval, days_back=5):
    """Fetch historical OHLC data from Twelve Data API."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    try:
        ts = td.time_series(
            symbol=symbol,
            interval=interval,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            outputsize=500
        )
        df = ts.as_pandas()
        if df is not None and not df.empty:
            df = df.rename(columns={
                'open': 'Open', 
                'high': 'High', 
                'low': 'Low', 
                'close': 'Close'
            })
            return df
        return None
    except Exception as e:
        logger.error(f"Error fetching {symbol}: {e}")
        return None

# ========== INDICATOR CALCULATIONS ==========
def calculate_ema(series, period):
    """Calculate Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()

def calculate_rsi(series, period=14):
    """Calculate RSI using Wilder's smoothing."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_adx(df, period=14):
    """Calculate Average Directional Index (ADX)."""
    high = df['High']
    low = df['Low']
    close = df['Close'].shift(1)
    
    tr1 = high - low
    tr2 = abs(high - close)
    tr3 = abs(low - close)
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    
    up_move = high.diff()
    down_move = -low.diff()
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    
    plus_di = 100 * (pd.Series(plus_dm).rolling(window=period).mean() / atr)
    minus_di = 100 * (pd.Series(minus_dm).rolling(window=period).mean() / atr)
    
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.rolling(window=period).mean()
    
    return adx, plus_di, minus_di

def get_higher_tf_trend(symbol):
    """Check trend on higher timeframe (1H) using 200 EMA."""
    df = fetch_historical_data(symbol, HIGHER_TF, days_back=10)
    if df is None or len(df) < 200:
        return "neutral"
    ema200 = calculate_ema(df['Close'], 200)
    current_price = df['Close'].iloc[-1]
    current_ema = ema200.iloc[-1]
    if current_price > current_ema:
        return "bullish"
    elif current_price < current_ema:
        return "bearish"
    return "neutral"

def is_near_high_impact_news():
    """Check if current time is within blackout period of high-impact news."""
    now = datetime.now()
    if now.weekday() >= 5:  # Saturday or Sunday
        return True
    
    current_time = now.strftime("%H:%M")
    for news_time in HIGH_IMPACT_NEWS_TIMES:
        news_dt = datetime.strptime(news_time, "%H:%M").time()
        current_dt = now.time()
        news_minutes = news_dt.hour * 60 + news_dt.minute
        current_minutes = current_dt.hour * 60 + current_dt.minute
        if abs(current_minutes - news_minutes) <= NEWS_BLACKOUT_MINUTES:
            return True
    return False

def calculate_position_units(price_usd: float, pair: str) -> int:
    """Calculate units to trade for a $15,000 position value."""
    if pair == "USD/JPY":
        units = TARGET_NOTIONAL
    else:
        units = TARGET_NOTIONAL / price_usd
    return int(round(units))

def get_signal(pair_symbol):
    """Generate trading signal with crossover AND pullback entries."""
    symbol = PAIR_SYMBOLS[pair_symbol]
    df = fetch_historical_data(symbol, TIMEFRAME, days_back=5)
    if df is None or len(df) < 150:
        return None, None, f"Insufficient data for {pair_symbol}"
    
    df['EMA_Fast'] = calculate_ema(df['Close'], FAST_MA)
    df['EMA_Slow'] = calculate_ema(df['Close'], SLOW_MA)
    df['EMA_Pullback'] = calculate_ema(df['Close'], PULLBACK_MA)
    df['RSI'] = calculate_rsi(df['Close'], RSI_PERIOD)
    df['ADX'], _, _ = calculate_adx(df, ADX_PERIOD)
    
    last = df.iloc[-2]
    prev = df.iloc[-3]
    price = last['Close']
    rsi = last['RSI']
    adx = last['ADX']
    ema_pull = last['EMA_Pullback']
    
    curr_fast = last['EMA_Fast']
    curr_slow = last['EMA_Slow']
    prev_fast = prev['EMA_Fast']
    prev_slow = prev['EMA_Slow']
    buy_cross = (prev_fast <= prev_slow) and (curr_fast > curr_slow)
    sell_cross = (prev_fast >= prev_slow) and (curr_fast < curr_slow)
    
    if pd.isna(adx) or adx < ADX_THRESHOLD:
        return None, None, f"ADX: {adx:.1f} (<{ADX_THRESHOLD})"
    if is_near_high_impact_news():
        return None, None, "News blackout"
    
    ht_trend = get_higher_tf_trend(symbol)
    
    # Crossover signals
    if buy_cross and 50 < rsi < 70 and ht_trend == "bullish":
        return "BUY (Crossover)", price, f"Price: {price:.5f} | RSI: {rsi:.1f} | ADX: {adx:.1f} | HTF: {ht_trend}"
    if sell_cross and 30 < rsi < 50 and ht_trend == "bearish":
        return "SELL (Crossover)", price, f"Price: {price:.5f} | RSI: {rsi:.1f} | ADX: {adx:.1f} | HTF: {ht_trend}"
    
    # Pullback signals
    prev_close = prev['Close']
    prev_ema = prev['EMA_Pullback']
    last_close = last['Close']
    bullish_pullback = (prev_close < prev_ema) and (last_close > ema_pull) and (ht_trend == "bullish") and (rsi > PULLBACK_RSI_MIN_BUY)
    bearish_pullback = (prev_close > prev_ema) and (last_close < ema_pull) and (ht_trend == "bearish") and (rsi < PULLBACK_RSI_MAX_SELL)
    
    if bullish_pullback:
        return "BUY (Pullback)", last_close, f"Price: {last_close:.5f} | RSI: {rsi:.1f} | ADX: {adx:.1f} | Pullback to EMA{PULLBACK_MA}"
    if bearish_pullback:
        return "SELL (Pullback)", last_close, f"Price: {last_close:.5f} | RSI: {rsi:.1f} | ADX: {adx:.1f} | Pullback to EMA{PULLBACK_MA}"
    
    return None, None, f"No signal | ADX:{adx:.1f} | HTF:{ht_trend}"

async def send_signal(bot, pair, signal_type, details, price):
    """Send signal to Telegram with position size recommendation."""
    units = calculate_position_units(price, pair)
    units_formatted = f"{units:,}"
    mini_lots = units / 10000
    lots_formatted = f"{mini_lots:.2f}"
    
    msg = (
        f"🔔 *{signal_type} for {pair}*\n"
        f"{details}\n"
        f"\n"
        f"💰 *Position Size:* $15,000 notional\n"
        f"📊 *Units to trade:* {units_formatted}\n"
        f"🎯 *That's:* {lots_formatted} mini lots (1 mini lot = 10,000 units)\n"
        f"\n"
        f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}"
    )
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")
        logger.info(f"Sent {signal_type} for {pair} - Position: {units_formatted} units")
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")

async def main():
    """Main entry point - checks all pairs once and exits."""
    logger.info("Starting forex signal check...")
    
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    signals_sent = 0
    
    for pair in PAIRS:
        logger.info(f"Checking {pair}...")
        signal_type, price, details = get_signal(pair)
        if signal_type:
            await send_signal(bot, pair, signal_type, details, price)
            signals_sent += 1
        else:
            logger.info(f"{pair}: {details}")
    
    logger.info(f"Signal check complete. {signals_sent} signal(s) sent.")
    
    # Flush any pending logs before exit
    for handler in logging.root.handlers:
        handler.flush()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())