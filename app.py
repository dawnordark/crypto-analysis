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
import numpy as np
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
API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '')
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
OI_CACHE_EXPIRATION = 5 * 60  # 5分钟缓存过期

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

RESISTANCE_INTERVALS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']
ALL_PERIODS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']

def init_client():
    global client
    max_retries = 5
    retry_delay = 5
    
    # 检查API密钥是否设置
    if not API_KEY or not API_SECRET:
        logger.error("❌ Binance API密钥未设置，请设置环境变量BINANCE_API_KEY和BINANCE_API_SECRET")
        return False
    
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

def get_next_update_time():
    now = datetime.now(timezone.utc)
    # 计算下一个5分5秒的时间点
    current_minute = now.minute
    current_second = now.second
    minutes_to_add = 5 - (current_minute % 5)
    seconds_to_add = 5 - current_second if minutes_to_add == 0 else (5 - current_second) + (minutes_to_add - 1) * 60
    next_update = now + timedelta(seconds=seconds_to_add)
    return next_update

def get_open_interest(symbol, period, use_cache=True):
    try:
        # 验证币种格式
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return {'series': [], 'timestamps': []}

        current_time = datetime.now(timezone.utc)
        cache_key = f"{symbol}_{period}"
        
        if use_cache and cache_key in oi_data_cache:
            cached_data = oi_data_cache[cache_key]
            # 检查缓存是否过期
            if 'expiration' in cached_data and cached_data['expiration'] > current_time:
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
        
        # 设置5分钟缓存过期
        expiration = current_time + timedelta(seconds=OI_CACHE_EXPIRATION)
        oi_data_cache[cache_key] = {
            'data': oi_data,
            'expiration': expiration
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

def find_swing_points(prices, window=5):
    """识别摆动点（局部高点和低点）"""
    highs = []
    lows = []
    
    for i in range(window, len(prices) - window):
        # 检查是否为局部高点
        if all(prices[i] > prices[i - j] for j in range(1, window+1)) and \
           all(prices[i] > prices[i + j] for j in range(1, window+1)):
            highs.append(prices[i])
        
        # 检查是否为局部低点
        if all(prices[i] < prices[i - j] for j in range(1, window+1)) and \
           all(prices[i] < prices[i + j] for j in range(1, window+1)):
            lows.append(prices[i])
    
    return highs, lows

def calculate_fib_levels(high, low):
    """计算斐波那契回调水平"""
    if high <= low:
        return []
    
    levels = []
    ratios = [0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618]
    
    for ratio in ratios:
        level = high - (high - low) * ratio
        levels.append(level)
    
    return levels

def calculate_trendline_levels(prices):
    """使用线性回归计算趋势线水平"""
    if len(prices) < 10:
        return []
    
    try:
        # 使用线性回归
        x = np.arange(len(prices))
        y = np.array(prices)
        
        # 计算斜率 (m) 和截距 (b)
        A = np.vstack([x, np.ones(len(x))]).T
        m, b = np.linalg.lstsq(A, y, rcond=None)[0]
        
        # 计算当前时刻的趋势线价格
        current_level = m * len(prices) + b
        return [current_level]
    except:
        return []

def detect_resonance_levels(levels, tolerance=0.005):
    """检测共振水平（聚类分析）"""
    if not levels:
        return {'resistance': [], 'support': []}
    
    levels.sort()
    clusters = []
    current_cluster = [levels[0]]
    
    for level in levels[1:]:
        if level <= current_cluster[-1] * (1 + tolerance):
            current_cluster.append(level)
        else:
            clusters.append(current_cluster)
            current_cluster = [level]
    
    clusters.append(current_cluster)
    
    # 计算每个聚类的平均水平和强度
    cluster_levels = []
    for cluster in clusters:
        avg = sum(cluster) / len(cluster)
        strength = len(cluster)
        cluster_levels.append((avg, strength))
    
    # 按强度降序排序
    cluster_levels.sort(key=lambda x: x[1], reverse=True)
    
    # 只返回前3个阻力位和前3个支撑位
    resistance = [x[0] for x in cluster_levels if x[0] > 0][:3]
    support = [x[0] for x in cluster_levels if x[0] > 0][:3]
    
    return {'resistance': resistance, 'support': support}

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
            return {'resistance': [], 'support': [], 'current_price': 0}
        
        # 获取当前价格
        try:
            ticker = client.futures_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            logger.info(f"📊 {symbol}当前价格: {current_price}")
        except Exception as e:
            logger.error(f"❌ 获取{symbol}当前价格失败: {str(e)}")
            current_price = 0
        
        all_levels = []
        interval_levels_map = {}
        
        for interval in RESISTANCE_INTERVALS:
            try:
                logger.info(f"📊 获取K线数据: {symbol} {interval}")
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=100)
                
                if not klines or len(klines) < 10:
                    logger.warning(f"⚠️ {symbol}在{interval}的K线数据不足")
                    continue

                high_prices = [float(k[2]) for k in klines]
                low_prices = [float(k[3]) for k in klines]
                close_prices = [float(k[4]) for k in klines]
                
                # 1. 识别摆动点
                swing_highs, swing_lows = find_swing_points(high_prices)
                logger.debug(f"📊 {symbol}在{interval}的摆动高点: {swing_highs}, 摆动低点: {swing_lows}")
                
                # 2. 计算斐波那契水平
                recent_high = max(high_prices[-30:]) if len(high_prices) >= 30 else max(high_prices)
                recent_low = min(low_prices[-30:]) if len(low_prices) >= 30 else min(low_prices)
                fib_levels = calculate_fib_levels(recent_high, recent_low)
                logger.debug(f"📊 {symbol}在{interval}的斐波那契水平: {fib_levels}")
                
                # 3. 计算趋势线水平
                trendline_levels = calculate_trendline_levels(close_prices)
                logger.debug(f"📊 {symbol}在{interval}的趋势线水平: {trendline_levels}")
                
                # 合并所有水平
                interval_levels = swing_highs + swing_lows + fib_levels + trendline_levels
                interval_levels_map[interval] = interval_levels
                
                # 添加到全局列表
                all_levels.extend(interval_levels)
                
            except Exception as e:
                logger.error(f"计算{symbol}在{interval}的阻力位失败: {str(e)}")
                logger.error(traceback.format_exc())

        # 全局共振检测
        final_levels = detect_resonance_levels(all_levels)
        logger.info(f"📊 {symbol}全局最优阻力位: {final_levels['resistance']}, 支撑位: {final_levels['support']}")
        
        # 找出关键阻力位和支撑位（最接近当前价格的）
        key_resistance = None
        key_support = None
        
        if current_price > 0:
            # 找出最接近的阻力位（高于当前价格）
            resistances_above = [r for r in final_levels['resistance'] if r > current_price]
            if resistances_above:
                key_resistance = min(resistances_above, key=lambda x: abs(x - current_price))
                resistance_distance = ((key_resistance - current_price) / current_price) * 100
            
            # 找出最接近的支撑位（低于当前价格）
            supports_below = [s for s in final_levels['support'] if s < current_price]
            if supports_below:
                key_support = max(supports_below, key=lambda x: abs(x - current_price))
                support_distance = ((current_price - key_support) / current_price) * 100
        
        # 找出关键周期（共振强度最高的阻力位/支撑位出现的周期）
        key_resistance_intervals = {}
        key_support_intervals = {}
        
        for interval, levels in interval_levels_map.items():
            for level in levels:
                # 检查是否为关键阻力位
                if key_resistance and abs(level - key_resistance) / key_resistance < 0.01:
                    key_resistance_intervals[interval] = level
                
                # 检查是否为关键支撑位
                if key_support and abs(level - key_support) / key_support < 0.01:
                    key_support_intervals[interval] = level
        
        result = {
            'resistance': final_levels['resistance'],
            'support': final_levels['support'],
            'current_price': current_price,
            'key_resistance': key_resistance,
            'key_support': key_support,
            'key_resistance_intervals': key_resistance_intervals,
            'key_support_intervals': key_support_intervals
        }
        
        resistance_cache[symbol] = {
            'levels': result,
            'expiration': now + RESISTANCE_CACHE_EXPIRATION
        }
        return result
    except Exception as e:
        logger.error(f"计算{symbol}的阻力位失败: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            'resistance': [], 
            'support': [], 
            'current_price': 0,
            'key_resistance': None,
            'key_support': None,
            'key_resistance_intervals': {},
            'key_support_intervals': {}
        }

def analyze_daily_rising(symbol):
    """分析日线上涨条件"""
    try:
        # 1. 获取日线持仓量数据
        daily_oi = get_open_interest(symbol, '1d')
        daily_series = daily_oi.get('series', [])
        
        # 2. 检查日线上涨条件
        if len(daily_series) >= 30 and is_latest_highest(daily_series):
            daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
            logger.info(f"📊 {symbol} 日线上涨条件满足，涨幅: {daily_change:.2f}%")
            
            # 获取关键阻力位
            resistance_data = calculate_resistance_levels(symbol)
            key_resistance = resistance_data.get('key_resistance', 0)
            
            return {
                'symbol': symbol,
                'oi': daily_series[-1],
                'change': round(daily_change, 2),
                'key_resistance': key_resistance
            }
        return None
    except Exception as e:
        logger.error(f"❌ 分析{symbol}日线上涨时出错: {str(e)}")
        return None

def analyze_all_cycles(symbol, daily_rising_item):
    """分析全周期上涨条件"""
    try:
        logger.info(f"📊 开始全周期分析: {symbol}")
        period_status = {}
        period_count = 0
        
        # 确保日线状态为True
        period_status['1d'] = True
        
        for period in ALL_PERIODS:
            if period == '1d':
                continue
                
            oi_data = get_open_interest(symbol, period)
            oi_series = oi_data.get('series', [])
            
            # 确保正确计算周期数量
            status = len(oi_series) >= 30 and is_latest_highest(oi_series)
            period_status[period] = status
            
            if status:
                period_count += 1
        
        # 检查是否所有周期都满足条件
        all_intervals_up = all(period_status.values())
        
        if all_intervals_up:
            logger.info(f"📊 {symbol} 全周期上涨条件满足")
            
            # 获取关键阻力位
            resistance_data = calculate_resistance_levels(symbol)
            key_resistance = resistance_data.get('key_resistance', 0)
            
            return {
                'symbol': symbol,
                'oi': daily_rising_item['oi'],
                'change': daily_rising_item['change'],
                'period_status': period_status,
                'period_count': period_count,
                'key_resistance': key_resistance
            }
        return None
    except Exception as e:
        logger.error(f"❌ 分析{symbol}全周期时出错: {str(e)}")
        return None

def analyze_short_term_active(symbol):
    """分析短期活跃条件"""
    try:
        # 获取5分钟持仓量数据
        min5_oi = get_open_interest(symbol, '5m')
        min5_series = min5_oi.get('series', [])
        
        # 获取日线持仓量数据
        daily_oi = get_open_interest(symbol, '1d')
        daily_series = daily_oi.get('series', [])
        
        if len(min5_series) >= 30 and len(daily_series) >= 30:
            min5_max = max(min5_series[-30:])
            daily_avg = sum(daily_series[-30:]) / 30
            
            if min5_max > 0 and daily_avg > 0:
                ratio = min5_max / daily_avg
                logger.debug(f"📊 {symbol} 短期活跃比率: {ratio:.2f}")
                
                if ratio > 1.5:
                    logger.info(f"📊 {symbol} 短期活跃条件满足")
                    return {
                        'symbol': symbol,
                        'oi': min5_series[-1],
                        'ratio': round(ratio, 2)
                    }
        return None
    except Exception as e:
        logger.error(f"❌ 分析{symbol}短期活跃时出错: {str(e)}")
        return None

def analyze_trends():
    start_time = time.time()
    logger.info("🔍 开始分析币种趋势...")
    symbols = get_high_volume_symbols()
    
    if not symbols:
        logger.warning("⚠️ 没有找到高交易量币种")
        return {
            'daily_rising': [],
            'short_term_active': [],
            'all_cycle_rising': [],
            'analysis_time': 0
        }

    logger.info(f"🔍 开始分析 {len(symbols)} 个币种")

    daily_rising = []
    short_term_active = []
    all_cycle_rising = []

    # 使用10个线程并行处理
    with ThreadPoolExecutor(max_workers=10) as executor:
        # 第一步：并行分析日线上涨
        daily_futures = {executor.submit(analyze_daily_rising, symbol): symbol for symbol in symbols}
        
        # 第二步：并行分析短期活跃
        short_term_futures = {executor.submit(analyze_short_term_active, symbol): symbol for symbol in symbols}
        
        # 等待日线上涨结果
        for future in as_completed(daily_futures):
            symbol = daily_futures[future]
            try:
                result = future.result()
                if result:
                    daily_rising.append(result)
            except Exception as e:
                logger.error(f"❌ 处理{symbol}的日线上涨时出错: {str(e)}")
        
        # 第三步：分析全周期上涨（基于日线上涨的结果）
        if daily_rising:
            all_cycle_futures = {executor.submit(analyze_all_cycles, coin['symbol'], coin): coin['symbol'] for coin in daily_rising}
            for future in as_completed(all_cycle_futures):
                symbol = all_cycle_futures[future]
                try:
                    result = future.result()
                    if result:
                        all_cycle_rising.append(result)
                except Exception as e:
                    logger.error(f"❌ 处理{symbol}的全周期时出错: {str(e)}")
        
        # 等待短期活跃结果
        for future in as_completed(short_term_futures):
            symbol = short_term_futures[future]
            try:
                result = future.result()
                if result:
                    short_term_active.append(result)
            except Exception as e:
                logger.error(f"❌ 处理{symbol}的短期活跃时出错: {str(e)}")

    # 排序结果
    daily_rising.sort(key=lambda x: x.get('change', 0), reverse=True)
    short_term_active.sort(key=lambda x: x.get('ratio', 0), reverse=True)
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
        logger.error(traceback.format_exc())
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
                next_analysis_time = get_next_update_time()
                
                new_data = {
                    "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "daily_rising": result['daily_rising'],
                    "short_term_active": result['short_term_active'],
                    "all_cycle_rising": result['all_cycle_rising'],
                    "analysis_time": result['analysis_time'],
                    "next_analysis_time": next_analysis_time.strftime("%Y-%m-%d %H:%M:%S")
                }
                
                logger.info(f"📊 分析结果已生成")
                logger.info(f"全周期上涨币种数量: {len(new_data['all_cycle_rising'])}")
                logger.info(f"日线上涨币种数量: {len(new_data['daily_rising'])}")
                logger.info(f"短期活跃币种数量: {len(new_data['short_term_active'])}")
                
                data_cache = new_data
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
            next_time = get_next_update_time()
            wait_seconds = (next_time - analysis_end).total_seconds()
            logger.info(f"⏳ 下次分析将在 {wait_seconds:.1f} 秒后 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})")
            
            logger.info("=" * 50)
        except Exception as e:
            logger.error(f"❌ 分析失败: {str(e)}")
            logger.error(traceback.format_exc())

def schedule_analysis():
    logger.info("⏰ 定时分析调度器启动")
    now = datetime.now(timezone.utc)
    next_time = get_next_update_time()
    initial_wait = (next_time - now).total_seconds()
    logger.info(f"⏳ 首次分析将在 {initial_wait:.1f} 秒后开始 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})...")
    time.sleep(max(0, initial_wait))

    while True:
        analysis_start = datetime.now(timezone.utc)
        logger.info(f"🔔 触发定时分析任务 ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')}")
        analysis_queue.put("ANALYZE")
        
        analysis_duration = (datetime.now(timezone.utc) - analysis_start).total_seconds()
        now = datetime.now(timezone.utc)
        next_time = get_next_update_time()
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
                if 'key_resistance' not in coin:
                    coin['key_resistance'] = 0
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
        
        logger.info(f"📦 返回数据: 日线上涨 {len(data['daily_rising'])}个, 全周期上涨 {len(data['all_cycle_rising'])}个")
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
    logger.info(f"🔑 API密钥: {API_KEY[:5]}...{API_KEY[-3:] if API_KEY else '未设置'}")
    logger.info(f"🌐 服务端口: {PORT}")
    logger.info("💾 数据存储: 内存存储 (无持久化)")
    logger.info("=" * 50)
    
    if start_background_threads():
        logger.info("🚀 启动服务器...")
        app.run(host='0.0.0.0', port=PORT, debug=False)
    else:
        logger.critical("🔥 无法启动服务，请检查错误日志")
