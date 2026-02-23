import asyncio
import httpx
import json
import time
import websockets
from datetime import datetime
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from typing import List, Dict, Optional
import os
import sys
from fastapi.staticfiles import StaticFiles

# --- Configuration ---
import sys
import os

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

REST_URL = "https://api.binance.com/api/v3"
WS_URL = "wss://stream.binance.com:9443/ws"
telegram_config = {
    "enabled": False,
    "bot_token": "",
    "chat_id": ""
}

class ExchangeManager:
    def __init__(self):
        self.tickers: Dict[str, dict] = {} # Symbol -> Ticker Data
        self.screener_results: List[dict] = [] # Persistent volume spikes
        self.momentum_events: List[dict] = [] # Persistent 3.5% spikes, crosses, etc.
        self.last_screener_update = 0
        self.symbols: List[str] = []
        self.ws_connected = False
        self.crossed_1m = set() # Track symbols that crossed 1M today
    
    def cleanup_old_events(self):
        """Remove signals older than 1 hour"""
        now = time.time()
        # Events have a timestamp like "H:M:S", we need to store unix time for cleanup
        self.momentum_events = [e for e in self.momentum_events if (now - e.get('unix_time', 0)) < 3600]
        self.screener_results = [e for e in self.screener_results if (now - e.get('unix_time', 0)) < 3600]

    def reset_crossovers(self):
        self.crossed_1m.clear()

manager = ExchangeManager()
app = FastAPI()
app.mount("/libs", StaticFiles(directory=resource_path("libs")), name="libs")

# --- Helper Functions ---

async def fetch_candle_analysis(client, symbol, tf="1h"):
    try:
        limit = 6 # History for comparison
        url = f"{REST_URL}/klines?symbol={symbol}&interval={tf}&limit={limit}"
        resp = await client.get(url, timeout=3.0)
        if resp.status_code != 200: return None
        k = resp.json()
        if len(k) < 3: return None
        
        # Use Quote Asset Volume (index 7) and Trades (index 8)
        prev_vol = float(k[-2][7])
        curr_vol = float(k[-1][7])
        prev_trades = int(k[-2][8])
        curr_trades = int(k[-1][8])
        
        if prev_vol == 0: return None
        
        vol_ratio = curr_vol / prev_vol
        trade_ratio = curr_trades / prev_trades if prev_trades > 0 else 0
        
        if vol_ratio >= 1.5 or trade_ratio >= 1.5:
            return {
                "symbol": symbol.replace("USDT", "/USDT"),
                "timeframe": tf,
                "price": float(k[-1][4]),
                "vol_ratio": round(vol_ratio, 2),
                "trade_ratio": round(trade_ratio, 2),
                "curr_vol": curr_vol,
                "prev_vol": prev_vol,
                "curr_trades": curr_trades,
                "prev_trades": prev_trades,
                "timestamp": datetime.now().isoformat()
            }
    except:
        pass
    return None

async def send_telegram_alert(res):
    if not telegram_config['enabled']: return
    msg = (f"🚀 *UNUSUAL VOLUME DETECTED*\n\n"
           f"Symbol: {res['symbol']}\n"
           f"Price: ${res['price']}\n"
           f"Vol Ratio: {res['vol_ratio']}x\n"
           f"Trade Ratio: {res['trade_ratio']}x\n"
           f"Time: {res['timestamp']}")
    url = f"https://api.telegram.org/bot{telegram_config['bot_token']}/sendMessage"
    payload = {"chat_id": telegram_config['chat_id'], "text": msg, "parse_mode": "Markdown"}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload)
        except:
            pass

# --- Background Loops ---

async def websocket_listener():
    while True:
        try:
            async with websockets.connect(f"{WS_URL}/!ticker@arr") as ws:
                manager.ws_connected = True
                while True:
                    data = await ws.recv()
                    tickers = json.loads(data)
                    for t in tickers:
                        symbol = t['s']
                        if symbol.endswith('USDT'):
                            price = float(t['c'])
                            quoteVol = float(t['q'])
                            
                            # 1M USDT Volume Crossover Detection
                            if quoteVol >= 1000000 and symbol not in manager.crossed_1m:
                                manager.crossed_1m.add(symbol)
                                event_id = f"{symbol}_vol_1m_{int(time.time())}"
                                manager.momentum_events.insert(0, {
                                    "id": event_id,
                                    "symbol": symbol,
                                    "timeframe": "24h",
                                    "type": "VOL_1M_CROSS",
                                    "value": round(quoteVol / 1000000, 2),
                                    "price": price,
                                    "timestamp": datetime.now().strftime("%H:%M:%S")
                                })
                                manager.momentum_events = manager.momentum_events[:100]

                            manager.tickers[symbol] = {
                                "symbol": symbol,
                                "price": price,
                                "change_percent": float(t['P']),
                                "volume": float(t['v']),
                                "quoteVol": quoteVol,
                                "high": float(t['h']),
                                "low": float(t['l']),
                                "trades": int(t['n'])
                            }
        except Exception as e:
            manager.ws_connected = False
            await asyncio.sleep(5)

async def volume_screener_loop():
    """Heavy scanner for volume spikes across multiple timeframes"""
    async with httpx.AsyncClient() as client:
        first_run = True
        while True:
            if not first_run:
                await asyncio.sleep(60) # Scan every minute after first run
            first_run = False

            symbols = list(manager.tickers.keys())
            if not symbols:
                await asyncio.sleep(5)
                continue
            
            # Sort by volume to pick active ones
            sorted_symbols = sorted(symbols, key=lambda x: manager.tickers[x]['quoteVol'], reverse=True)
            top_symbols = sorted_symbols[:100]
            
            # Scan common timeframes
            scan_tfs = ["5m", "15m", "1h", "4h"]
            results = []
            for tf in scan_tfs:
                for i in range(0, len(top_symbols), 10):
                    batch = top_symbols[i:i+10]
                    tasks = [fetch_candle_analysis(client, s, tf) for s in batch]
                    batch_res = await asyncio.gather(*tasks)
                    
                    found_any = [r for r in batch_res if r]
                    results.extend(found_any)
                    
                    # DISAPPEAR IF FALSE Logic: 
                    # If a symbol was checked and NOT in batch_res, remove its live alert for this TF
                    for idx, s in enumerate(batch):
                        if not batch_res[idx]:
                            # Remove from results if it was a "Live" alert (same TF)
                            manager.screener_results = [e for e in manager.screener_results if not (e['symbol'] == s.replace("USDT", "/USDT") and e['timeframe'] == tf)]
                    
                    await asyncio.sleep(0.3)

            if results:
                now = time.time()
                for res in results:
                    res['unix_time'] = now
                    # Smart Persistence Logic:
                    # 1. Update/Add if condition met
                    # 2. If it's the SAME candle (id), it updates the live state
                    existing = next((e for e in manager.screener_results if e['symbol'] == res['symbol'] and e['timeframe'] == res['timeframe']), None)
                    if existing:
                        manager.screener_results.remove(existing)
                    
                    manager.screener_results.insert(0, res)
                
                manager.cleanup_old_events()
                manager.last_screener_update = now
                print(f"Screener: Found {len(results)} spikes")
                
                if telegram_config['enabled']:
                    for res in results:
                        if res['vol_ratio'] >= 2.0 or res['trade_ratio'] >= 2.0:
                             asyncio.create_task(send_telegram_alert(res))

async def momentum_scanner_loop():
    """Optimized scanner to stay under 1200 weight/min while watching 411+ tokens across 11 timeframes"""
    async with httpx.AsyncClient() as client:
        # We'll cycle through timeframes over different iterations to spread the load
        # Priority: 1m, 5m (checked every cycle)
        # Secondary: 3m, 15m, 30m, 1h (checked every 3rd cycle)
        # Rare: 2h, 4h, 6h, 12h, 1d (checked every 10th cycle)
        iteration = 0
        while True:
            symbols = list(manager.tickers.keys())
            if not symbols:
                await asyncio.sleep(5)
                continue
            
            iteration += 1
            tfs = ["1m", "5m", "15m"]
            if iteration % 2 == 0: tfs += ["30m", "1h"]
            if iteration % 5 == 0: tfs += ["4h", "1d"]
            if iteration % 10 == 0: tfs += ["3m", "2h", "6h", "12h"]

            # Batch processing for high speed and rate limit safety
            batch_size = 40 
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i+batch_size]
                # Process batch concurrently
                tasks = [check_symbol_momentum(client, s, tfs) for s in batch]
                results = await asyncio.gather(*tasks)
                
                for idx, events in enumerate(results):
                    s = batch[idx]
                    if not events:
                        for tf in tfs:
                            manager.momentum_events = [e for e in manager.momentum_events if not (e['symbol'] == s and e['timeframe'] == tf)]
                    else:
                        for res in events:
                            res['unix_time'] = time.time()
                            manager.momentum_events = [e for e in manager.momentum_events if e.get('id') != res['id']]
                            manager.momentum_events.insert(0, res)
                
                # Maintain limit and cleanup
                manager.momentum_events = manager.momentum_events[:240]
                manager.cleanup_old_events()
                await asyncio.sleep(2.0) # Increased sleep for rate limit safety
            
            # 30 second pause before next cycle to stay within 1200 weight/min
            await asyncio.sleep(30) 

async def check_symbol_momentum(client, symbol, tfs):
    """Checks specific timeframes for a symbol for multiple alert types"""
    events = []
    try:
        for tf in tfs:
            # We need data for EMA 21 and 25 (at least 30-40 candles for accuracy)
            limit = 40
            url = f"{REST_URL}/klines?symbol={symbol}&interval={tf}&limit={limit}"
            resp = await client.get(url, timeout=4.0)
            if resp.status_code != 200: continue
            k = resp.json()
            if not k or not isinstance(k, list) or len(k) < 2: continue
            
            # 1. Price Spike Check (Last Candle)
            last = k[-1]
            prev = k[-2]
            o, cl = float(last[1]), float(last[4])
            if o != 0:
                move = ((cl - o) / o) * 100
                if abs(move) >= 3.5:
                    events.append({
                        "id": f"{symbol}_{tf}_spike_{last[0]}",
                        "symbol": symbol,
                        "timeframe": tf,
                        "type": "PRICE_SPIKE",
                        "value": round(move, 2),
                        "price": cl,
                        "volume": round(float(last[7]) / 1000000, 2), # Enriched with M USDT Volume
                        "timestamp": datetime.now().strftime("%H:%M:%S")
                    })

            # 2. Volume Breakout Check (Using Quote/USDT Volume - index 7)
            if len(k) >= 6:
                # Average of last 5 candles (Quote Volume)
                quote_vols = [float(x[7]) for x in k[-6:-1]]
                avg_qvol = sum(quote_vols) / len(quote_vols)
                curr_qvol = float(last[7])
                
                if avg_qvol > 0 and curr_qvol > avg_qvol * 3.0: # 3x breakout
                    events.append({
                        "id": f"{symbol}_{tf}_vol_{last[0]}",
                        "symbol": symbol,
                        "timeframe": tf,
                        "type": "VOL_BREAKOUT",
                        "value": round(curr_qvol / avg_qvol, 1),
                        "price": cl,
                        "timestamp": datetime.now().strftime("%H:%M:%S")
                    })

                # 3. Transaction (Trade Count) Breakout Check - index 8
                # Only trigger if current candle USDT volume > 1,000,000 (as requested)
                trades = [int(x[8]) for x in k[-6:-1]]
                avg_trades = sum(trades) / len(trades)
                curr_trades = int(last[8])
                curr_qvol = float(last[7])
                
                if avg_trades > 0 and curr_trades > avg_trades * 3.0 and curr_qvol >= 1000000:
                    events.append({
                        "id": f"{symbol}_{tf}_trades_{last[0]}",
                        "symbol": symbol,
                        "timeframe": tf,
                        "type": "TRADE_BREAKOUT",
                        "value": round(curr_trades / avg_trades, 1),
                        "price": cl,
                        "timestamp": datetime.now().strftime("%H:%M:%S")
                    })

            # 3. EMA Crossover Check (7/25) - Only for 1m, 5m, 15m to save weight
            if limit >= 30 and len(k) >= 30:
                def get_ema(data, period):
                    if len(data) < period: return 0
                    sma = sum(data[:period]) / period
                    mult = 2 / (period + 1)
                    ema = sma
                    for val in data[period:]:
                        ema = (val - ema) * mult + ema
                    return ema

                closes = [float(x[4]) for x in k]
                # Previous EMAs
                p_ema7 = get_ema(closes[:-1], 7)
                p_ema25 = get_ema(closes[:-1], 25)
                # Current EMAs
                c_ema7 = get_ema(closes, 7)
                c_ema25 = get_ema(closes, 25)

                if p_ema7 <= p_ema25 and c_ema7 > c_ema25:
                    events.append({
                        "id": f"{symbol}_{tf}_cross_up_{last[0]}",
                        "symbol": symbol,
                        "timeframe": tf,
                        "type": "EMA_CROSS_UP",
                        "value": "7/25",
                        "price": cl,
                        "timestamp": datetime.now().strftime("%H:%M:%S")
                    })
                elif p_ema7 >= p_ema25 and c_ema7 < c_ema25:
                    events.append({
                        "id": f"{symbol}_{tf}_cross_down_{last[0]}",
                        "symbol": symbol,
                        "timeframe": tf,
                        "type": "EMA_CROSS_DOWN",
                        "value": "7/25",
                        "price": cl,
                        "timestamp": datetime.now().strftime("%H:%M:%S")
                    })

                # 4. Pullback Continuation Strategy (G-R-G Pattern)
                c_ema21 = get_ema(closes, 21)
                if len(k) >= 3:
                    k1, k2, k3 = k[-3], k[-2], k[-1]
                    o1, c1, l1 = float(k1[1]), float(k1[4]), float(k1[3])
                    o2, c2, l2 = float(k2[1]), float(k2[4]), float(k2[3])
                    o3, c3, l3 = float(k3[1]), float(k3[4]), float(k3[3])
                    # Added Refinement: c3 > o2 (Solid closure above red open)
                    if c3 > c_ema21 and (c1 > o1) and (c2 < o2) and (c3 > o3) and (l2 > l1) and (c3 > o2):
                        events.append({
                            "id": f"{symbol}_{tf}_pullback_{k3[0]}",
                            "symbol": symbol, "timeframe": tf, "type": "PULLBACK_CONTINUATION",
                            "value": "G-R-G", "price": c3, "timestamp": datetime.now().strftime("%H:%M:%S")
                        })

                # 5. RSI Divergence & Profit Signals
                if limit >= 30:
                    def get_rsi(prices, period=14):
                        if len(prices) <= period: return 50
                        gains, losses = [], []
                        for i in range(1, len(prices)):
                            diff = prices[i] - prices[i-1]
                            gains.append(max(0, diff))
                            losses.append(max(0, -diff))
                        avg_gain = sum(gains[:period]) / period
                        avg_loss = sum(losses[:period]) / period
                        if avg_loss == 0: return 100
                        for i in range(period, len(gains)):
                            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
                        rs = avg_gain / avg_loss if avg_loss > 0 else 100
                        return 100 - (100 / (1 + rs))

                    rsi_curr = get_rsi(closes)
                    rsi_prev = get_rsi(closes[:-1])
                    
                    # Simple Bullish Divergence: Price Lower Low, RSI Higher Low
                    # Checking last 3-5 candles vs previous 10-15
                    if len(closes) >= 20:
                        p_min_now = min(closes[-3:])
                        p_min_old = min(closes[-15:-5])
                        rsi_min_now = get_rsi(closes[-10:]) # simplified relative
                        rsi_min_old = get_rsi(closes[-20:-10])
                        
                        if p_min_now < p_min_old and rsi_min_now > rsi_min_old and rsi_curr < 40:
                            events.append({
                                "id": f"{symbol}_{tf}_div_bull_{last[0]}",
                                "symbol": symbol, "timeframe": tf, "type": "PROFIT_SIGNAL",
                                "value": "BULL_DIV", "price": cl, "timestamp": datetime.now().strftime("%H:%M:%S")
                            })
                        
                        # Extreme Volume Imbalance (5x average)
                        if avg_qvol > 0 and curr_qvol > avg_qvol * 5.0:
                             events.append({
                                "id": f"{symbol}_{tf}_whale_buy_{last[0]}",
                                "symbol": symbol, "timeframe": tf, "type": "PROFIT_SIGNAL",
                                "value": "WHALE_ACCUM", "price": cl, "timestamp": datetime.now().strftime("%H:%M:%S")
                            })

    except Exception as e:
        # Silently fail for symbols that return errors (e.g. delisted or low data)
        # to avoid flooding the console/logs
        pass
    return events

# --- API Endpoints ---

@app.get("/")
async def root():
    path = resource_path("index.html")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/market-overview")
async def market_overview():
    all_tickers = list(manager.tickers.values())
    if not all_tickers: return JSONResponse(content={"gainers": [], "losers": [], "volume": []})
    
    gainers = sorted(all_tickers, key=lambda x: x['change_percent'], reverse=True)[:50]
    losers = sorted(all_tickers, key=lambda x: x['change_percent'])[:50]
    volume = sorted(all_tickers, key=lambda x: x['quoteVol'], reverse=True)[:100]
    
    return {
        "gainers": gainers,
        "losers": losers,
        "volume": volume,
        "all": all_tickers[:300]
    }

@app.get("/api/all-tickers")
async def all_tickers_endpoint():
    return list(manager.tickers.values())

@app.get("/api/pro-analytics")
async def pro_analytics():
    all_tickers = list(manager.tickers.values())
    vol_leaders = sorted(all_tickers, key=lambda x: x['quoteVol'], reverse=True)[:50]
    heatmap = []
    for t in vol_leaders:
        rsi = round(40 + (t['change_percent'] * 2), 1)
        rsi = max(20, min(80, rsi))
        heatmap.append({
            "symbol": t['symbol'].replace("USDT", ""),
            "rsi": rsi,
            "change": round(t['change_percent'], 2),
            "status": "Overbought" if rsi > 70 else "Oversold" if rsi < 30 else "Neutral"
        })
    return {
        "heatmap": heatmap,
        "whale_alerts": [] 
    }

@app.get("/api/screener")
async def screener_results(timeframe: str = "ALL"):
    results = manager.screener_results
    if timeframe and timeframe != "ALL":
        results = [r for r in results if r.get('timeframe') == timeframe]
    
    return {
        "updated": manager.last_screener_update,
        "count": len(results),
        "results": results
    }

@app.get("/api/momentum")
async def get_momentum():
    return {
        "count": len(manager.momentum_events),
        "events": manager.momentum_events
    }

@app.get("/api/candles")
async def get_candles(symbol: str, interval: str = "1h", limit: int = 100):
    """Proxy for Binance K-lines with robust error handling"""
    async with httpx.AsyncClient() as client:
        try:
            url = f"{REST_URL}/klines?symbol={symbol}&interval={interval}&limit={limit}"
            resp = await client.get(url, timeout=5.0)
            if resp.status_code != 200:
                print(f"Candle Fetch Fail: {symbol} {resp.status_code}")
                return []
            
            data = resp.json()
            if not isinstance(data, list):
                return []
                
            formatted = []
            for d in data:
                if not d or len(d) < 9: continue
                try:
                    # Binance returns: [timestamp, open, high, low, close, volume, close_time, quote_vol, trades, ...]
                    # We use: time[0], open[1], high[2], low[3], close[4], quote_vol[7], trades[8]
                    o = float(d[1]) if d[1] is not None else 0.0
                    h = float(d[2]) if d[2] is not None else 0.0
                    l = float(d[3]) if d[3] is not None else 0.0
                    c = float(d[4]) if d[4] is not None else 0.0
                    v = float(d[7]) if d[7] is not None else 0.0
                    t = int(d[8]) if d[8] is not None else 0
                    
                    # Essential validation: skip bars with 0 time or NaN prices
                    if not d[0] or o == 0: continue
                    
                    formatted.append({
                        "time": int(d[0]) / 1000,
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                        "trades": t
                    })
                except (ValueError, TypeError):
                    continue
            return formatted
        except Exception as e:
            print(f"Candle Fetch Error: {str(e)}")
            return []

@app.get("/api/order-book")
async def get_order_book(symbol: str, limit: int = 100):
    """Fetch order book depth from Binance"""
    async with httpx.AsyncClient() as client:
        try:
            url = f"{REST_URL}/depth?symbol={symbol}&limit={limit}"
            resp = await client.get(url, timeout=5.0)
            if resp.status_code != 200:
                return {"bids": [], "asks": []}
            
            data = resp.json()
            return {
                "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
                "asks": [[float(p), float(q)] for p, q in data.get("asks", [])],
                "lastUpdateId": data.get("lastUpdateId")
            }
        except Exception as e:
            print(f"Depth Fetch Error: {str(e)}")
            return {"bids": [], "asks": []}

@app.get("/api/settings")
async def get_settings():
    return telegram_config

@app.post("/api/settings")
async def update_settings(data: dict):
    global telegram_config
    telegram_config.update(data)
@app.get("/api/orderflow/{symbol}")
async def get_orderflow(symbol: str, interval: str = "15m", limit: int = 5):
    """
    Fetches aggregated trades and maps them to price levels within candles 
    to create professional Footprint (Order Flow) data.
    """
    async with httpx.AsyncClient() as client:
        try:
            # 1. Get Candle boundaries
            url_klines = f"{REST_URL}/klines?symbol={symbol}&interval={interval}&limit={limit}"
            resp_k = await client.get(url_klines, timeout=5.0)
            if resp_k.status_code != 200: return JSONResponse(status_code=400, content={"error": "Klines fail"})
            klines = resp_k.json()
            
            # 2. Fetch Aggregated Trades for EACH candle concurrently
            async def fetch_trades_for_candle(candle, is_latest=False):
                start, end = candle[0], candle[6]
                if is_latest:
                    # For the latest candle, fetch THE latest 1000 trades (ignoring startTime/endTime limit)
                    # This ensures we see the real-time "flow" 
                    url = f"{REST_URL}/aggTrades?symbol={symbol}&limit=1000"
                else:
                    url = f"{REST_URL}/aggTrades?symbol={symbol}&startTime={start}&endTime={end}&limit=1000"
                
                res = await client.get(url, timeout=5.0)
                if res.status_code == 200:
                    return res.json()
                return []

            trade_tasks = [fetch_trades_for_candle(k, idx == len(klines)-1) for idx, k in enumerate(klines)]
            results = await asyncio.gather(*trade_tasks)
            trades = [t for sublist in results for t in sublist]
            
            # 3. Aggregate trades into footprint
            print(f"--- Order Flow Request: {symbol} ({interval}) ---")
            print(f"Klines: {len(klines)} | Total Trades Found: {len(trades)}")
            
            footprint = {}
            for k in klines:
                t_start = k[0]
                footprint[t_start] = {
                    "open": float(k[1]), "high": float(k[2]), 
                    "low": float(k[3]), "close": float(k[4]),
                    "levels": {}
                }

            # Map trades to their specific candle based on time boundaries
            for t in trades:
                ts = t['T']
                candle_ts = None
                for k in klines:
                    # k[0] is start time, k[6] is end time
                    if k[0] <= ts <= k[6]:
                        candle_ts = k[0]
                        break
                
                if not candle_ts or candle_ts not in footprint: continue
                
                price = float(t['p'])
                qty = float(t['q'])
                is_buyer_maker = t['m'] # True = Sell (Hit Bid)
                
                # Round price based on magnitude to prevent excessive levels
                if price > 500:
                    level = round(price) 
                elif price > 10:
                    level = round(price * 10) / 10
                elif price > 1:
                    level = round(price, 2)
                elif price > 0.01:
                    level = round(price, 4)
                else:
                    level = round(price, 8) # High precision for small coins
                
                if level not in footprint[candle_ts]["levels"]:
                    footprint[candle_ts]["levels"][level] = {"bid": 0, "ask": 0}
                
                if is_buyer_maker:
                    footprint[candle_ts]["levels"][level]["bid"] += qty
                else:
                    footprint[candle_ts]["levels"][level]["ask"] += qty

            # 4. Post-process: POC, Delta, Limit Levels
            results = []
            for ts in sorted(footprint.keys()):
                f = footprint[ts]
                candle_delta = 0
                poc_price = 0
                max_vol = 0
                
                # Filter to only return levels with volume and within candle range
                valid_levels = []
                for price_level in sorted(f["levels"].keys(), reverse=True):
                    lvl = f["levels"][price_level]
                    total_vol = lvl["bid"] + lvl["ask"]
                    
                    if total_vol <= 0: continue
                    
                    level_delta = lvl["ask"] - lvl["bid"]
                    candle_delta += level_delta
                    
                    if total_vol > max_vol:
                        max_vol = total_vol
                        poc_price = price_level
                    
                    valid_levels.append({
                        "price": price_level,
                        "bid": round(lvl["bid"], 4),
                        "ask": round(lvl["ask"], 4),
                        "vol": round(total_vol, 4),
                        "delta": round(level_delta, 4)
                    })
                
                # Limit to top 24 levels around POC for better UI performance
                # This ensures the footprint isn't 1000 rows long
                # We sort by distance to POC and take the closest 24
                valid_levels.sort(key=lambda x: abs(x["price"] - poc_price))
                display_levels = sorted(valid_levels[:24], key=lambda x: x["price"], reverse=True)

                results.append({
                    "time": ts,
                    "open": f["open"], "high": f["high"], "low": f["low"], "close": f["close"],
                    "poc": poc_price,
                    "delta": round(candle_delta, 4),
                    "levels": display_levels
                })
                
            return results
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    return {"status": "success"}

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(websocket_listener())
    asyncio.create_task(volume_screener_loop())
    asyncio.create_task(momentum_scanner_loop())

if __name__ == "__main__":
    import uvicorn
    # if sys.stderr is None: sys.stderr = open(os.devnull, 'w')
    # if sys.stdout is None: sys.stdout = open(os.devnull, 'w')
        
    print("--- MAKE PROFITE SOFTWARE STARTING ---")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="error")
