#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import traceback

# 完全移除 TA-Lib 依赖
talib = None
print("ℹ️ 使用内置技术指标计算函数，无需外部依赖")

import time
import re
import json
import math
import requests
import threading
import queue
import logging
import urllib3
import numpy as np
import sqlite3
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_from_directory
from binance.client import Client
from flask_cors import CORS
from collections import OrderedDict
from contextlib import contextmanager
from functools import wraps
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 禁用不必要的警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 配置类
class Config:
    PORT = int(os.environ.get("PORT", 9600))
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
    API_KEY = os.environ.get('BINANCE_API_KEY', '')
    API_SECRET = os.environ.get('BINANCE_API_SECRET', '')
    
    # 缓存配置
    CACHE_CONFIG = {
        'oi': {
            '5m': 5 * 60, '15m': 15 * 60, '30m': 30 * 60,
            '1h': 60 * 60, '2h': 2 * 60 * 60, '4h': 4 * 60 * 60,
            '6h': 6 * 60 * 60, '12h': 12 * 60 * 60, '1d': 24 * 60 * 60
        },
        'resistance': 24 * 3600,
        'volume': 5 * 60
    }
    
    # 分析配置
    VOLUME_THRESHOLD = 10000000  # 1000万USDT
    MAX_WORKERS = 10
    REQUEST_TIMEOUT = 30
    MAX_RETRIES = 3
    RETRY_DELAY = 5

config = Config()

# 设置日志级别
root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, config.LOG_LEVEL))

# 创建日志处理器
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(getattr(logging, config.LOG_LEVEL))
file_handler = logging.FileHandler('app.log')
file_handler.setLevel(getattr(logging, config.LOG_LEVEL))

# 日志格式
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

# 添加处理器
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)
logger.info(f"✅ 日志级别设置为: {config.LOG_LEVEL}")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='/static')
# 扩展 CORS 配置
CORS(app, resources={
    r"/api/*": {"origins": "*"},
    r"/static/*": {"origins": "*"}
})

# Binance API 配置
client = None

# 数据缓存 - 优化缓存结构
data_cache = {
    "last_updated": "从未更新",
    "daily_rising": [],
    "short_term_active": [],
    "all_cycle_rising": [],
    "analysis_time": 0,
    "next_analysis_time": "计算中..."
}

current_data_cache = data_cache.copy()

# LRU缓存实现
class LRUCache:
    def __init__(self, capacity: int):
        self.cache = OrderedDict()
        self.capacity = capacity
        self.lock = threading.RLock()
    
    def get(self, key):
        with self.lock:
            if key not in self.cache:
                return None
            self.cache.move_to_end(key)
            return self.cache[key]
    
    def put(self, key, value):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            if len(self.cache) > self.capacity:
                self.cache.popitem(last=False)
    
    def clear_expired(self, expiration_check_func):
        with self.lock:
            expired_keys = [
                key for key, value in self.cache.items()
                if expiration_check_func(value)
            ]
            for key in expired_keys:
                del self.cache[key]
            return len(expired_keys)
    
    def __len__(self):
        return len(self.cache)

# 优化的缓存结构
oi_data_cache = LRUCache(capacity=1000)
resistance_cache = LRUCache(capacity=500)
symbol_volume_cache = LRUCache(capacity=500)

# 使用队列进行线程间通信
analysis_queue = queue.Queue()
executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)

# 只保留有效的9个周期
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

VALID_PERIODS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']
RESISTANCE_INTERVALS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']

# 性能监控装饰器
def timing_decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            return result
        finally:
            execution_time = time.time() - start_time
            if execution_time > 1.0:  # 记录超过1秒的操作
                logger.warning(f"{func.__name__} 执行时间: {execution_time:.2f}秒")
    return wrapper

# 数据库管理
class AnalysisDatabase:
    def __init__(self, db_path="analysis.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS analysis_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL,
                    oi_value REAL,
                    is_highest BOOLEAN,
                    analysis_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, period, analysis_time)
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS resistance_levels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    level_type TEXT NOT NULL,
                    price REAL,
                    strength REAL,
                    test_count INTEGER,
                    analysis_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
    
    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def save_analysis_result(self, symbol, period, oi_value, is_highest):
        with self._get_connection() as conn:
            conn.execute('''
                INSERT INTO analysis_results (symbol, period, oi_value, is_highest)
                VALUES (?, ?, ?, ?)
            ''', (symbol, period, oi_value, is_highest))

# 创建数据库实例
db = AnalysisDatabase()

# Binance客户端单例
class BinanceClient:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._client = None
            cls._instance._initialized = False
        return cls._instance
    
    def initialize(self):
        if not self._initialized and config.API_KEY and config.API_SECRET:
            max_retries = config.MAX_RETRIES
            retry_delay = config.RETRY_DELAY
            
            for attempt in range(max_retries):
                try:
                    logger.info(f"🔧 尝试初始化Binance客户端 (第{attempt+1}次)...")
                    self._client = Client(
                        api_key=config.API_KEY, 
                        api_secret=config.API_SECRET,
                        requests_params={'timeout': config.REQUEST_TIMEOUT}
                    )
                    
                    server_time = self._client.get_server_time()
                    logger.info(f"✅ Binance客户端初始化成功，服务器时间: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
                    self._initialized = True
                    return True
                except Exception as e:
                    logger.error(f"❌ 初始化Binance客户端失败: {str(e)}")
                    if attempt < max_retries - 1:
                        logger.info(f"🔄 {retry_delay}秒后重试初始化客户端...")
                        time.sleep(retry_delay)
            logger.critical("🔥 无法初始化Binance客户端，已达到最大重试次数")
        return self._initialized
    
    @property
    def client(self):
        if not self._initialized:
            if not self.initialize():
                raise RuntimeError("Binance client not initialized")
        return self._client

# 创建Binance客户端实例
binance_client = BinanceClient()

def init_client():
    return binance_client.initialize()

def get_next_update_time(period):
    tz_shanghai = timezone(timedelta(hours=8))
    now = datetime.now(tz_shanghai)
    minutes = PERIOD_MINUTES.get(period, 5)
    
    if period.endswith('m'):
        period_minutes = int(period[:-1])
        current_minute = now.minute
        current_period_minute = (current_minute // period_minutes) * period_minutes
        next_update = now.replace(minute=current_period_minute, second=2, microsecond=0) + timedelta(minutes=period_minutes)
        if next_update < now:
            next_update += timedelta(minutes=period_minutes)
    elif period.endswith('h'):
        period_hours = int(period[:-1])
        current_hour = now.hour
        current_period_hour = (current_hour // period_hours) * period_hours
        next_update = now.replace(hour=current_period_hour, minute=0, second=2, microsecond=0) + timedelta(hours=period_hours)
    else:
        next_update = now.replace(hour=0, minute=0, second=2, microsecond=0) + timedelta(days=1)

    return next_update

def cleanup_cache():
    """清理不符合条件的缓存数据"""
    try:
        current_time = datetime.now(timezone.utc)
        cleaned_count = 0
        
        # 清理持仓量缓存
        def oi_expiration_check(cached_data):
            return 'expiration' in cached_data and cached_data['expiration'] <= current_time
        
        cleaned_count += oi_data_cache.clear_expired(oi_expiration_check)
        
        # 清理低交易量币种的阻力位缓存
        for cache_key in list(resistance_cache.cache.keys()):
            symbol = cache_key.replace('_resistance', '')
            volume_data = symbol_volume_cache.get(symbol)
            if volume_data and (volume_data.get('expiration', current_time) <= current_time or 
                volume_data.get('volume', 0) < config.VOLUME_THRESHOLD):
                resistance_cache.cache.pop(cache_key, None)
                cleaned_count += 1
        
        logger.info(f"🧹 缓存清理完成，清理了 {cleaned_count} 个过期或无效条目")
        return cleaned_count
    except Exception as e:
        logger.error(f"❌ 缓存清理失败: {str(e)}")
        return 0

@timing_decorator
def get_symbol_volume(symbol):
    """获取币种交易量并缓存"""
    try:
        current_time = datetime.now(timezone.utc)
        
        cached_data = symbol_volume_cache.get(symbol)
        if cached_data and cached_data['expiration'] > current_time:
            return cached_data['volume']
        
        if not binance_client.initialize():
            return 0
            
        ticker = binance_client.client.futures_ticker(symbol=symbol)
        volume = float(ticker.get('quoteVolume', 0))
        
        symbol_volume_cache.put(symbol, {
            'volume': volume,
            'expiration': current_time + timedelta(seconds=config.CACHE_CONFIG['volume'])
        })
        
        return volume
    except Exception as e:
        logger.error(f"❌ 获取{symbol}交易量失败: {str(e)}")
        return 0

def get_open_interest_with_retry(symbol, period, max_retries=3):
    """带重试机制的持仓量获取"""
    for attempt in range(max_retries):
        try:
            return get_open_interest(symbol, period)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == max_retries - 1:
                raise
            wait_time = (attempt + 1) * 2  # 指数退避
            logger.warning(f"第{attempt+1}次重试获取{symbol}的{period}持仓量，等待{wait_time}秒")
            time.sleep(wait_time)

@timing_decorator
def get_open_interest(symbol, period, use_cache=True):
    """获取持仓量数据，支持智能缓存"""
    try:
        if not re.match(r"^[A-Z0-9]{1,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return {'series': [], 'timestamps': []}

        if period not in VALID_PERIODS:
            logger.warning(f"⚠️ 不支持的周期: {period}")
            return {'series': [], 'timestamps': []}
        
        current_time = datetime.now(timezone.utc)
        cache_key = f"{symbol}_{period}"
        
        # 检查缓存
        if use_cache:
            cached_data = oi_data_cache.get(cache_key)
            if cached_data and 'expiration' in cached_data and cached_data['expiration'] > current_time:
                logger.debug(f"📈 使用缓存数据: {symbol} {period}")
                return cached_data['data']
            
            # 缓存过期但还在获取新数据的时间窗口内，暂时使用缓存
            next_update_time = get_next_update_time(period)
            time_until_update = (next_update_time - current_time).total_seconds()
            
            if time_until_update > 0 and time_until_update < 300 and cached_data:  # 5分钟内
                logger.info(f"⏳ {symbol} {period} 缓存过期，但接近更新时间，暂时使用缓存")
                return cached_data['data']

        # 获取新数据
        logger.info(f"📡 请求持仓量数据: symbol={symbol}, period={period}")
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}

        # 使用会话和重试策略
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        response = session.get(url, params=params, timeout=15)
        logger.debug(f"📡 响应状态: {response.status_code}")

        if response.status_code != 200:
            logger.error(f"❌ 获取{symbol}的{period}持仓量失败: HTTP {response.status_code}")
            # 返回缓存数据（如果有）
            cached_data = oi_data_cache.get(cache_key)
            if cached_data:
                return cached_data['data']
            return {'series': [], 'timestamps': []}

        data = response.json()
        logger.debug(f"📡 获取到 {len(data)} 条持仓量数据")

        if not isinstance(data, list) or len(data) == 0:
            logger.warning(f"⚠️ {symbol}的{period}持仓量数据为空")
            # 返回缓存数据（如果有）
            cached_data = oi_data_cache.get(cache_key)
            if cached_data:
                return cached_data['data']
            return {'series': [], 'timestamps': []}
            
        data.sort(key=lambda x: x['timestamp'])
        oi_series = [float(item['sumOpenInterest']) for item in data]
        timestamps = [item['timestamp'] for item in data]

        if len(oi_series) < 30:
            logger.warning(f"⚠️ {symbol}的{period}持仓量数据不足30个点")
            # 返回缓存数据（如果有）
            cached_data = oi_data_cache.get(cache_key)
            if cached_data:
                return cached_data['data']
            return {'series': [], 'timestamps': []}
            
        oi_data = {
            'series': oi_series, 
            'timestamps': timestamps
        }
        
        # 设置缓存过期时间
        expiration = current_time + timedelta(seconds=config.CACHE_CONFIG['oi'].get(period, 5*60))
        oi_data_cache.put(cache_key, {
            'data': oi_data,
            'expiration': expiration
        })

        # 保存到数据库
        try:
            db.save_analysis_result(symbol, period, oi_series[-1] if oi_series else 0, True)
        except Exception as e:
            logger.warning(f"保存分析结果到数据库失败: {str(e)}")

        logger.info(f"📈 获取新数据: {symbol} {period} ({len(oi_series)}点)")
        return oi_data
    except Exception as e:
        logger.error(f"❌ 获取{symbol}的{period}持仓量失败: {str(e)}")
        # 返回缓存数据（如果有）
        cached_data = oi_data_cache.get(cache_key)
        if cached_data:
            return cached_data['data']
        logger.error(traceback.format_exc())
        return {'series': [], 'timestamps': []}

def is_latest_highest(oi_data):
    if not oi_data or len(oi_data) < 30:
        logger.debug("持仓量数据不足30个点")
        return False

    latest_value = oi_data[-1]
    prev_data = oi_data[-30:-1]
    
    return latest_value > max(prev_data) if prev_data else False

@timing_decorator
def detect_price_reaction_levels(symbol, interval):
    """基于价格反应检测阻力位和支撑位"""
    try:
        logger.info(f"📊 分析价格反应: {symbol} {interval}")
        
        # 获取K线数据
        klines = binance_client.client.futures_klines(symbol=symbol, interval=interval, limit=200)
        if not klines or len(klines) < 100:
            return [], []
        
        high_prices = [float(k[2]) for k in klines]
        low_prices = [float(k[3]) for k in klines]
        close_prices = [float(k[4]) for k in klines]
        
        # 检测局部高点和低点
        resistance_levels = []
        support_levels = []
        
        # 检测阻力位（价格多次上涨至此区域后回落）
        for i in range(2, len(high_prices)-2):
            if (high_prices[i] > high_prices[i-1] and 
                high_prices[i] > high_prices[i-2] and 
                high_prices[i] > high_prices[i+1] and 
                high_prices[i] > high_prices[i+2]):
                
                # 检查该价格区域是否多次被测试
                test_count = 0
                price_tolerance = high_prices[i] * 0.002  # 0.2%的容差范围
                
                for j in range(len(high_prices)):
                    if abs(high_prices[j] - high_prices[i]) <= price_tolerance:
                        test_count += 1
                
                # 如果被测试多次且价格回落，则认为是阻力位
                if test_count >= 3:
                    # 计算强度基于测试次数和回落幅度
                    decline_after_test = 0
                    for j in range(i, min(i+5, len(close_prices))):
                        if close_prices[j] < high_prices[i]:
                            decline_after_test += 1
                    
                    strength = min(1.0, (test_count * 0.2 + decline_after_test * 0.1))
                    
                    resistance_levels.append({
                        'price': high_prices[i],
                        'strength': round(strength, 2),
                        'test_count': test_count,
                        'type': 'price_reaction'
                    })
        
        # 检测支撑位（价格多次下跌至此区域后反弹）
        for i in range(2, len(low_prices)-2):
            if (low_prices[i] < low_prices[i-1] and 
                low_prices[i] < low_prices[i-2] and 
                low_prices[i] < low_prices[i+1] and 
                low_prices[i] < low_prices[i+2]):
                
                # 检查该价格区域是否多次被测试
                test_count = 0
                price_tolerance = low_prices[i] * 0.002  # 0.2%的容差范围
                
                for j in range(len(low_prices)):
                    if abs(low_prices[j] - low_prices[i]) <= price_tolerance:
                        test_count += 1
                
                # 如果被测试多次且价格反弹，则认为是支撑位
                if test_count >= 3:
                    # 计算强度基于测试次数和反弹幅度
                    rise_after_test = 0
                    for j in range(i, min(i+5, len(close_prices))):
                        if close_prices[j] > low_prices[i]:
                            rise_after_test += 1
                    
                    strength = min(1.0, (test_count * 0.2 + rise_after_test * 0.1))
                    
                    support_levels.append({
                        'price': low_prices[i],
                        'strength': round(strength, 2),
                        'test_count': test_count,
                        'type': 'price_reaction'
                    })
        
        # 合并相近的水平
        resistance_levels = merge_similar_levels(resistance_levels)
        support_levels = merge_similar_levels(support_levels)
        
        # 按强度排序并返回前5个
        resistance_levels.sort(key=lambda x: x['strength'], reverse=True)
        support_levels.sort(key=lambda x: x['strength'], reverse=True)
        
        return resistance_levels[:5], support_levels[:5]
        
    except Exception as e:
        logger.error(f"价格反应分析失败: {str(e)}")
        return [], []

def merge_similar_levels(levels):
    """合并相近的价格水平"""
    if not levels:
        return []
    
    merged = []
    levels.sort(key=lambda x: x['price'])
    
    i = 0
    while i < len(levels):
        current = levels[i]
        group = [current]
        
        j = i + 1
        while j < len(levels) and abs(levels[j]['price'] - current['price']) <= current['price'] * 0.005:
            group.append(levels[j])
            j += 1
        
        # 选择组中强度最高的水平
        best_level = max(group, key=lambda x: x['strength'])
        # 合并测试次数
        total_tests = sum(level['test_count'] for level in group)
        best_level['test_count'] = total_tests
        # 重新计算强度
        best_level['strength'] = min(1.0, best_level['strength'] * (1 + 0.1 * (len(group) - 1)))
        
        merged.append(best_level)
        i = j
    
    return merged

@timing_decorator
def calculate_resistance_levels(symbol):
    try:
        logger.info(f"📊 计算阻力位: {symbol}")
        now = time.time()
        
        cache_key = f"{symbol}_resistance"
        cached_data = resistance_cache.get(cache_key)
        if cached_data and cached_data['expiration'] > now:
            logger.debug(f"📊 使用缓存的阻力位数据: {symbol}")
            return cached_data['levels']
        
        if not binance_client.initialize():
            logger.error("❌ 无法初始化Binance客户端，无法计算阻力位")
            return {'resistance': {}, 'support': {}, 'current_price': 0}
        
        try:
            ticker = binance_client.client.futures_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            logger.info(f"📊 {symbol}当前价格: {current_price}")
        except Exception as e:
            logger.error(f"❌ 获取{symbol}当前价格失败: {str(e)}")
            current_price = None
        
        interval_levels = {}
        
        for interval in RESISTANCE_INTERVALS:
            try:
                logger.info(f"📊 分析价格反应: {symbol} {interval}")
                resistance_levels, support_levels = detect_price_reaction_levels(symbol, interval)
                
                # 计算距离当前价格的百分比
                resistance_with_distance = []
                support_with_distance = []
                
                if current_price and current_price > 0:
                    for level in resistance_levels:
                        distance_percent = (level['price'] - current_price) / current_price * 100
                        resistance_with_distance.append({
                            'price': round(level['price'], 4),
                            'strength': level['strength'],
                            'distance_percent': round(distance_percent, 2),
                            'test_count': level['test_count'],
                            'type': level['type']
                        })
                    
                    for level in support_levels:
                        distance_percent = (level['price'] - current_price) / current_price * 100
                        support_with_distance.append({
                            'price': round(level['price'], 4),
                            'strength': level['strength'],
                            'distance_percent': round(distance_percent, 2),
                            'test_count': level['test_count'],
                            'type': level['type']
                        })
                
                interval_levels[interval] = {
                    'resistance': resistance_with_distance[:3],  # 只返回前3个
                    'support': support_with_distance[:3]         # 只返回前3个
                }
                
                logger.info(f"📊 {symbol}在{interval}的有效阻力位: {len(resistance_with_distance)}个, 支撑位: {len(support_with_distance)}个")
                
            except Exception as e:
                logger.error(f"计算{symbol}在{interval}的阻力位失败: {str(e)}")
                logger.error(traceback.format_exc())

        levels = {
            'levels': interval_levels,
            'current_price': current_price or 0
        }
        
        resistance_cache.put(cache_key, {
            'levels': levels,
            'expiration': now + config.CACHE_CONFIG['resistance']
        })
        return levels
    except Exception as e:
        logger.error(f"计算{symbol}的阻力位失败: {str(e)}")
        logger.error(traceback.format_exc())
        return {'levels': {}, 'current_price': 0}

@timing_decorator
def analyze_symbol(symbol):
    try:
        logger.info(f"🔍 开始分析币种: {symbol}")
        
        # 检查交易量，过滤低交易量币种
        volume = get_symbol_volume(symbol)
        if volume < config.VOLUME_THRESHOLD:
            logger.info(f"⏭️ 跳过低交易量币种: {symbol} (交易量: {volume:.0f})")
            return {
                'symbol': symbol,
                'period_status': {p: False for p in VALID_PERIODS},
                'period_count': 0
            }

        symbol_result = {
            'symbol': symbol,
            'daily_rising': None,
            'short_term_active': None,
            'all_cycle_rising': None,
            'period_status': {p: False for p in VALID_PERIODS},
            'period_count': 0
        }

        daily_oi = get_open_interest_with_retry(symbol, '1d')
        daily_series = daily_oi.get('series', [])
        
        if len(daily_series) >= 30:
            daily_status = is_latest_highest(daily_series)
            symbol_result['period_status']['1d'] = daily_status
            
            if daily_status:
                daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
                logger.info(f"📊 {symbol} 日线上涨条件满足，涨幅: {daily_change:.2f}%")
                
                daily_rising_item = {
                    'symbol': symbol,
                    'oi': daily_series[-1],
                    'change': round(daily_change, 2),
                    'period_status': symbol_result['period_status'].copy()
                }
                symbol_result['daily_rising'] = daily_rising_item
                symbol_result['period_count'] = 1

                logger.info(f"📊 开始全周期分析: {symbol}")
                all_intervals_up = True
                for period in VALID_PERIODS:
                    if period == '1d':
                        continue
                        
                    oi_data = get_open_interest_with_retry(symbol, period)
                    oi_series = oi_data.get('series', [])
                    
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
                        'period_count': symbol_result['period_count'],
                        'period_status': symbol_result['period_status'].copy()
                    }
                
                if symbol_result['daily_rising']:
                    symbol_result['daily_rising']['period_status'] = symbol_result['period_status'].copy()
                    symbol_result['daily_rising']['period_count'] = symbol_result['period_count']

        min5_oi = get_open_interest_with_retry(symbol, '5m')
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
                        'ratio': round(ratio, 2)
                    }

        logger.info(f"✅ 完成分析币种: {symbol}")
        return symbol_result
    except Exception as e:
        logger.error(f"❌ 处理{symbol}时出错: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            'symbol': symbol,
            'period_status': {p: False for p in VALID_PERIODS},
            'period_count': 0
        }

@timing_decorator
def analyze_trends():
    start_time = time.time()
    logger.info("🔍 开始分析币种趋势...")
    
    # 清理过期缓存
    cleanup_cache()
    
    symbols = get_high_volume_symbols()
    
    if not symbols:
        logger.warning("⚠️ 没有找到高交易量币种")
        return data_cache

    logger.info(f"🔍 开始分析 {len(symbols)} 个币种")

    daily_rising = []
    short_term_active = []
    all_cycle_rising = []

    futures = [executor.submit(analyze_symbol, symbol) for symbol in symbols]
    
    for future in as_completed(futures):
        try:
            result = future.result()
            if result.get('daily_rising'):
                daily_rising.append(result['daily_rising'])
            if result.get('short_term_active'):
                short_term_active.append(result['short_term_active'])
            if result.get('all_cycle_rising'):
                all_cycle_rising.append(result['all_cycle_rising'])
        except Exception as e:
            logger.error(f"❌ 处理币种时出错: {str(e)}")

    daily_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)
    short_term_active.sort(key=lambda x: x.get('ratio', 0), reverse=True)
    all_cycle_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)

    analysis_time = time.time() - start_time
    logger.info(f"📊 分析结果: 日线上涨 {len(daily_rising)}个, 短期活跃 {len(short_term_active)}个, 全部周期上涨 {len(all_cycle_rising)}个")
    logger.info(f"✅ 分析完成: 用时{analysis_time:.2f}秒")

    tz_shanghai = timezone(timedelta(hours=8))
    return {
        'daily_rising': daily_rising,
        'short_term_active': short_term_active,
        'all_cycle_rising': all_cycle_rising,
        'analysis_time': analysis_time,
        'last_updated': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
        'next_analysis_time': get_next_update_time('5m').strftime("%Y-%m-%d %H:%M:%S")
    }

def get_high_volume_symbols():
    if not binance_client.initialize():
        logger.error("❌ 无法连接API")
        return []

    try:
        logger.info("📊 获取高交易量币种...")
        tickers = binance_client.client.futures_ticker()
        filtered = [
            t for t in tickers if float(t.get('quoteVolume', 0)) > config.VOLUME_THRESHOLD
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
                analysis_queue.task_done()
                break

            analysis_start = datetime.now(timezone.utc)
            logger.info(f"⏱️ 开始更新数据 ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')})...")

            backup_cache = data_cache.copy()
            current_backup = current_data_cache.copy()

            try:
                result = analyze_trends()
                
                new_data = {
                    "last_updated": result['last_updated'],
                    "daily_rising": result['daily_rising'],
                    "short_term_active": result['short_term_active'],
                    "all_cycle_rising": result['all_cycle_rising'],
                    "analysis_time": result['analysis_time'],
                    "next_analysis_time": result['next_analysis_time']
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
            
            next_time = get_next_update_time('5m')
            wait_seconds = (next_time - analysis_end).total_seconds()
            logger.info(f"⏳ 下次分析将在 {wait_seconds:.1f} 秒后 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})")
            
            logger.info("=" * 50)
            
            analysis_queue.task_done()
        except Exception as e:
            logger.error(f"❌ 分析失败: {str(e)}")
            logger.error(traceback.format_exc())
            analysis_queue.task_done()

def schedule_analysis():
    logger.info("⏰ 定时分析调度器启动")
    now = datetime.now(timezone.utc)
    next_time = get_next_update_time('5m')
    initial_wait = (next_time - now).total_seconds()
    logger.info(f"⏳ 首次分析将在 {initial_wait:.1f} 秒后开始 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})...")
    time.sleep(max(0, initial_wait))

    while True:
        analysis_start = datetime.now(timezone.utc)
        logger.info(f"🔔 触发定时分析任务 ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')}")
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

@app.route('/api/data', methods=['GET'])
def get_data():
    global current_data_cache
    try:
        logger.info("📡 收到 /api/data 请求")
        
        # 强制刷新数据如果缓存为空
        if not current_data_cache or current_data_cache.get("last_updated") == "从未更新":
            logger.info("🔄 缓存为空，触发即时分析")
            analysis_queue.put("ANALYZE")
            return jsonify({
                'last_updated': "数据生成中...",
                'daily_rising': [],
                'short_term_active': [],
                'all_cycle_rising': [],
                'analysis_time': 0,
                'next_analysis_time': "计算中..."
            })
        
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
        
        daily_rising = validate_coins(current_data_cache.get('daily_rising', []))
        short_term_active = validate_coins(current_data_cache.get('short_term_active', []))
        all_cycle_rising = validate_coins(current_data_cache.get('all_cycle_rising', []))
        
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
        
        logger.info(f"📦 返回数据: 日线上涨 {len(filtered_daily_rising)}个, 全周期上涨 {len(all_cycle_rising)}个")
        return jsonify(data)
    
    except Exception as e:
        logger.error(f"❌ 获取数据失败: {str(e)}")
        tz_shanghai = timezone(timedelta(hours=8))
        return jsonify({
            'last_updated': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
            'daily_rising': [],
            'short_term_active': [],
            'all_cycle_rising': [],
            'analysis_time': 0,
            'next_analysis_time': get_next_update_time('5m').strftime("%Y-%m-%d %H:%M:%S")
        })

@app.route('/api/resistance_levels/<symbol>', methods=['GET'])
def get_resistance_levels(symbol):
    try:
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        logger.info(f"📊 获取阻力位数据: {symbol}")
        levels = calculate_resistance_levels(symbol)
        
        if not isinstance(levels, dict):
            logger.error(f"❌ 阻力位数据结构错误: {type(levels)}")
            return jsonify({'error': 'Invalid resistance data structure'}), 500
        
        if 'levels' not in levels:
            levels['levels'] = {}
        if 'current_price' not in levels:
            levels['current_price'] = 0
        
        return jsonify(levels)
    except Exception as e:
        logger.error(f"❌ 获取阻力位数据失败: {symbol}, {str(e)}")
        return jsonify({
            'levels': {},
            'current_price': 0,
            'error': str(e)
        }), 500

@app.route('/api/oi_chart/<symbol>/<period>', methods=['GET'])
def get_oi_chart_data(symbol, period):
    try:
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        if period not in VALID_PERIODS:
            logger.warning(f"⚠ 不支持的周期: {period}")
            return jsonify({'error': 'Unsupported period'}), 400

        logger.info(f"📈 获取持仓量图表数据: symbol={symbol}, period={period}")
        oi_data = get_open_interest_with_retry(symbol, period, use_cache=True)
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
        binance_status = 'ok'
        if binance_client._initialized:
            try:
                binance_client.client.get_server_time()
            except:
                binance_status = 'error'
        else:
            binance_status = 'not initialized'
        
        tz_shanghai = timezone(timedelta(hours=8))
        return jsonify({
            'status': 'healthy',
            'binance': binance_status,
            'last_updated': current_data_cache.get('last_updated', 'N/A'),
            'next_analysis_time': current_data_cache.get('next_analysis_time', 'N/A'),
            'server_time': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
            'cache_stats': {
                'oi_cache_size': len(oi_data_cache),
                'resistance_cache_size': len(resistance_cache),
                'volume_cache_size': len(symbol_volume_cache)
            },
            'config': {
                'volume_threshold': config.VOLUME_THRESHOLD,
                'max_workers': config.MAX_WORKERS
            }
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

@app.route('/api/status')
def status():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': '1.0.0',
        'cache_stats': {
            'oi_cache': len(oi_data_cache),
            'resistance_cache': len(resistance_cache),
            'volume_cache': len(symbol_volume_cache)
        }
    })

def start_background_threads():
    static_path = app.static_folder
    if not os.path.exists(static_path):
        os.makedirs(static_path)
    
    index_path = os.path.join(static_path, 'index.html')
    if not os.path.exists(index_path):
        with open(index_path, 'w') as f:
            f.write("<html><body><h1>请将前端文件放入static目录</h1></body></html>")
    
    if not binance_client.initialize():
        logger.critical("❌ 无法初始化客户端")
        return False
    
    global current_data_cache
    tz_shanghai = timezone(timedelta(hours=8))
    current_data_cache = {
        "last_updated": "等待首次分析",
        "daily_rising": [],
        "short_term_active": [],
        "all_cycle_rising": [],
        "analysis_time": 0,
        "next_analysis_time": get_next_update_time('5m').strftime("%Y-%m-%d %H:%M:%S")
    }
    logger.info("🆕 创建初始内存数据记录")
    
    worker_thread = threading.Thread(target=analysis_worker, name="AnalysisWorker")
    worker_thread.daemon = True
    worker_thread.start()
    
    scheduler_thread = threading.Thread(target=schedule_analysis, name="AnalysisScheduler")
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    # 添加初始分析任务
    analysis_queue.put("ANALYZE")
    logger.info("🔄 已提交初始分析任务")
    
    logger.info("✅ 后台线程启动成功")
    return True

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info(f"🚀 启动加密货币持仓量分析服务")
    logger.info(f"🔑 API密钥: {config.API_KEY[:5]}...{config.API_KEY[-3:] if config.API_KEY else '未设置'}")
    logger.info(f"🌐 服务端口: {config.PORT}")
    logger.info("💾 数据存储: 内存存储 + SQLite持久化")
    logger.info("=" * 50)
    
    if start_background_threads():
        logger.info("🚀 启动服务器...")
        app.run(host='0.0.0.0', port=config.PORT, debug=False)
    else:
        logger.critical("🔥 无法启动服务，请检查错误日志")
