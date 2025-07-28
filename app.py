#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import json
import math
import requests
import threading
import queue
import logging
import traceback
import urllib3
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_from_directory
from binance.client import Client

# 禁用不必要的警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 设置日志级别
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, LOG_LEVEL))

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
    "last_updated": "从未更新",
    "daily_rising": [],
    "short_term_active": [],
    "all_cycle_rising": [],
    "analysis_time": 0,
    "next_analysis_time": "计算中..."
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

def init_client():
    global client
    max_retries = 5
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            logger.info(f"🔧 尝试初始化Binance客户端 (第{attempt+1}次)...")
            client = Client(
                api_key=API_KEY, 
                api_secret=API_SECRET,
                requests_params={'timeout': 30}
            )
            
            # 测试连接
            server_time = client.get_server_time()
            logger.info(f"✅ Binance客户端初始化成功，服务器时间: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
            return True
        except Exception as e:
            logger.error(f"❌ 初始化Binance客户端失败: {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"🔄 {retry_delay}秒后重试初始化客户端...")
                time.sleep(retry_delay)
    logger.critical("🔥 无法初始化Binance客户端，已达到最大重试次数")
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
        # 验证币种格式
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return {'series': [], 'timestamps': []}

        current_time = datetime.now(timezone.utc)
        cache_key = f"{symbol}_{period}"
        
        if use_cache and oi_data_cache.get(cache_key):
            cached_data = oi_data_cache[cache_key]
            if 'next_update' in cached_data and cached_data['next_update'] > current_time:
                logger.debug(f"📈 使用缓存数据: {symbol} {period}")
                return cached_data['data']

        logger.info(f"📡 请求持仓量数据: symbol={symbol}, period={period}")
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}

        response = requests.get(url, params=params, timeout=15)
        logger.debug(f"📡 响应状态: {response.status_code}")

        if response.status_code != 200:
            logger.error(f"❌ 获取{symbol}的{period}持仓量失败: HTTP {response.status_code}")
            return {'series': [], 'timestamps': []}

        data = response.json()
        logger.debug(f"📡 获取到 {len(data)} 条持仓量数据")

        if not isinstance(data, list) or len(data) == 0:
            logger.warning(f"⚠️ {symbol}的{period}持仓量数据为空")
            return {'series': [], 'timestamps': []}
            
        data.sort(key=lambda x: x['timestamp'])
        oi_series = [float(item['sumOpenInterest']) for item in data]
        timestamps = [item['timestamp'] for item in data]

        if len(oi_series) < 5:
            logger.warning(f"⚠️ {symbol}的{period}持仓量数据不足")
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

        logger.info(f"📈 获取新数据: {symbol} {period} ({len(oi_series)}点)")
        return oi_data
    except Exception as e:
        logger.error(f"❌ 获取{symbol}的{period}持仓量失败: {str(e)}")
        logger.error(traceback.format_exc())
        return {'series': [], 'timestamps': []}

def is_latest_highest(oi_data):
    if not oi_data or len(oi_data) < 30:
        logger.debug("持仓量数据不足30个点")
        return False

    latest_value = oi_data[-1]
    prev_data = oi_data[-30:-1]
    
    return latest_value > max(prev_data) if prev_data else False

def calculate_resistance_levels(symbol):
    try:
        logger.info(f"📊 计算阻力位: {symbol}")
        now = time.time()
        
        # 检查缓存
        if symbol in resistance_cache:
            cache_data = resistance_cache[symbol]
            if cache_data['expiration'] > now:
                logger.debug(f"📊 使用缓存的阻力位数据: {symbol}")
                return cache_data['levels']
        
        # 确保客户端已初始化
        if client is None and not init_client():
            logger.error("❌ 无法初始化Binance客户端，无法计算阻力位")
            return {}
        
        levels = {}
        
        for interval in RESISTANCE_INTERVALS:
            try:
                logger.info(f"📊 获取K线数据: {symbol} {interval}")
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

                # 计算斐波那契回撤位
                fib_levels = {
                    '0.236': recent_max - (recent_max - recent_min) * 0.236,
                    '0.382': recent_max - (recent_max - recent_min) * 0.382,
                    '0.5': recent_max - (recent_max - recent_min) * 0.5,
                    '0.618': recent_max - (recent_max - recent_min) * 0.618,
                    '0.786': recent_max - (recent_max - recent_min) * 0.786,
                    '1.0': recent_max
                }

                # 收集所有价格水平
                price_levels = [recent_high, recent_low]
                price_levels.extend(fib_levels.values())
                
                # 添加整数位
                base = 10 ** (math.floor(math.log10(recent_high)) - 1)
                integer_level = round(recent_high / base) * base
                price_levels.append(integer_level)

                # 去重并排序
                price_levels = sorted(set(price_levels), reverse=True)
                
                # 分离阻力和支撑
                resistance = [p for p in price_levels if recent_min < p <= recent_max][:3]
                support = [p for p in price_levels if p <= recent_min or p > recent_max][:3]

                levels[interval] = {
                    'resistance': resistance,
                    'support': support
                }
                
                logger.info(f"📊 {symbol}在{interval}的阻力位计算完成")
            except Exception as e:
                logger.error(f"计算{symbol}在{interval}的阻力位失败: {str(e)}")
                levels[interval] = {'resistance': [], 'support': []}

        resistance_cache[symbol] = {
            'levels': levels,
            'expiration': now + RESISTANCE_CACHE_EXPIRATION
        }
        logger.info(f"📊 {symbol}的阻力位计算完成")
        return levels
    except Exception as e:
        logger.error(f"计算{symbol}的阻力位失败: {str(e)}")
        return {}

def analyze_symbol(symbol):
    try:
        logger.info(f"🔍 开始分析币种: {symbol}")
        symbol_result = {
            'symbol': symbol,
            'daily_rising': None,
            'short_term_active': None,
            'all_cycle_rising': None,
            'period_status': {p: False for p in ALL_PERIODS},
            'period_count': 0
        }

        # 1. 获取日线持仓量数据
        daily_oi = get_open_interest(symbol, '1d')
        daily_series = daily_oi.get('series', [])
        
        # 2. 检查日线上涨条件
        if len(daily_series) >= 30 and is_latest_highest(daily_series):
            daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
            logger.info(f"📊 {symbol} 日线上涨条件满足，涨幅: {daily_change:.2f}%")
            
            symbol_result['daily_rising'] = {
                'symbol': symbol,
                'oi': daily_series[-1],
                'change': round(daily_change, 2),
                'period_count': 1
            }
            symbol_result['period_status']['1d'] = True
            symbol_result['period_count'] = 1

            # 3. 全周期分析
            logger.info(f"📊 开始全周期分析: {symbol}")
            all_intervals_up = True
            for period in ALL_PERIODS:
                if period == '1d':
                    continue
                    
                oi_data = get_open_interest(symbol, period)
                oi_series = oi_data.get('series', [])
                
                # 确保正确计算周期数量
                status = len(oi_series) >= 30 and is_latest_highest(oi_series)
                symbol_result['period_status'][period] = status
                
                if status:
                    symbol_result['period_count'] += 1
                else:
                    all_intervals_up = False

            if all_intervals_up:
                logger.info(f"📊 {symbol} 全周期上涨条件满足")
                symbol_result['all_cycle_rising'] = {
                    'symbol': symbol,
                    'oi': daily_series[-1],
                    'change': round(daily_change, 2),
                    'period_count': symbol_result['period_count']
                }

        # 4. 短期活跃度分析
        min5_oi = get_open_interest(symbol, '5m')
        min5_series = min5_oi.get('series', [])
        
        if len(min5_series) >= 30 and len(daily_series) >= 30:
            min5_max = max(min5_series[-30:])
            daily_avg = sum(daily_series[-30:]) / 30
            
            if min5_max > 0 and daily_avg > 0:
                ratio = min5_max / daily_avg
                logger.debug(f"📊 {symbol} 短期活跃比率: {ratio:.2f}")
                
                if ratio > 1.5:
                    logger.info(f"📊 {symbol} 短期活跃条件满足")
                    symbol_result['short_term_active'] = {
                        'symbol': symbol,
                        'oi': min5_series[-1],
                        'ratio': round(ratio, 2),
                        'period_count': symbol_result['period_count']
                    }

        logger.info(f"✅ 完成分析币种: {symbol}")
        return symbol_result
    except Exception as e:
        logger.error(f"❌ 处理{symbol}时出错: {str(e)}")
        return {
            'symbol': symbol,
            'period_status': {p: False for p in ALL_PERIODS},
            'period_count': 0
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
                # 添加 period_status 到币种对象
                result['daily_rising']['period_status'] = result['period_status']
                daily_rising.append(result['daily_rising'])
            if result.get('short_term_active'):
                result['short_term_active']['period_status'] = result['period_status']
                short_term_active.append(result['short_term_active'])
            if result.get('all_cycle_rising'):
                result['all_cycle_rising']['period_status'] = result['period_status']
                all_cycle_rising.append(result['all_cycle_rising'])
        except Exception as e:
            logger.error(f"❌ 处理{symbol}时出错: {str(e)}")

        if processed % max(1, total_symbols // 10) == 0 or processed == total_symbols:
            logger.info(f"⏳ 分析进度: {processed}/{total_symbols} ({int(processed/total_symbols*100)}%)")

    # 排序结果 - 按符合周期数量排序
    daily_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)
    short_term_active.sort(key=lambda x: x.get('period_count', 0), reverse=True)
    all_cycle_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)

    analysis_time = time.time() - start_time
    logger.info(f"📊 分析结果: 日线上涨 {len(daily_rising)}个, 短期活跃 {len(short_term_active)}个, 全部周期上涨 {len(all_cycle_rising)}个")
    logger.info(f"✅ 分析完成: 用时{analysis_time:.2f}秒")

    return {
        'daily_rising': daily_rising,
        'short_term_active': short_term_active,
        'all_cycle_rising': all_cycle_rising,
        'analysis_time': analysis_time
    }

def get_high_volume_symbols():
    # 确保客户端已初始化
    if client is None and not init_client():
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
        return [t['symbol'] for t in filtered]
    except Exception as e:
        logger.error(f"❌ 获取高交易量币种失败: {str(e)}")
        return []

def analysis_worker():
    global data_cache, current_data_cache
    logger.info("🔧 数据分析线程启动")

    # 初始数据使用默认缓存
    data_cache = {
        "last_updated": "从未更新",
        "daily_rising": [],
        "short_term_active": [],
        "all_cycle_rising": [],
        "analysis_time": 0,
        "next_analysis_time": "计算中..."
    }
    current_data_cache = data_cache.copy()

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
                
                logger.info(f"📊 分析结果已生成")
                data_cache = new_data
                current_data_cache = new_data.copy()
                logger.info(f"✅ 数据更新成功")
            except Exception as e:
                logger.error(f"❌ 分析过程中出错: {str(e)}")
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

def schedule_analysis():
    logger.info("⏰ 定时分析调度器启动")
    now = datetime.now(timezone.utc)
    next_time = get_next_update_time('5m')
    initial_wait = (next_time - now).total_seconds()
    logger.info(f"⏳ 首次分析将在 {initial_wait:.1f} 秒后开始 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})...")
    time.sleep(max(0, initial_wait))

    while True:
        analysis_start = datetime.now(timezone.utc)
        logger.info(f"🔔 触发定时分析任务 ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')})")
        analysis_queue.put("ANALYZE")
        analysis_queue.join()

        analysis_duration = (datetime.now(timezone.utc) - analysis_start).total_seconds()
        now = datetime.now(timezone.utc)
        next_time = get_next_update_time('5m')
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
        return send_from_directory(app.static_folder, 'index.html')
    except Exception as e:
        logger.error(f"❌ 处理首页请求失败: {str(e)}")
        return "Internal Server Error", 500

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(app.static_folder, filename)

@app.after_request
def add_cors_headers(response):
    # 添加 CORS 头
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    return response

@app.route('/api/data', methods=['GET'])
def get_data():
    global current_data_cache
    try:
        logger.info("📡 收到 /api/data 请求")
        
        # 确保数据格式正确
        if not current_data_cache or not isinstance(current_data_cache, dict):
            logger.warning("⚠️ 当前数据缓存格式错误，重置为默认")
            current_data_cache = {
                "last_updated": "从未更新",
                "daily_rising": [],
                "short_term_active": [],
                "all_cycle_rising": [],
                "analysis_time": 0,
                "next_analysis_time": "计算中..."
            }
        
        # 确保所有数组元素都有必要的字段
        def validate_coins(coins):
            valid_coins = []
            for coin in coins:
                if not isinstance(coin, dict):
                    continue
                if 'symbol' not in coin:
                    coin['symbol'] = '未知币种'
                if 'oi' not in coin:
                    coin['oi'] = 0
                if 'change' not in coin:
                    coin['change'] = 0
                if 'ratio' not in coin:
                    coin['ratio'] = 0
                if 'period_count' not in coin:
                    coin['period_count'] = 0
                if 'period_status' not in coin:
                    coin['period_status'] = {}
                valid_coins.append(coin)
            return valid_coins
        
        # 确保数组类型正确
        daily_rising = validate_coins(current_data_cache.get('daily_rising', []))
        short_term_active = validate_coins(current_data_cache.get('short_term_active', []))
        all_cycle_rising = validate_coins(current_data_cache.get('all_cycle_rising', []))
        
        # 过滤掉全周期上涨的币种（不在日线上涨列表显示）
        all_cycle_symbols = {coin['symbol'] for coin in all_cycle_rising}
        filtered_daily_rising = [coin for coin in daily_rising if coin['symbol'] not in all_cycle_symbols]
        
        data = {
            'last_updated': current_data_cache.get('last_updated', ""),
            'daily_rising': filtered_daily_rising,
            'short_term_active': short_term_active,
            'all_cycle_rising': all_cycle_rising,
            'analysis_time': current_data_cache.get('analysis_time', 0),
            'next_analysis_time': current_data_cache.get('next_analysis_time', "")
        }
        
        logger.info(f"📦 返回数据: {len(data['daily_rising'])} 日线上涨币种")
        return jsonify(data)
    
    except Exception as e:
        logger.error(f"❌ 获取数据失败: {str(e)}")
        # 返回有结构的空数据而不是错误
        return jsonify({
            'last_updated': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            'daily_rising': [],
            'short_term_active': [],
            'all_cycle_rising': [],
            'analysis_time': 0,
            'next_analysis_time': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        })

@app.route('/api/resistance_levels/<symbol>', methods=['GET'])
def get_resistance_levels(symbol):
    try:
        # 验证币种格式
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        logger.info(f"📊 获取阻力位数据: {symbol}")
        levels = calculate_resistance_levels(symbol)
        return jsonify(levels)
    except Exception as e:
        logger.error(f"❌ 获取阻力位数据失败: {symbol}, {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/oi_chart/<symbol>/<period>', methods=['GET'])
def get_oi_chart_data(symbol, period):
    try:
        # 验证币种格式
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        if period not in PERIOD_MINUTES:
            logger.warning(f"⚠ 不支持的周期: {period}")
            return jsonify({'error': 'Unsupported period'}), 400

        logger.info(f"📈 获取持仓量图表数据: symbol={symbol}, period={period}")
        oi_data = get_open_interest(symbol, period, use_cache=True)
        return jsonify({
            'data': oi_data.get('series', []),
            'timestamps': oi_data.get('timestamps', [])
        })
    except Exception as e:
        logger.error(f"❌ 获取持仓量图表数据失败: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    try:
        # 检查Binance连接
        binance_status = 'ok'
        if client:
            try:
                client.get_server_time()
            except:
                binance_status = 'error'
        else:
            binance_status = 'not initialized'
        
        return jsonify({
            'status': 'healthy',
            'binance': binance_status,
            'last_updated': current_data_cache.get('last_updated', 'N/A'),
            'next_analysis_time': current_data_cache.get('next_analysis_time', 'N/A'),
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
    
    # 初始化客户端
    if not init_client():
        logger.critical("❌ 无法初始化客户端")
        return False
    
    # 确保缓存中有初始数据
    global current_data_cache
    if not current_data_cache or not current_data_cache.get('last_updated') or current_data_cache.get('last_updated') == "从未更新":
        # 创建初始数据记录
        current_data_cache = {
            "last_updated": "等待首次分析",
            "daily_rising": [],
            "short_term_active": [],
            "all_cycle_rising": [],
            "analysis_time": 0,
            "next_analysis_time": "计算中..."
        }
        logger.info("🆕 创建初始内存数据记录")
    
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
    logger.info(f"🚀 启动加密货币持仓量分析服务 (内存存储版)")
    logger.info(f"🔑 API密钥: {API_KEY[:5]}...{API_KEY[-3:]}")
    logger.info(f"🌐 服务端口: {PORT}")
    logger.info("💾 数据存储: 内存存储 (无持久化)")
    logger.info("=" * 50)
    
    if start_background_threads():
        logger.info("🚀 启动服务器...")
        app.run(host='0.0.0.0', port=PORT, debug=False)
    else:
        logger.critical("🔥 无法启动服务，请检查错误日志")
