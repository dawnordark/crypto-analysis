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

# 设置日志级别 (默认改为INFO)
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

# 获取根记录器并设置级别
root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, LOG_LEVEL))

# 降低第三方库日志级别
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("binance").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# 创建控制台处理器
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(getattr(logging, LOG_LEVEL))

# 创建文件处理器
file_handler = logging.FileHandler('app.log')
file_handler.setLevel(getattr(logging, LOG_LEVEL))

# 创建格式器
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

# ==================== 数据缓存 ====================
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

RESISTANCE_INTERVALS = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
ALL_PERIODS = ['5m', '15m', '30m', '1h', '2h', '4极速赛车开奖网
def init_db():
    try:
        logger.debug("🛠️ 开始初始化数据库...")
        conn = sqlite3.connect('data.db')
        c = conn.cursor()

        # 检查表是否存在
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_data'")
        table_exists = c.fetchone()

        if not table_exists:
            logger.info("🛠️ 创建数据库表...")
            c.execute('''CREATE TABLE crypto_data
                        (id INTEGER PRIMARY KEY, 
                        last_updated TEXT,
                        daily_rising TEXT,
                        short_term_active TEXT,
                        all_cycle_rising TEXT,
                        analysis_time REAL,
                        next_analysis_time TEXT,
                        resistance_data TEXT)''')
            conn.commit()
            logger.info("✅ 数据库表创建成功")
        else:
            # 检查表结构是否最新
            c.execute("PRAGMA table_info(crypto_data)")
            columns = [col[1] for col in c.fetchall()]
            if 'next_analysis_time' not in columns:
                logger.info("🛠️ 更新数据库表结构...")
                c.execute("ALTER TABLE crypto_data ADD COLUMN next_analysis_time TEXT")
                conn.commit()
                logger.info("✅ 数据库表结构更新成功")
            else:
                logger.info("✅ 数据库表已存在且结构最新")

        conn.close()
        logger.debug("🛠️ 数据库初始化完成")
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {str(e)}")
        logger.error(traceback.format_exc())
        try:
            logger.warning("🗑️ 尝试删除损坏的数据库文件...")
            os.remove('data.db')
            logger.warning("🗑️ 删除损坏的数据库文件，将创建新数据库")
            init_db()
        except Exception as e2:
            logger.critical(f"🔥 无法修复数据库: {str(e2)}")
            logger.critical(traceback.format_exc())

def save_to_db(data):
    try:
        logger.debug("💾 开始保存数据到数据库...")
        conn = sqlite3.connect('极速赛车开奖网
        c = conn.cursor()

        # 确保表存在
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_data'")
        if not c.fetchone():
            logger.warning("⚠️ 数据库表不存在，正在创建...")
            init_db()

        resistance_json = json.dumps(resistance_cache)

        # 插入或更新数据
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
        conn.close()
        logger.info("💾 数据保存到数据库成功")
    except Exception as e:
        logger.error(f"❌ 保存数据到数据库失败: {str(e)}")
        logger.error(traceback.format_exc())

def get_last_valid_data():
    try:
        logger.debug("🔍 尝试获取最后有效数据...")
        conn = sqlite3.connect('data.db')
        c = conn.cursor()

        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_data'")
        if not c.fetchone():
            logger.warning("⚠️ 数据库表不存在")
            return None

        c.execute("SELECT * FROM crypto_data ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        conn.close()

        if row:
            logger.debug(f"🔍 找到最后有效数据: ID={row[0]}, 时间={row[1]}")
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
        logger.debug("🔍 数据库中没有有效数据")
        return None
    except Exception as e:
        logger.error(f"❌ 获取最后有效数据失败: {str(e)}")
        logger.error(traceback.format_exc())
        return None

def init_client():
    global client
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            logger.debug(f"🔧 尝试初始化Binance客户端 (第{attempt+1}次)...")
            client_params = {
                'api_key': API_KEY, 
                'api_secret': API_SECRET,
                'requests_params': {'timeout': 30}
            }

            client = Client(**client_params)
            
            # 测试连接
            server_time = client.get_server_time()
            logger.info(f"✅ Binance客户端初始化成功，服务器时间: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
            return True
        except Exception as e:
            logger.error(f"❌ 初始化Binance客户端失败: {str(e)}")
            logger.error(traceback.format_exc())
            if attempt < max_retries - 1:
                logger.info(f"🔄 {retry_delay}秒后重试初始化客户端...")
                time.sleep(retry_delay)
            else:
                logger.critical("🔥 无法初始化Binance客户端，已达到最大重试次数")
                return False
    return False

def get_next_update_time(period):
    minutes = PERIOD_MINUTES.get(period, 5)
    now = datetime.now(timezone.utc)

    if period.endswith('m'):
        period_minutes = int(period[:-1])
        current_minute = now.minute
        current_period_minute = (current_minute // period_minutes) * period_minutes
        current_period_start = now.replace(minute=current_period_minute,
                                           second=0,
                                           microsecond=0)
    elif period.endswith('h'):
        period_hours = int(period[:-1])
        current_hour = now.hour
        current_period_hour = (current_hour // period_hours) * period_hours
        current_period_start = now.replace(hour=current_period_hour,
                                           minute=0,
                                           second=0,
                                           microsecond=0)
    else:  # 1d
        current_period_start = now.replace(hour=0,
                                           minute=0,
                                           second=0,
                                           microsecond=0)

    next_update = current_period_start + timedelta(minutes=minutes)

    if next_update < now:
        next_update += timedelta(minutes=minutes)

    return next_update

def get_open_interest(symbol, period, use_cache=True):
    try:
        # 更宽松的币种格式验证 (允许数字和更短的代码)
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

        if period not in PERIOD_MINUTES:
            logger.warning(f"⚠️ 不支持的周期: {period}, 使用默认5m")
            period = '5m'

        current_time = datetime.now(timezone.utc)

        cache_key = f"{symbol}_{period}"
        if use_cache and oi_data_cache.get(cache_key):
            cached_data = oi_data_cache[cache_key]
            if 'next_update' in cached_data and cached_data['next_update'] > current_time:
                logger.debug(f"📈 使用缓存数据: {symbol} {period}")
                return {
                    'series': cached_data['data']['series'].copy(),
                    'timestamps': cached_data['data']['timestamps'].copy(),
                    'cache_time': cached_data['data']['cache_time']
                }

        logger.info(f"📡 请求持仓量数据: symbol={symbol}, period={period}")
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}

        response = requests.get(url, params=params, timeout=15)
        logger.debug(f"📡 响应状态: {response.status_code}")

        if response.status_code != 200:
            logger.error(f"❌ 获取{symbol}的{period}持仓量失败: HTTP {response.status_code} - {response.text}")
            return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

        data = response.json()
        logger.debug(f"📡 响应数据: {data[:1]}...")

        if not isinstance(data, list):
            logger.error(f"❌ 无效的持仓量数据格式: {symbol} {period} - 响应: {data}")
            return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

        if len(data) == 0:
            logger.warning(f"⚠️ {symbol}的{period}持仓量数据为空")
            return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

        data.sort(key=lambda x: x['timestamp'])
        oi_series = [float(item['sumOpenInterest']) for item in data]
        timestamps = [item['timestamp'] for item in data]
        cache_time = datetime.now(timezone.utc).isoformat()

        if len(oi_series) < 5:
            logger.warning(f"⚠️ {symbol}的{period}持仓量数据不足: 只有{len(oi_series)}个点")
            return {'series': [], 'timestamps': [], 'cache_time': cache_time}

        oi_data = {
            'series': oi_series, 
            'timestamps': timestamps,
            'cache_time': cache_time
        }
        next_update = get_next_update_time(period)

        oi_data_cache[cache_key] = {
            'data': oi_data,
            'next_update': next_update
        }

        logger.info(f"📈 获取新数据: {symbol} {period} ({len(oi_series)}点)")
        return oi_data
    except Exception as e:
        logger.error(f"❌ 获取{symbol}的{period}持仓量失败: {str(e)}")
        logger.error(traceback.format_exc())
        return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

def is_latest_highest(oi_data):
    if len(oi_data) < 30:
        logger.debug("持仓量数据不足30个点")
        return False

    latest_value = oi_data[-1]
    prev_data = oi_data[-30:-1]

    if not prev_data:
        logger.debug("无历史数据用于比较")
        return False

    result = latest_value > max(prev_data)
    logger.debug(f"持仓量创新高检查: 最新值={latest_value}, 历史最大值={max(prev_data)}, 结果={result}")
    return result

def calculate_resistance_levels(symbol):
    try:
        logger.debug(f"📊 计算阻力位: {symbol}")
        
        # 确保客户端已初始化
        if client is None:
            logger.warning("⚠️ Binance客户端未初始化，尝试重新初始化")
            if not init_client():
                logger.error("❌ 无法初始化Binance客户端，无法计算阻力位")
                return {}
        
        now = time.time()
        if symbol in resistance_cache:
            cache_data = resistance_cache[symbol]
            if cache_data['expiration'] > now:
                logger.debug(f"📊 使用缓存的阻力位数据: {symbol}")
                return cache_data['levels']

        levels = {}
        logger.debug(f"📊 开始计算{symbol}的阻力位")

        for interval in RESISTANCE_INTERVALS:
            try:
                logger.debug(f"📊 获取K线数据: {symbol} {interval}")
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=100)
                
                if not klines or len(klines) < 10:
                    logger.warning(f"⚠️ {symbol}在{interval}的K线数据不足")
                    continue

                high_prices = [float(k[2]) for k in klines]
                low_prices = [float(k[3]) for k in klines]
                recent_high = max(high_prices[-10:])
                recent_low = min(low_prices[-10:])

                lookback = min(30, len(high_prices))
                recent_max = max(high_prices[-lookback:])
                recent_min = min(low_prices[-lookback:])

                if recent_max <= recent_min:
                    logger.warning(f"⚠️ {symbol}在{interval}的最近高点和低点无效")
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
                
                logger.debug(f"📊 {symbol}在{interval}的阻力位计算完成")
            except Exception as e:
                logger.error(f"计算{symbol}在{interval}的阻力位失败: {str(e)}")
                logger.error(traceback.format_exc())
                levels[interval] = {
                    'resistance': [],
                    'support': []
                }

        resistance_cache[symbol] = {
            'levels': levels,
            'expiration': now + RESISTANCE_CACHE_EXPIRATION
        }
        logger.info(f"📊 {symbol}的阻力位计算完成")
        return levels
    except Exception as e:
        logger.error(f"计算{symbol}的阻力位失败: {str(e)}")
        logger.error(traceback.format_exc())
        return {}

def analyze_symbol(symbol):
    try:
        logger.info(f"🔍 开始分析币种: {symbol}")
        symbol_result = {
            'symbol': symbol,
            'daily_rising': None,
            'short_term_active': None,
            'all_cycle_rising': None,
            'rising_periods': [],
            'period_status': {p: False for p in ALL_PERIODS},
            'period_count': 0,
            'oi_data': {}
        }

        # 1. 获取日线持仓量数据
        logger.debug(f"📊 获取日线持仓量: {symbol}")
        daily_oi = get_open_interest(symbol, '1d', use_cache=True)
        symbol_result['oi_data']['1d'] = daily_oi
        daily_series = daily_oi['series'] if daily_oi and 'series' in daily_oi else []
        
        logger.debug(f"📊 日线持仓量数据长度: {len(daily_series)}")

        # 2. 检查日线上涨条件
        if len(daily_series) >= 30:
            daily_up = is_latest_highest(daily_series)
            logger极速赛车开奖网
            if daily_up:
                daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
                logger.debug(f"📊 日线变化: {daily_change:.2f}%")
                
                symbol_result['daily_rising'] = {
                    'symbol': symbol,
                    'oi': daily_series[-1],
                    'change': round(daily_change, 2),
                    'period_count': 1
                }
                symbol_result['rising_periods'].append('1d')
                symbol_result['period_status']['1d'] = True
                symbol_result['period_count'] = 1

                # 3. 全周期分析
                logger.debug(f"📊 开始全周期分析: {symbol}")
                all_intervals_up = True
                for period in ALL_PERIODS:
                    if period == '1d':
                        continue

                    logger.debug(f"📊 分析周期: {period}")
                    oi_data = get_open_interest(symbol, period, use_cache=True)
                    symbol_result['oi_data'][period] = oi_data
                    oi_series = oi_data['series'] if oi_data and 'series' in oi_data else []
                    
                    if len(oi_series) < 30:
                        logger.debug(f"📊 数据不足: {symbol} {period}只有{len(oi_series)}个点")
                        status = False
                    else:
                        status = is_latest_highest(oi_series)
                        
                    symbol_result['period_status'][period] = status
                    logger.debug(f"📊 周期状态: {period} = {status}")

                    if status:
                        symbol_result['rising_periods'].append(period)
                        symbol_result['period_count'] += 1
                    else:
                        all_intervals_up = False

                if all_intervals_up:
                    logger.debug(f"📊 全周期上涨: {symbol}")
                    symbol_result['all_cycle_rising'] = {
                        'symbol': symbol,
                        'oi': daily_series[-1],
                        'change': round(daily_change, 2),
                        'periods': symbol_result['rising_periods'],
                        'period_status': symbol_result['period_status'],
                        'period_count': symbol_result['period_count']
                    }

        # 4. 短期活跃度分析
        logger.debug(f"📊 分析短期活跃度: {symbol}")
        min5_oi = get_open_interest(symbol, '5m', use_cache=True)
        
        # 添加空数据检查
        if not min5_oi or not min5_oi.get('series'):
            logger.warning(f"⚠️ {symbol} 5m持仓量数据为空，跳过短期活跃度分析")
            min5_series = []
        else:
            min5_series = min5_oi['series']
            symbol_result['oi_data']['5m'] = min5_oi

        if len(min5_series) >= 30 and len(daily_series) >= 30:
            min5_max = max(min5_series[-30:])
            daily_avg = sum(daily_series[-30:]) / 30
            logger.debug(f"📊 短期最大值: {min5_max}, 日均值: {daily_avg}")

            if min5_max > 0 and daily_avg > 0:
                ratio = min5_max / daily_avg
                logger.debug(f"📊 短期活跃比率: {ratio:.2f}")
                
                if ratio > 1.5:
                    logger.debug(f"📊 短期活跃: {symbol} 比率={ratio:.2f}")
                    symbol_result['short_term_active'] = {
                        'symbol': symbol,
                        'oi': min5_series[-1],
                        'ratio': round(ratio, 2),
                        'period_status': symbol_result['period_status'],
                        'period_count': symbol_result['period_count']
                    }

        if symbol_result['daily_rising']:
            symbol_result['daily_rising']['period_status'] = symbol_result['period_status']
            symbol_result['daily_rising']['period_count'] = symbol_result['period_count']

        logger.info(f"✅ 完成分析币种: {symbol}")
        return symbol_result
    except Exception as e:
        logger.error(f"❌ 处理{symbol}时出错: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            'symbol': symbol,
            'period_status': {p: False for p in ALL_PERIODS},
            'period_count': 0,
            'oi_data': {}
        }

def analyze_trends():
    start_time = time.time()
    logger.info("🔍 开始分析币种趋势...")
    symbols = get_high_volume_symbols()
    
    if not symbols:
        logger.warning("⚠️ 没有找到高交易量币种")
        return data_cache

    logger.info(f"🔍 开始分析 {len(symbols)} 个币种")

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
        except Exception as e:
            logger.error(f"❌ 处理{symbol}时出错: {str(e)}")

        if processed % max(1, total_symbols // 10) == 0 or processed == total_symbols:
            logger.info(f"⏳ 分析进度: {processed}/{total_symbols} ({int(processed/total_symbols*100)}%)")

    daily_rising.sort(key=lambda x: (x.get('period_count', 0), x.get('change', 0)), reverse=True)
    short_term_active.sort(key=lambda x: (极速赛车开奖网
    all_cycle_rising.sort(key=lambda x: (x.get('period_count', 0), x.get('change', 0)), reverse=True)

    logger.info(f"📊 分析结果: 日线上涨 {len(daily_rising)}个, 短期活跃 {len(short_term_active)}个, 全部周期上涨 {len(all_cycle_rising)}个")
    analysis_time = time.time() - start_time
    logger.info(f"✅ 分析完成: 用时{analysis_time:.2f}秒")

    return {
        'daily_rising': daily_rising,
        'short_term_active': short_term_active,
        'all_cycle_rising': all_cycle_rising,
        'analysis_time': analysis_time
    }

def get_high_volume_symbols():
    # 确保客户端已初始化
    if client is None:
        logger.warning("⚠️ Binance客户端未初始化，尝试重新初始化")
        if not init_client():
            logger.error("❌ 无法连接API")
            return []

    try:
        logger.info("📊 获取高交易量币种...")
        tickers = client.futures_ticker()
        filtered = [
            t for t in tickers if float(t.get('quoteVolume', 0)) > 10000000
            and t.get('symbol', '').endswith('USDT')
        ]
        logger.info(f"📊 找到 {len(filtered)} 个高交易量币种")
        logger.debug(f"📊 前5个币种: {[t['symbol'] for t in filtered[:5]]}")
        return [t['symbol'] for t in filtered]
    except Exception as e:
        logger.error(f"❌ 获取高交易量币种失败: {str(e)}")
        logger.error(traceback.format_exc())
        return []

def analysis_worker():
    global data_cache, current_data_cache
    logger.info("🔧 数据分析线程启动")
    init_db()

    initial_data = get_last_valid_data()
    if initial_data:
        logger.info("🔁 加载历史数据")
        data_cache = initial_data
        current_data_cache = data_cache.copy()
        logger.debug(f"🔁 加载的数据: {json.dumps(initial_data, indent=2)}")
    else:
        logger.info("🆕 没有历史数据，将进行首次分析")

    while True:
        try:
            task = analysis_queue.get()
            if task == "STOP":
                logger.info("🛑 收到停止信号，结束分析线程")
                break

            analysis_start = datetime.now(timezone.utc)
            logger.info(f"⏱️ 开始更新数据 ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')})...")

            backup_cache = data_cache.copy()
            current_backup = current_data_cache.copy()

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
                
                logger.debug(f"📊 分析结果: {json.dumps(new_data, indent=2)}")

                data_cache = new_data
                save_to_db(new_data)
                current_data_cache = new_data.copy()
                logger.info(f"✅ 数据更新成功")
            except Exception as e:
                logger.error(f"❌ 分析过程中出错: {str(e)}")
                logger.error(traceback.format_exc())
                data_cache = backup_cache
                current_data_cache = current_backup
                logger.info("🔄 恢复历史数据")

            analysis_end = datetime.now(timezone.utc)
            analysis_duration = (analysis_end - analysis_start).total_seconds()
            logger.info(f"⏱️ 分析耗时: {analysis_duration:.2f}秒")
            
            # 记录下一次分析时间
            next_time = get_next_update_time('5m')
            wait_seconds = (next_time - analysis_end).total_seconds()
            logger.info(f"⏳ 下次分析将在 {wait_seconds:.1f} 秒后 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})")
            
            logger.info("=" * 50)

        except Exception as e:
            logger.error(f"❌ 分析失败: {str(e)}")
            logger.error(traceback.format_exc())
        analysis_queue.task_done()

def schedule_analysis():
    logger.info("⏰ 定时分析调度器启动")
    now = datetime.now(timezone.utc)
    current_minute = now.minute
    next_minute = ((current_minute // 5) + 1) * 5
    if next_minute >= 60:
        next_minute = 0
        next_time = now.replace(hour=now.hour + 1, minute=next_minute, second=0, microsecond=0)
    else:
        next_time = now.replace(minute=next_minute, second=0, microsecond=0)

    initial_wait = (next_time - now).total_seconds()
    logger.info(f"⏳ 首次分析将在 {initial_wait:.1f} 秒后开始 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})...")
    time.sleep(initial_wait)

    while True:
        analysis_start = datetime.now(timezone.utc)
        logger.info(f"🔔 触发定时分析任务 ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')})")
        analysis_queue.put("ANALYZE")
        analysis_queue.join()

        analysis_duration = (datetime.now(timezone.utc) - analysis_start).total_seconds()
        now = datetime.now(timezone.utc)
        current_minute = now.minute
        next_minute = ((current_minute // 5) + 1) * 5
        if next_minute >= 60:
            next_minute = 0
            next_time = now.replace(hour=now.hour + 1, minute=next_minute, second=0, microsecond=0)
        else:
            next_time = now.replace(minute=next_minute, second=0, microsecond=0)

        wait_time = (next_time - now).total_seconds()

        if wait_time < 0:
            wait_time = 0
        elif analysis_duration > 240:
            wait_time = 0
            logger.warning("⚠️ 分析耗时过长，立即开始下一次分析")
        elif wait_time > 300:
            adjusted_wait = max(60, wait_time - 120)
            logger.info(f"⏳ 调整等待时间: {wait_time:.1f}秒 -> {adjusted_wait:.1f}秒")
            wait_time = adjusted_wait

        logger.info(f"⏳ 下次分析将在 {wait_time:.1f} 秒后 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})")
        time.sleep(wait_time)

# API路由
@app.route('/')
def index():
    try:
        logger.debug("🌐 处理首页请求")
        static_path = app.static_folder
        if static_path is None:
            return "静态文件路径未配置", 500

        index_path = os.path.join(static_path, 'index.html')
        if not os.path.exists(index_path):
            return "index.html not found", 404
        return send_from_directory(static_path, 'index.html', mimetype='text/html')
    except Exception as e:
        logger.error(f"❌ 处理首页请求失败: {str(e)}")
        return "Internal Server Error", 500

@app.route('/static/<path:filename>')
def static_files(filename):
    try:
        logger.debug(f"📁 请求静态文件: {filename}")
        static_path = app.static_folder
        if static_path is None:
            return "静态文件路径未配置", 500

        return send_from_directory(static_path, filename)
    except Exception as e:
        logger.error(f"❌ 处理静态文件请求失败: {str(e)}")
        return "File not found", 404

@app.route('/api/data', methods=['GET'])
def get_data():
    global current_data_cache
    try:
        logger.debug("📡 请求/api/data")
        data = {
            'last_updated': current_data_cache.get('last_updated', "") or "",
            'daily_rising': current_data_cache.get('daily_rising', []) or [],
            'short_term_active': current_data_cache.get('short_term_active', []) or [],
            'all_cycle_rising': current_data_cache.get('all_cycle_rising', []) or [],
            'analysis_time': current_data_cache.get('analysis_time', 0),
            'next_analysis_time': current_data_cache.get('next_analysis_time', "") or ""
        }
        logger.debug(f"📡 返回数据: {json.dumps(data, indent=2)}")
        return jsonify(data)
    except Exception as e:
        logger.error(f"❌ 获取数据失败: {str(e)}")
        last_data = get_last_valid_data()
        if last_data:
            return jsonify(last_data)
        return jsonify({'error': str(e)}), 500

# 阻力位API端点
@app.route('/api/resistance_levels/<symbol>', methods=['GET'])
def get_resistance_levels(symbol):
    try:
        # 验证币种格式
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        logger.info(f"📊 获取阻力位数据: {symbol}")
        levels = calculate_resistance_levels(symbol)
        
        if not levels:
            logger.warning(f"⚠️ 未找到阻力位数据: {symbol}")
            return jsonify({'error': 'Resistance levels not found'}), 404
            
        return jsonify(levels)
    except Exception as e:
        logger.error(f"❌ 获取阻力位数据失败: {symbol}, {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# 持仓量图表数据端点
@app.route('/api/oi_chart/<symbol>/<period>', methods=['GET'])
def get_oi_chart_data(symbol, period):
    try:
        # 验证币种格式
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        if period not in PERIOD_MINUTES:
            logger.warning(f"⚠️ 不支持的周期: {period}")
            return jsonify({'error': 'Unsupported period'}), 400

        logger.info(f"📈 获取持仓量图表数据: symbol={symbol}, period={period}")
        oi_data = get_open_interest(symbol, period, use_cache=True)

        # 返回数据
        return jsonify({
            'data': oi_data['series'],
            'timestamps': oi_data['timestamps']
        })
    except Exception as e:
        logger.error(f"❌ 获取持仓量图表数据失败: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# 健康检查端点
@app.route('/health', methods=['GET'])
def health_check():
    try:
        # 检查数据库连接
        conn = sqlite3.connect('data.db')
        c = conn.cursor()
        c.execute("SELECT 1")
        conn.close()
        
        # 检查Binance连接
        binance_status = 'ok'
        if client:
            try:
                client.get_server_time()
            except:
                binance_status = 'error'
        
        return jsonify({
            'status': 'healthy',
            'database': 'ok',
            'binance': binance_status,
            'last_updated': current_data_cache.get('last_updated', 'N/A'),
            'next_analysis_time': current_data_cache.get('next_analysis_time', 'N/A'),
            'worker_alive': threading.current_thread().is_alive()
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

def start_background_threads():
    # 确保静态文件夹存在
    static_path = app.static_folder
    if not os.path.exists(static_path):
        os.makedirs(static_path)
    
    # 确保 index.html 存在
    index_path = os.path.join(static_path, 'index.html')
    if not os.path.exists(index_path):
        with open(index_path, 'w') as f:
            f.write("<html><body><h1>请将前端文件放入static目录</h1></body></html>")
    
    # 初始化数据库
    init_db()
    
    # 初始化客户端
    if not init_client():
        logger.critical("❌ 无法初始化客户端")
        return False
    
    # 启动后台线程
    worker_thread = threading.Thread(target=analysis_worker, name="AnalysisWorker")
    worker_thread.daemon = True
    worker_thread.start()
    
    scheduler_thread = threading.Thread(target=schedule_analysis, name="AnalysisScheduler")
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    logger.info("✅ 后台线程启动成功")
    return True

if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 9600))
    
    logger.info("=" * 50)
    logger.info(f"🚀 启动加密货币持仓量分析服务")
    logger.info(f"🔑 API密钥: {API_KEY[:5]}...{API_KEY[-3:]}")
    logger.info(f"🌐 服务端口: {PORT}")
    logger.info("=" * 50)
    
    if start_background_threads():
        logger.info("🚀 启动服务器...")
        app.run(host='0.0.0.0', port=PORT, debug=False)
    else:
        logger.error("❌ 服务启动失败")
