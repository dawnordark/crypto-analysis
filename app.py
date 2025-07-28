#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import json
import math
import sqlite3
import requests
import threading
import queue
import logging
import traceback
import urllib3
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_from_directory, request
from binance.client import Client

# 禁用不必要的警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 设置日志级别
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, LOG_LEVEL))

# 降低第三方库日志级别
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("binance").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# 创建日志处理器
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(getattr(logging, LOG_LEVEL))
file_handler = logging.FileHandler('app.log')
file_handler.setLevel(getattr(logging, LOG_LEVEL))

# 日志格式
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

# 添加处理器
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)
logger.info(f"✅ 日志级别设置为: {LOG_LEVEL}")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='/static')

# Binance API 配置
API_KEY = os.environ.get('BINANCE_API_KEY', 'your_api_key_here')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'your_api_secret_here')
client = None

# 数据缓存
data_cache = {
    "last_updated": None,
    "daily_rising": [],
    "short_term_active": [],
    "all_cycle_rising": [],
    "analysis_time": 0,
    "next_analysis_time": None
}

current_data_cache = data_cache.copy()
oi_data_cache = {}
resistance_cache = {}
RESISTANCE_CACHE_EXPIRATION = 24 * 3600

# 使用队列进行线程间通信
analysis_queue = queue.Queue()
executor = ThreadPoolExecutor(max_workers=10)

PERIOD_MINUTES = {
    '5m': 5,
    '15m': 15,
    '30m': 30,
    '1h': 60,
    '2h': 120,
    '4h': 240,
    '6h': 360,
    '12h': 720,
    '1d': 1440
}

RESISTANCE_INTERVALS = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', 
                        '1d', '3d', '1w', '1M']
ALL_PERIODS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d', '3d', '1w', '1M']

def init_db():
    try:
        conn = sqlite3.connect('data.db')
        c = conn.cursor()
        
        # 创建表
        c.execute('''CREATE TABLE IF NOT EXISTS crypto_data
                    (id INTEGER PRIMARY KEY, 
                    last_updated TEXT,
                    daily_rising TEXT,
                    short_term_active TEXT,
                    all_cycle_rising TEXT,
                    analysis_time REAL,
                    next_analysis_time TEXT,
                    resistance_data TEXT)''')
        
        # 检查表结构
        c.execute("PRAGMA table_info(crypto_data)")
        columns = [col[1] for col in c.fetchall()]
        if 'next_analysis_time' not in columns:
            c.execute("ALTER TABLE crypto_data ADD COLUMN next_analysis_time TEXT")
        
        conn.commit()
        logger.info("✅ 数据库初始化完成")
        return True
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {str(e)}")
        logger.error(traceback.format_exc())
        try:
            os.remove('data.db')
            return init_db()
        except Exception as e2:
            logger.critical(f"🔥 无法修复数据库: {str(e2)}")
            return False
    finally:
        if conn:
            conn.close()

def save_to_db(data):
    try:
        conn = sqlite3.connect('data.db')
        c = conn.cursor()
        
        resistance_json = json.dumps(resistance_cache)
        c.execute(
            "INSERT INTO crypto_data VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)",
            (data['last_updated'], 
             json.dumps(data['daily_rising']),
             json.dumps(data['short_term_active']),
             json.dumps(data['all_cycle_rising']), 
             data['analysis_time'],
             data.get('next_analysis_time', None),
             resistance_json))
        conn.commit()
        logger.info("💾 数据保存到数据库成功")
        return True
    except Exception as e:
        logger.error(f"❌ 保存数据到数据库失败: {str(e)}")
        return False
    finally:
        if conn:
            conn.close()

def get_last_valid_data():
    try:
        conn = sqlite3.connect('data.db')
        c = conn.cursor()
        
        c.execute("SELECT * FROM crypto_data ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        
        if not row:
            return None
            
        resistance_data = json.loads(row[7]) if row[7] else {}
        for symbol, data in resistance_data.items():
            resistance_cache[symbol] = data

        return {
            'last_updated': row[1],
            'daily_rising': json.loads(row[2]) if row[2] else [],
            'short_term_active': json.loads(row[3]) if row[3] else [],
            'all_cycle_rising': json.loads(row[4]) if row[4] else [],
            'analysis_time': row[5] or 0,
            'next_analysis_time': row[6]
        }
    except Exception as e:
        logger.error(f"❌ 获取最后有效数据失败: {str(e)}")
        return None
    finally:
        if conn:
            conn.close()

def init_client():
    global client
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            client = Client(
                api_key=API_KEY, 
                api_secret=API_SECRET,
                requests_params={'timeout': 30}
            )
            
            # 测试连接
            server_time = client.get_server_time()
            logger.info(f"✅ Binance客户端初始化成功")
            return True
        except Exception as e:
            logger.error(f"❌ 初始化Binance客户端失败: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    logger.critical("🔥 无法初始化Binance客户端")
    return False

def get_next_update_time(period):
    now = datetime.now(timezone.utc)
    minutes = PERIOD_MINUTES.get(period, 5)
    
    if period.endswith('m'):
        period_minutes = int(period[:-1])
        current_minute = now.minute
        current_period_minute = (current_minute // period_minutes) * period_minutes
        next_update = now.replace(minute=current_period_minute, second=0, microsecond=0) + timedelta(minutes=period_minutes)
    elif period.endswith('h'):
        period_hours = int(period[:-1])
        current_hour = now.hour
        current_period_hour = (current_hour // period_hours) * period_hours
        next_update = now.replace(hour=current_period_hour, minute=0, second=0, microsecond=0) + timedelta(hours=period_hours)
    else:  # 1d
        next_update = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    return next_update

def get_open_interest(symbol, period, use_cache=True):
    try:
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            return {'series': [], 'timestamps': []}

        current_time = datetime.now(timezone.utc)
        cache_key = f"{symbol}_{period}"
        
        if use_cache and oi_data_cache.get(cache_key):
            cached_data = oi_data_cache[cache_key]
            if 'next_update' in cached_data and cached_data['next_update'] > current_time:
                return cached_data['data']

        # 获取数据
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}
        response = requests.get(url, params=params, timeout=15)
        
        if response.status_code != 200:
            return {'series': [], 'timestamps': []}

        data = response.json()
        if not isinstance(data, list) or len(data) == 0:
            return {'series': [], 'timestamps': []}
            
        data.sort(key=lambda x: x['timestamp'])
        oi_series = [float(item['sumOpenInterest']) for item in data]
        timestamps = [item['timestamp'] for item in data]

        if len(oi_series) < 5:
            return {'series': [], 'timestamps': []}

        oi_data = {
            'series': oi_series, 
            'timestamps': timestamps
        }
        
        next_update = get_next_update_time(period)
        oi_data_cache[cache_key] = {
            'data': oi_data,
            'next_update': next_update
        }

        return oi_data
    except Exception as e:
        return {'series': [], 'timestamps': []}

def is_latest_highest(oi_data):
    if len(oi_data) < 30:
        return False

    latest_value = oi_data[-1]
    prev_data = oi_data[-30:-1]
    
    return latest_value > max(prev_data) if prev_data else False

def calculate_resistance_levels(symbol):
    try:
        now = time.time()
        if symbol in resistance_cache:
            cache_data = resistance_cache[symbol]
            if cache_data['expiration'] > now:
                return cache_data['levels']

        levels = {}
        
        # 确保客户端初始化
        if client is None and not init_client():
            return {}

        for interval in RESISTANCE_INTERVALS:
            try:
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=100)
                
                if not klines or len(klines) < 10:
                    continue

                high_prices = [float(k[2]) for k in klines]
                low_prices = [float(k[3]) for k in klines]
                recent_high = max(high_prices[-10:])
                recent_low = min(low_prices[-10:])
                lookback = min(30, len(high_prices))
                recent_max = max(high_prices[-lookback:])
                recent_min = min(low_prices[-lookback:])

                if recent_max <= recent_min:
                    continue

                fib_levels = {
                    '0.236': recent_max - (recent_max - recent_min) * 0.236,
                    '0.382': recent_max - (recent_max - recent_min) * 0.382,
                    '0.5': recent_max - (recent_max - recent_min) * 0.5,
                    '0.618': recent_max - (recent_max - recent_min) * 0.618,
                    '0.786': recent_max - (recent_max - recent_min) * 0.786,
                    '1.0': recent_max
                }

                price_levels = [recent_high, recent_low]
                price_levels.extend(fib_levels.values())
                base = 10 ** (math.floor(math.log10(recent_high)) - 1)
                integer_level = round(recent_high / base) * base
                price_levels.append(integer_level)

                price_levels = sorted(set(price_levels), reverse=True)
                resistance = [p for p in price_levels if p > recent_min and p <= recent_max]
                support = [p for p in price_levels if p < recent_min or p > recent_max]

                levels[interval] = {
                    'resistance': resistance[:3],
                    'support': support[:3]
                }
            except Exception:
                continue

        resistance_cache[symbol] = {
            'levels': levels,
            'expiration': now + RESISTANCE_CACHE_EXPIRATION
        }
        return levels
    except Exception:
        return {}

def analyze_symbol(symbol):
    try:
        symbol_result = {
            'symbol': symbol,
            'daily_rising': None,
            'short_term_active': None,
            'all_cycle_rising': None,
            'rising_periods': [],
            'period_status': {p: False for p in ALL_PERIODS},
            'period_count': 0
        }

        # 日线分析
        daily_oi = get_open_interest(symbol, '1d')
        daily_series = daily_oi.get('series', [])
        
        if len(daily_series) >= 30 and is_latest_highest(daily_series):
            daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
            symbol_result['daily_rising'] = {
                'symbol': symbol,
                'oi': daily_series[-1],
                'change': round(daily_change, 2),
                'period_count': 1
            }
            symbol_result['period_status']['1d'] = True
            symbol_result['period_count'] = 1
            symbol_result['rising_periods'].append('1d')

            # 全周期分析
            all_intervals_up = True
            for period in ALL_PERIODS:
                if period == '1d':
                    continue
                    
                oi_data = get_open_interest(symbol, period)
                oi_series = oi_data.get('series', [])
                status = len(oi_series) >= 30 and is_latest_highest(oi_series)
                
                symbol_result['period_status'][period] = status
                if status:
                    symbol_result['rising_periods'].append(period)
                    symbol_result['period_count'] += 1
                else:
                    all_intervals_up = False

            if all_intervals_up:
                symbol_result['all_cycle_rising'] = {
                    'symbol': symbol,
                    'oi': daily_series[-1],
                    'change': round(daily_change, 2),
                    'periods': symbol_result['rising_periods'],
                    'period_count': symbol_result['period_count']
                }

        # 短期活跃度分析
        min5_oi = get_open_interest(symbol, '5m')
        min5_series = min5_oi.get('series', [])
        
        if len(min5_series) >= 30 and len(daily_series) >= 30:
            min5_max = max(min5_series[-30:])
            daily_avg = sum(daily_series[-30:]) / 30
            
            if min5_max > 0 and daily_avg > 0:
                ratio = min5_max / daily_avg
                if ratio > 1.5:
                    symbol_result['short_term_active'] = {
                        'symbol': symbol,
                        'oi': min5_series[-1],
                        'ratio': round(ratio, 2),
                        'period_count': symbol_result['period_count']
                    }

        return symbol_result
    except Exception:
        return {
            'symbol': symbol,
            'period_status': {p: False for p in ALL_PERIODS},
            'period_count': 0
        }

def analyze_trends():
    start_time = time.time()
    symbols = get_high_volume_symbols()
    
    if not symbols:
        return data_cache

    daily_rising = []
    short_term_active = []
    all_cycle_rising = []

    futures = {
        executor.submit(analyze_symbol, symbol): symbol
        for symbol in symbols
    }
    processed = 0
    total_symbols = len(symbols)

    for future in as_completed(futures):
        processed += 1
        symbol = futures[future]
        try:
            result = future.result()
            if result.get('daily_rising'):
                daily_rising.append(result['daily_rising'])
            if result.get('short_term_active'):
                short_term_active.append(result['short_term_active'])
            if result.get('all_cycle_rising'):
                all_cycle_rising.append(result['all_cycle_rising'])
        except Exception:
            continue

    daily_rising.sort(key=lambda x: (x.get('period_count', 0), x.get('change', 0)), reverse=True)
    short_term_active.sort(key=lambda x: x.get('ratio', 0), reverse=True)
    all_cycle_rising.sort(key=lambda x: (x.get('period_count', 0), x.get('change', 0)), reverse=True)

    analysis_time = time.time() - start_time
    return {
        'daily_rising': daily_rising,
        'short_term_active': short_term_active,
        'all_cycle_rising': all_cycle_rising,
        'analysis_time': analysis_time
    }

def get_high_volume_symbols():
    if client is None and not init_client():
        return []

    try:
        tickers = client.futures_ticker()
        return [
            t['symbol'] for t in tickers 
            if float(t.get('quoteVolume', 0)) > 10000000
            and t.get('symbol', '').endswith('USDT')
        ]
    except Exception:
        return []

def analysis_worker():
    global data_cache, current_data_cache
    init_db()
    initial_data = get_last_valid_data()
    if initial_data:
        data_cache = initial_data
        current_data_cache = data_cache.copy()

    while True:
        try:
            task = analysis_queue.get()
            if task == "STOP":
                break

            analysis_start = datetime.now(timezone.utc)
            backup_cache = data_cache.copy()

            try:
                result = analyze_trends()
                next_analysis_time = get_next_update_time('5m')
                
                new_data = {
                    "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "daily_rising": result['daily_rising'],
                    "short_term_active": result['short_term_active'],
                    "all_cycle_rising": result['all_cycle_rising'],
                    "analysis_time": result['analysis_time'],
                    "next_analysis_time": next_analysis_time.strftime("%Y-%m-%d %H:%M:%S")
                }
                
                data_cache = new_data
                save_to_db(new_data)
                current_data_cache = new_data.copy()
            except Exception:
                data_cache = backup_cache
        except Exception:
            continue
        analysis_queue.task_done()

def schedule_analysis():
    now = datetime.now(timezone.utc)
    next_time = get_next_update_time('5m')
    initial_wait = (next_time - now).total_seconds()
    time.sleep(max(0, initial_wait))

    while True:
        analysis_queue.put("ANALYZE")
        analysis_queue.join()
        now = datetime.now(timezone.utc)
        next_time = get_next_update_time('5m')
        wait_time = (next_time - now).total_seconds()
        time.sleep(max(60, wait_time))

# API路由
@app.route('/')
def index():
    try:
        return send_from_directory(app.static_folder, 'index.html')
    except Exception:
        return "Internal Server Error", 500

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/api/data', methods=['GET'])
def get_data():
    global current_data_cache
    data = {
        'last_updated': current_data_cache.get('last_updated', ""),
        'daily_rising': current_data_cache.get('daily_rising', []),
        'short_term_active': current_data_cache.get('short_term_active', []),
        'all_cycle_rising': current_data_cache.get('all_cycle_rising', []),
        'analysis_time': current_data_cache.get('analysis_time', 0),
        'next_analysis_time': current_data_cache.get('next_analysis_time', "")
    }
    return jsonify(data)

@app.route('/api/resistance_levels/<symbol>', methods=['GET'])
def get_resistance_levels(symbol):
    try:
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            return jsonify({'error': 'Invalid symbol format'}), 400
            
        levels = calculate_resistance_levels(symbol)
        return jsonify(levels)
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/oi_chart/<symbol>/<period>', methods=['GET'])
def get_oi_chart_data(symbol, period):
    try:
        if period not in PERIOD_MINUTES:
            return jsonify({'error': 'Unsupported period'}), 400
            
        oi_data = get_open_interest(symbol, period)
        return jsonify({
            'data': oi_data.get('series', []),
            'timestamps': oi_data.get('timestamps', [])
        })
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    try:
        conn = sqlite3.connect('data.db')
        conn.close()
        binance_status = 'ok' if client and client.get_server_time() else 'error'
        return jsonify({
            'status': 'healthy',
            'binance': binance_status
        })
    except Exception:
        return jsonify({'status': 'unhealthy'}), 500

def start_background_threads():
    if not init_client():
        return False
    
    worker_thread = threading.Thread(target=analysis_worker, name="AnalysisWorker", daemon=True)
    worker_thread.start()
    
    scheduler_thread = threading.Thread(target=schedule_analysis, name="AnalysisScheduler", daemon=True)
    scheduler_thread.start()
    
    return True

if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 9600))
    if start_background_threads():
        app.run(host='0.0.0.0', port=PORT, debug=False)
