#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-

import sys
import subprocess
import os
import time
import re
import traceback  # 添加 traceback 导入
import json
import math
import sqlite3
import requests
import threading
import queue
import logging
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_from_directory, request
from binance.client import Client

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

# 获取当前文件所在目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='/static')

# Binance API 配置 - 使用环境变量
API_KEY = os.environ.get('BINANCE_API_KEY', 'your_api_key_here')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'your_api_secret_here')
client = None

# ==================== 数据缓存 ====================
data_cache = {
    "last_updated": None,
    "daily_rising": [],
    "short_term_active": [],
    "all_cycle_rising": [],
    "analysis_time": 0
}

# 只读数据缓存
current_data_cache = data_cache.copy()

# 持仓量数据缓存 {symbol: {period: {data: ..., next_update: ...}}}
oi_data_cache = {}

# 阻力位数据缓存 {symbol: {interval: levels, expiration}}
resistance_cache = {}
RESISTANCE_CACHE_EXPIRATION = 24 * 3600  # 24小时缓存

# 使用队列进行线程间通信
analysis_queue = queue.Queue()

# 线程池执行器
executor = ThreadPoolExecutor(max_workers=10)

# 周期设置 (分钟)
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

# 阻力位分析周期
RESISTANCE_INTERVALS = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']

# 所有分析周期
ALL_PERIODS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']

# 初始化数据库
def init_db():
    try:
        conn = sqlite3.connect('data.db')
        c = conn.cursor()

        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_data'"
        )
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
                        resistance_data TEXT)''')
            conn.commit()
            logger.info("✅ 数据库表创建成功")
        else:
            logger.info("✅ 数据库表已存在")

        conn.close()
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {str(e)}")
        logger.error(traceback.format_exc())
        try:
            os.remove('data.db')
            logger.warning("🗑️ 删除损坏的数据库文件，将创建新数据库")
            init_db()
        except Exception:
            logger.critical("🔥 无法修复数据库，请手动检查")

# 保存数据到数据库
def save_to_db(data):
    try:
        conn = sqlite3.connect('data.db')
        c = conn.cursor()

        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_data'"
        )
        if not c.fetchone():
            logger.warning("⚠️ 数据库表不存在，正在创建...")
            c.execute('''CREATE TABLE crypto_data
                        (id INTEGER PRIMARY KEY, 
                        last_updated TEXT,
                        daily_rising TEXT,
                        short_term_active TEXT,
                        all_cycle_rising TEXT,
                        analysis_time REAL,
                        resistance_data TEXT)''')
            conn.commit()

        resistance_json = json.dumps(resistance_cache)

        c.execute(
            "INSERT INTO crypto_data VALUES (NULL, ?, ?, ?, ?, ?, ?)",
            (data['last_updated'], json.dumps(data['daily_rising']),
             json.dumps(data['short_term_active']),
             json.dumps(data['all_cycle_rising']), 
             data['analysis_time'],
             resistance_json))
        conn.commit()
        conn.close()
        logger.info("💾 数据保存到数据库成功")
    except Exception as e:
        logger.error(f"❌ 保存数据到数据库失败: {str(e)}")
        logger.error(traceback.format_exc())
        try:
            init_db()
            save_to_db(data)
        except Exception:
            logger.critical("🔥 无法保存数据到数据库")

# 获取最后有效数据
def get_last_valid_data():
    try:
        conn = sqlite3.connect('data.db')
        c = conn.cursor()

        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_data'"
        )
        if not c.fetchone():
            return None

        c.execute("SELECT * FROM crypto_data ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        conn.close()

        if row:
            resistance_data = json.loads(row[6]) if row[6] else {}
            for symbol, data in resistance_data.items():
                resistance_cache[symbol] = data

            return {
                'last_updated': row[1],
                'daily_rising': json.loads(row[2]),
                'short_term_active': json.loads(row[3]),
                'all_cycle_rising': json.loads(row[4]),
                'analysis_time': row[5]
            }
        return None
    except Exception as e:
        logger.error(f"❌ 获取最后有效数据失败: {str(e)}")
        logger.error(traceback.format_exc())
        return None

# 初始化客户端
def init_client():
    global client
    try:
        # 如果已经初始化则直接返回
        if client:
            return True
            
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
        return False

# 计算下一个更新时间点
def get_next_update_time(period):
    minutes = PERIOD_MINUTES.get(period, 5)
    now = datetime.now(timezone.utc)

    if period.endswith('m'):
        current_minute = now.minute
        period_minutes = int(period[:-1])
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

# 获取持仓量数据
def get_open_interest(symbol, period, use_cache=True):
    try:
        # 验证币种格式 - 更严格的验证
        if not re.match(r"^[A-Z]{3,15}USDT$", symbol):
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

        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}

        logger.info(f"📡 请求持仓量数据: {url}?symbol={symbol}&period={period}")
        response = requests.get(url, params=params, timeout=15)

        # 检查响应状态
        if response.status_code != 200:
            logger.error(f"❌ 获取{symbol}的{period}持仓量失败: HTTP {response.status_code} - {response.text}")
            return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

        data = response.json()

        # 检查返回的数据格式
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

        # 添加数据验证
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

# 检查持仓量是否创新高
def is_latest_highest(oi_data):
    if len(oi_data) < 30:
        return False

    latest_value = oi_data[-1]
    prev_data = oi_data[-30:-1]

    if not prev_data:
        return False

    return latest_value > max(prev_data)

# 计算阻力位
def calculate_resistance_levels(symbol):
    try:
        now = time.time()
        if symbol in resistance_cache:
            cache_data = resistance_cache[symbol]
            if cache_data['expiration'] > now:
                return cache_data['levels']

        # 确保客户端已初始化
        if not client:
            if not init_client():
                logger.error(f"❌ 无法初始化客户端，无法获取{symbol}的阻力位")
                return {}

        levels = {}

        for interval in RESISTANCE_INTERVALS:
            try:
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
        return levels
    except Exception as e:
        logger.error(f"计算{symbol}的阻力位失败: {str(e)}")
        logger.error(traceback.format_exc())
        return {}

# 分析单个币种趋势
def analyze_symbol(symbol):
    try:
        symbol_result = {
            'symbol': symbol,
            'daily_rising': None,
            'short_term_active': None,
            'all_cycle_rising': None,
            'rising_periods': [],
            'period_status': {p: False for p in ALL_PERIODS},
            'period_count': 0,
            'oi_data': {}  # 存储所有周期的持仓量数据
        }

        # 1. 获取日线持仓量数据
        daily_oi = get_open_interest(symbol, '1d', use_cache=True)
        symbol_result['oi_data']['1d'] = daily_oi
        daily_series = daily_oi['series']

        # 2. 检查日线上涨条件
        if len(daily_series) >= 30:
            daily_up = is_latest_highest(daily_series)

            if daily_up:
                daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
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
                all_intervals_up = True
                for period in ALL_PERIODS:
                    if period == '1d':
                        continue

                    oi_data = get_open_interest(symbol, period, use_cache=True)
                    symbol_result['oi_data'][period] = oi_data
                    oi_series = oi_data['series']
                    if len(oi_series) < 30:
                        status = False
                    else:
                        status = is_latest_highest(oi_series)
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
                        'period_status': symbol_result['period_status'],
                        'period_count': symbol_result['period_count']
                    }

        # 4. 短期活跃度分析
        min5_oi = get_open_interest(symbol, '5m', use_cache=True)
        symbol_result['oi_data']['5m'] = min5_oi
        min5_series = min5_oi['series']
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
                        'period_status': symbol_result['period_status'],
                        'period_count': symbol_result['period_count']
                    }

        if symbol_result['daily_rising']:
            symbol_result['daily_rising']['period_status'] = symbol_result['period_status']
            symbol_result['daily_rising']['period_count'] = symbol_result['period_count']

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

# 分析币种趋势
def analyze_trends():
    start_time = time.time()
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
    short_term_active.sort(key=lambda x: (x.get('period_count', 0), x.get('ratio', 0)), reverse=True)
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

# 获取高交易量币种
def get_high_volume_symbols():
    if not client:
        if not init_client():
            return []

    try:
        tickers = client.futures_ticker()
        filtered = [
            t for t in tickers if float(t.get('quoteVolume', 0)) > 10000000
            and t.get('symbol', '').endswith('USDT')
        ]
        logger.info(f"📊 找到 {len(filtered)} 个高交易量币种")
        return [t['symbol'] for t in filtered]
    except Exception as e:
        logger.error(f"❌ 获取高交易量币种失败: {str(e)}")
        logger.error(traceback.format_exc())
        return []

# 数据分析工作线程
def analysis_worker():
    global data_cache, current_data_cache
    logger.info("🔧 数据分析线程启动")
    init_db()

    initial_data = get_last_valid_data()
    if initial_data:
        data_cache = initial_data
        current_data_cache = data_cache.copy()
        logger.info(f"🔁 加载历史数据")
    else:
        logger.info("🆕 没有历史数据，将进行首次分析")

    while True:
        try:
            task = analysis_queue.get()
            if task == "STOP":
                break

            analysis_start = datetime.now(timezone.utc)
            logger.info(f"⏱️ 开始更新数据 ({analysis_start.strftime('%H:%M:%S')})...")

            backup_cache = data_cache.copy()
            current_backup = current_data_cache.copy()

            try:
                result = analyze_trends()
                new_data = {
                    "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "daily_rising": result['daily_rising'],
                    "short_term_active": result['short_term_active'],
                    "all_cycle_rising": result['all_cycle_rising'],
                    "analysis_time": result['analysis_time']
                }

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
            logger.info("=" * 50)

        except Exception as e:
            logger.error(f"❌ 分析失败: {str(e)}")
            logger.error(traceback.format_exc())
        analysis_queue.task_done()

# 定时触发数据分析
def schedule_analysis():
    now = datetime.now(timezone.utc)
    current_minute = now.minute
    next_minute = ((current_minute // 5) + 1) * 5
    if next_minute >= 60:
        next_minute = 0
        next_time = now.replace(hour=now.hour + 1, minute=next_minute, second=0, microsecond=0)
    else:
        next_time = now.replace(minute=next_minute, second=0, microsecond=0)

    initial_wait = (next_time - now).total_seconds()
    logger.info(f"⏳ 首次分析将在 {initial_wait:.1f} 秒后开始 ({next_time.strftime('%H:%M:%S')})...")
    time.sleep(initial_wait)

    while True:
        analysis_start = datetime.now(timezone.utc)
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

        logger.info(f"⏳ 下次分析将在 {wait_time:.1f} 秒后 ({next_time.strftime('%H:%M:%S')}")
        time.sleep(wait_time)

# API路由
@app.route('/')
def index():
    try:
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
        return jsonify({
            'last_updated': current_data_cache['last_updated'] or "",
            'daily_rising': current_data_cache['daily_rising'] or [],
            'short_term_active': current_data_cache['short_term_active'] or [],
            'all_cycle_rising': current_data_cache['all_cycle_rising'] or [],
            'analysis_time': current_data_cache.get('analysis_time', 0)
        })
    except Exception as e:
        logger.error(f"❌ 获取数据失败: {str(e)}")
        last_data = get_last_valid_data()
        if last_data:
            return jsonify(last_data)
        return jsonify({'error': str(e)}), 500

@app.route('/api/oi_chart/<symbol>/<period>', methods=['GET'])
def get_oi_chart(symbol, period):
    try:
        # 严格验证币种格式
        if not re.match(r"^[A-Z]{3,15}USDT$", symbol):
            logger.error(f"❌ 无效的币种名称格式: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        # 验证周期
        if period not in PERIOD_MINUTES:
            logger.warning(f"⚠️ 不支持的周期: {period}, 使用5m")
            period = '5m'

        oi_data = get_open_interest(symbol, period, use_cache=True)

        # 更好的数据验证
        if not oi_data or 'series' not in oi_data or 'timestamps' not in oi_data:
            logger.error(f"❌ 无效的持仓量数据结构: {symbol} {period}")
            return jsonify({'error': 'Invalid data structure'}), 500

        if len(oi_data['series']) == 0 or len(oi_data['timestamps']) == 0:
            logger.warning(f"⚠️ 无有效持仓量数据: {symbol} {period}")
            return jsonify({'error': 'No data available'}), 404

        # 更好的时间戳处理
        timestamps = []
        for ts in oi_data['timestamps']:
            try:
                dt = datetime.fromtimestamp(ts / 1000, timezone.utc)
                timestamps.append(dt.strftime('%m-%d %H:%M'))
            except Exception as e:
                logger.warning(f"时间戳转换失败: {ts} - {str(e)}")
                continue

        # 确保时间戳和系列长度一致
        if len(timestamps) != len(oi_data['series']):
            logger.error(f"❌ 数据长度不匹配: {len(timestamps)}时间戳 vs {len(oi_data['series'])}数据点")
            return jsonify({'error': 'Data length mismatch'}), 500

        return jsonify({
            'symbol': symbol,
            'period': period,
            'data': oi_data['series'],
            'labels': timestamps,
            'cache_time': oi_data.get('cache_time', datetime.now(timezone.utc).isoformat())
        })
    except Exception as e:
        logger.error(f"❌ 获取持仓量图表失败: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/kline/<symbol>', methods=['GET'])
def get_kline(symbol):
    # 确保客户端已初始化
    if not client:
        if not init_client():
            logger.error("无法连接API")
            return jsonify({'error': '无法连接API'}), 500

    try:
        if not symbol or not isinstance(symbol, str) or not symbol.endswith('USDT'):
            return jsonify({'error': 'Invalid symbol format'}), 400

        klines = client.futures_klines(
            symbol=symbol, 
            interval='5m', 
            limit=30,
            timeout=10
        )

        if not klines or len(klines) < 10:
            return jsonify({'error': 'Insufficient kline data'}), 404

        data = []
        for k in klines:
            try:
                data.append({
                    'time': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4])
                })
            except Exception:
                continue

        return jsonify({'symbol': symbol, 'data': data})
    except Exception as e:
        logger.error(f"❌ 获取K线失败: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# 修改阻力位接口以支持简化币种名称
@app.route('/api/resistance_levels/<symbol>', methods=['GET'])
def get_resistance_levels(symbol):
    try:
        # 检查是否传入简化币种名称（不带USDT后缀）
        if not symbol.endswith('USDT') and len(symbol) <= 5:
            # 自动添加USDT后缀
            symbol = symbol.upper() + 'USDT'

        # 验证币种格式
        if not re.match(r"^[A-Z]{3,15}USDT$", symbol):
            return jsonify({'error': 'Invalid symbol format'}), 400

        # 获取阻力位数据
        levels = calculate_resistance_levels(symbol)

        # 如果获取失败，尝试使用基础币种名称
        if not levels:
            base_symbol = symbol.replace('USDT', '')
            logger.warning(f"尝试使用基础币种名称: {base_symbol}")
            levels = calculate_resistance_levels(base_symbol + 'USDT')

        if not levels:
            return jsonify({'error': 'No resistance levels available'}), 404

        return jsonify(levels)
    except Exception as e:
        logger.error(f"❌ 获取阻力位失败: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# 新增阻力位查询API（支持POST和简化名称）
@app.route('/api/query_resistance_levels', methods=['POST'])
def query_resistance_levels():
    try:
        data = request.get_json()
        if not data or 'symbol' not in data:
            return jsonify({'error': 'Missing symbol parameter'}), 400

        symbol = data['symbol'].strip().upper()

        # 如果输入的是简化币种名称（不带USDT后缀）
        if not symbol.endswith('USDT'):
            # 自动添加USDT后缀
            symbol += 'USDT'

        # 验证币种格式
        if not re.match(r"^[A-Z]{3,15}USDT$", symbol):
            return jsonify({'error': 'Invalid symbol format'}), 400

        # 获取阻力位数据
        levels = calculate_resistance_levels(symbol)

        # 如果获取失败，尝试使用基础币种名称
        if not levels:
            base_symbol = symbol.replace('USDT', '')
            logger.warning(f"尝试使用基础币种名称: {base_symbol}")
            levels = calculate_resistance_levels(base_symbol + 'USDT')

        if not levels:
            return jsonify({'error': 'No resistance levels available'}), 404

        return jsonify({
            'symbol': symbol,
            'levels': levels
        })
    except Exception as e:
        logger.error(f"❌ 阻力位查询失败: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/test')
def test():
    return '✅ 服务正常运行!'

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
    worker_thread = threading.Thread(target=analysis_worker)
    worker_thread.daemon = True
    worker_thread.start()
    
    scheduler_thread = threading.Thread(target=schedule_analysis)
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    return True

if __name__ == '__main__':
    # 获取端口
    PORT = int(os.environ.get("PORT", 9600))
    
    # 启动后台线程
    if start_background_threads():
        logger.info("🚀 启动服务器...")
        app.run(host='0.0.0.0', port=PORT, debug=False)
    else:
        logger.error("❌ 服务启动失败")
