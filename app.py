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
import urllib3
import asyncio
import aiohttp
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_from_directory

# 检查是否在 Render 环境
IS_RENDER = os.environ.get('RENDER', False)

# 简化日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# 禁用不必要的警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='/')

# 延迟导入可能耗时的模块
try:
    from binance.client import Client
    from flask_cors import CORS
    CORS(app)
    BINANCE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"❌ 部分依赖不可用: {e}")
    BINANCE_AVAILABLE = False

# Binance API 配置
API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '')

# 邮件配置
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.qq.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
EMAIL_USER = os.environ.get('EMAIL_USER', '')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', '')
EMAIL_RECEIVERS = os.environ.get('EMAIL_RECEIVERS', '').split(',')  # 多个邮箱用逗号分隔

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
OI_CACHE_EXPIRATION = 5 * 60

# 使用队列进行线程间通信
analysis_queue = queue.Queue()
executor = ThreadPoolExecutor(max_workers=5)

# 周期配置
PERIOD_MINUTES = {
    '5m': 5, '15m': 15, '30m': 30, '1h': 60, '2h': 120,
    '4h': 240, '6h': 360, '12h': 720, '1d': 1440
}

VALID_PERIODS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']
RESISTANCE_INTERVALS = ['1m', '15m', '1d']

# 分析线程状态
analysis_thread_running = False
analysis_thread = None

# 异步HTTP客户端会话
aio_session = None

def init_client():
    """初始化 Binance 客户端"""
    global client
    
    if not BINANCE_AVAILABLE:
        logger.error("❌ Binance 客户端不可用，缺少依赖")
        return False
        
    if not API_KEY or not API_SECRET:
        logger.error("❌ Binance API密钥未设置")
        return False
    
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            logger.info(f"🔧 尝试初始化Binance客户端 (第{attempt+1}次)...")
            client = Client(
                api_key=API_KEY, 
                api_secret=API_SECRET,
                requests_params={'timeout': 20}
            )
            
            server_time = client.get_server_time()
            logger.info(f"✅ Binance客户端初始化成功")
            return True
        except Exception as e:
            logger.error(f"❌ 初始化Binance客户端失败: {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"🔄 {retry_delay}秒后重试...")
                time.sleep(retry_delay)
    
    logger.critical("🔥 无法初始化Binance客户端")
    return False

def send_email_notification(all_cycle_rising_coins):
    """发送邮件提醒"""
    try:
        if not EMAIL_USER or not EMAIL_PASSWORD or not EMAIL_RECEIVERS:
            logger.warning("⚠️ 邮件配置不完整，跳过发送邮件")
            return False
        
        if not all_cycle_rising_coins:
            logger.info("📧 没有全部周期上涨的币种，不发送邮件")
            return False
        
        # 准备邮件内容
        subject = "出现全部周期上涨币种"
        
        # 构建币种列表
        coin_list = "\n".join([f"• {coin['symbol']}" for coin in all_cycle_rising_coins])
        
        body = f"""
发现 {len(all_cycle_rising_coins)} 个全部周期上涨的币种：

{coin_list}

分析时间: {datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")}
        
请注意：此提醒仅基于持仓量分析，投资有风险，请谨慎决策。
        """
        
        # 创建邮件
        msg = MIMEMultipart()
        msg['From'] = EMAIL_USER
        msg['To'] = ", ".join(EMAIL_RECEIVERS)
        msg['Subject'] = subject
        
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        # 发送邮件
        server = None
        try:
            if SMTP_PORT == 587:
                # TLS 连接
                server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
                server.starttls()
            elif SMTP_PORT == 465:
                # SSL 连接
                server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
            else:
                server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USER, EMAIL_RECEIVERS, msg.as_string())
            server.quit()
            
            logger.info(f"✅ 邮件发送成功，收件人: {len(EMAIL_RECEIVERS)} 个")
            return True
            
        except Exception as e:
            logger.error(f"❌ 邮件发送失败: {str(e)}")
            if server:
                server.quit()
            return False
            
    except Exception as e:
        logger.error(f"❌ 邮件通知处理失败: {str(e)}")
        return False

async def init_aio_session():
    """初始化异步HTTP会话"""
    global aio_session
    if aio_session is None:
        timeout = aiohttp.ClientTimeout(total=30)
        connector = aiohttp.TCPConnector(limit=20, limit_per_host=10)
        aio_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return aio_session

async def close_aio_session():
    """关闭异步HTTP会话"""
    global aio_session
    if aio_session:
        await aio_session.close()
        aio_session = None

async def get_open_interest_async(symbol, period, use_cache=True):
    """异步获取持仓量数据"""
    try:
        if not re.match(r"^[A-Z0-9]{1,10}USDT$", symbol):
            return {'series': [], 'timestamps': []}

        if period not in VALID_PERIODS:
            return {'series': [], 'timestamps': []}
        
        current_time = datetime.now(timezone.utc)
        cache_key = f"{symbol}_{period}"
        
        if use_cache and cache_key in oi_data_cache:
            cached_data = oi_data_cache[cache_key]
            if 'expiration' in cached_data and cached_data['expiration'] > current_time:
                return cached_data['data']

        logger.debug(f"📡 异步请求持仓量数据: {symbol} {period}")
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}

        session = await init_aio_session()
        
        async with session.get(url, params=params, headers={'X-MBX-APIKEY': API_KEY} if API_KEY else {}) as response:
            if response.status != 200:
                return {'series': [], 'timestamps': []}

            data = await response.json()
            
            if not isinstance(data, list) or len(data) == 0:
                return {'series': [], 'timestamps': []}
                
            data.sort(key=lambda x: x['timestamp'])
            oi_series = [float(item['sumOpenInterest']) for item in data]
            timestamps = [item['timestamp'] for item in data]

            if len(oi_series) < 30:
                return {'series': [], 'timestamps': []}
                
            oi_data = {
                'series': oi_series, 
                'timestamps': timestamps
            }
            
            expiration = current_time + timedelta(seconds=OI_CACHE_EXPIRATION)
            oi_data_cache[cache_key] = {
                'data': oi_data,
                'expiration': expiration
            }

            return oi_data
    except Exception as e:
        logger.error(f"❌ 异步获取{symbol}的{period}持仓量失败: {str(e)}")
        return {'series': [], 'timestamps': []}

def get_next_analysis_time():
    """计算下次分析时间（5分钟周期+45秒延迟）"""
    tz_shanghai = timezone(timedelta(hours=8))
    now = datetime.now(tz_shanghai)
    
    # 计算当前5分钟周期的开始时间
    current_minute = now.minute
    current_period_minute = (current_minute // 5) * 5
    
    # 当前5分钟周期的结束时间
    current_period_end = now.replace(minute=current_period_minute, second=0, microsecond=0) + timedelta(minutes=5)
    
    # 下次分析时间 = 当前周期结束时间 + 45秒延迟
    next_analysis = current_period_end + timedelta(seconds=45)
    
    # 如果下次分析时间已经过去，则计算下一个周期
    if next_analysis <= now:
        next_analysis = current_period_end + timedelta(minutes=5, seconds=45)
    
    return next_analysis

def is_latest_highest(oi_series):
    """检查是否为近期最高点"""
    if not oi_series or len(oi_series) < 30:
        return False

    latest_value = oi_series[-1]
    prev_data = oi_series[-30:-1]
    
    return latest_value > max(prev_data) if prev_data else False

async def analyze_short_term_active_batch(symbols_batch):
    """批量分析短期活跃币种"""
    tasks = []
    for symbol in symbols_batch:
        tasks.append(analyze_short_term_active(symbol))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    short_term_active = []
    for result in results:
        if isinstance(result, dict) and result:
            short_term_active.append(result)
        elif isinstance(result, Exception):
            logger.error(f"❌ 短期活跃分析异常: {result}")
    
    return short_term_active

async def analyze_short_term_active(symbol):
    """分析短期活跃币种"""
    try:
        # 并行获取5分钟和日线数据
        min5_task = get_open_interest_async(symbol, '5m')
        daily_task = get_open_interest_async(symbol, '1d')
        
        min5_oi, daily_oi = await asyncio.gather(min5_task, daily_task)
        
        min5_series = min5_oi.get('series', [])
        daily_series = daily_oi.get('series', [])
        
        if len(min5_series) >= 30 and len(daily_series) >= 30:
            min5_max = max(min5_series[-30:])
            daily_avg = sum(daily_series[-30:]) / 30
            
            if min5_max > 0 and daily_avg > 0:
                ratio = min5_max / daily_avg
                
                if ratio > 1.5:
                    return {
                        'symbol': symbol,
                        'oi': min5_series[-1],
                        'ratio': round(ratio, 2)
                    }
        return None
    except Exception as e:
        logger.error(f"❌ 分析{symbol}短期活跃失败: {str(e)}")
        return None

async def analyze_daily_rising_batch(symbols_batch):
    """批量分析日线上涨币种"""
    tasks = []
    for symbol in symbols_batch:
        tasks.append(analyze_daily_rising(symbol))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    daily_rising = []
    for result in results:
        if isinstance(result, dict) and result:
            daily_rising.append(result)
        elif isinstance(result, Exception):
            logger.error(f"❌ 日线上涨分析异常: {result}")
    
    return daily_rising

async def analyze_daily_rising(symbol):
    """分析日线上涨币种"""
    try:
        daily_oi = await get_open_interest_async(symbol, '1d')
        daily_series = daily_oi.get('series', [])
        
        if len(daily_series) >= 30:
            daily_status = is_latest_highest(daily_series)
            
            if daily_status:
                daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
                
                return {
                    'symbol': symbol,
                    'oi': daily_series[-1],
                    'change': round(daily_change, 2),
                    'period_status': {'1d': True},
                    'period_count': 1
                }
        return None
    except Exception as e:
        logger.error(f"❌ 分析{symbol}日线上涨失败: {str(e)}")
        return None

async def analyze_all_cycle_rising_batch(daily_results_batch):
    """批量分析全部周期上涨币种"""
    tasks = []
    for daily_data in daily_results_batch:
        tasks.append(analyze_all_cycle_rising(daily_data['symbol'], daily_data))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_cycle_rising = []
    for result in results:
        if isinstance(result, dict) and result:
            all_cycle_rising.append(result)
        elif isinstance(result, Exception):
            logger.error(f"❌ 全部周期分析异常: {result}")
    
    return all_cycle_rising

async def analyze_all_cycle_rising(symbol, daily_data):
    """分析全部周期上涨币种（基于日线上涨币种）"""
    try:
        if not daily_data:
            return None
            
        period_status = daily_data['period_status'].copy()
        period_count = daily_data['period_count']
        all_intervals_up = True
        
        # 并行获取所有周期的数据
        tasks = []
        periods_to_analyze = [p for p in VALID_PERIODS if p != '1d']
        
        for period in periods_to_analyze:
            tasks.append(get_open_interest_async(symbol, period))
        
        oi_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理结果
        for i, period in enumerate(periods_to_analyze):
            oi_result = oi_results[i]
            if isinstance(oi_result, Exception):
                logger.error(f"❌ 获取{symbol}的{period}数据失败: {oi_result}")
                status = False
            else:
                oi_series = oi_result.get('series', [])
                status = len(oi_series) >= 30 and is_latest_highest(oi_series)
            
            period_status[period] = status
            
            if status:
                period_count += 1
            else:
                all_intervals_up = False
        
        if all_intervals_up:
            return {
                'symbol': symbol,
                'oi': daily_data['oi'],
                'change': daily_data['change'],
                'period_count': period_count,
                'period_status': period_status
            }
        
        # 即使不是全部周期上涨，也更新日线数据的周期状态
        daily_data['period_status'] = period_status
        daily_data['period_count'] = period_count
        
        return None
    except Exception as e:
        logger.error(f"❌ 分析{symbol}全部周期上涨失败: {str(e)}")
        return None

def get_high_volume_symbols():
    """获取高交易量币种"""
    if client is None and not init_client():
        logger.warning("❌ 无法获取高交易量币种：客户端未初始化")
        return []

    try:
        tickers = client.futures_ticker()
        # 获取所有高交易量币种
        filtered = [
            t for t in tickers if float(t.get('quoteVolume', 0)) > 10000000
            and t.get('symbol', '').endswith('USDT')
        ]
        symbols = [t['symbol'] for t in filtered]
        logger.info(f"📊 获取到 {len(symbols)} 个高交易量币种")
        return symbols
    except Exception as e:
        logger.error(f"❌ 获取高交易量币种失败: {str(e)}")
        return []

def split_into_batches(items, batch_size):
    """将列表分割成批次"""
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]

async def analyze_trends_async():
    """异步趋势分析逻辑"""
    start_time = time.time()
    logger.info("🔍 开始异步分析币种趋势...")
    
    # 步骤1: 获取高交易量币种
    symbols = get_high_volume_symbols()
    
    if not symbols:
        logger.warning("⚠️ 没有获取到高交易量币种，返回空数据")
        return data_cache

    logger.info(f"📊 开始分析 {len(symbols)} 个币种...")
    
    # 将币种分成10个一批
    symbol_batches = split_into_batches(symbols, 10)
    
    short_term_active = []
    daily_rising = []
    all_cycle_rising = []
    
    # 步骤2: 批量分析短期活跃币种（每批10个）
    logger.info(f"🔄 批量分析短期活跃币种...")
    short_term_start = time.time()
    
    for i, batch in enumerate(symbol_batches):
        logger.info(f"📦 短期活跃批次 {i+1}/{len(symbol_batches)} ({len(batch)} 个币种)")
        batch_results = await analyze_short_term_active_batch(batch)
        short_term_active.extend(batch_results)
        # 批次间短暂延迟，避免API限制
        if i < len(symbol_batches) - 1:
            await asyncio.sleep(0.5)
    
    short_term_time = time.time() - short_term_start
    logger.info(f"✅ 短期活跃分析完成: {len(short_term_active)}个, 耗时: {short_term_time:.2f}秒")
    
    # 步骤3: 批量分析日线上涨币种（每批10个）
    logger.info(f"🔄 批量分析日线上涨币种...")
    daily_start = time.time()
    
    for i, batch in enumerate(symbol_batches):
        logger.info(f"📦 日线上涨批次 {i+1}/{len(symbol_batches)} ({len(batch)} 个币种)")
        batch_results = await analyze_daily_rising_batch(batch)
        daily_rising.extend(batch_results)
        # 批次间短暂延迟
        if i < len(symbol_batches) - 1:
            await asyncio.sleep(0.5)
    
    daily_time = time.time() - daily_start
    logger.info(f"✅ 日线上涨分析完成: {len(daily_rising)}个, 耗时: {daily_time:.2f}秒")
    
    # 步骤4: 对日线上涨币种进行全部周期分析（每批10个）
    if daily_rising:
        logger.info(f"🔄 批量分析全部周期上涨币种...")
        all_cycle_start = time.time()
        
        daily_batches = split_into_batches(daily_rising, 10)
        
        for i, batch in enumerate(daily_batches):
            logger.info(f"📦 全部周期批次 {i+1}/{len(daily_batches)} ({len(batch)} 个币种)")
            batch_results = await analyze_all_cycle_rising_batch(batch)
            all_cycle_rising.extend(batch_results)
            # 批次间短暂延迟
            if i < len(daily_batches) - 1:
                await asyncio.sleep(0.5)
        
        all_cycle_time = time.time() - all_cycle_start
        logger.info(f"✅ 全部周期分析完成: {len(all_cycle_rising)}个, 耗时: {all_cycle_time:.2f}秒")
    
    # 将日线上涨但非全部周期上涨的币种加入daily_rising
    final_daily_rising = []
    all_cycle_symbols = {r['symbol'] for r in all_cycle_rising}
    
    for result in daily_rising:
        if result['symbol'] not in all_cycle_symbols:
            final_daily_rising.append(result)

    # 排序结果
    final_daily_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)
    short_term_active.sort(key=lambda x: x.get('ratio', 0), reverse=True)
    all_cycle_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)

    analysis_time = time.time() - start_time
    logger.info(f"📊 分析完成: 日线上涨 {len(final_daily_rising)}个, 短期活跃 {len(short_term_active)}个, 全部周期上涨 {len(all_cycle_rising)}个")
    logger.info(f"⏱️ 总分析时间: {analysis_time:.2f}秒")

    tz_shanghai = timezone(timedelta(hours=8))
    
    # 发送邮件提醒（如果有全部周期上涨的币种）
    if all_cycle_rising:
        logger.info(f"📧 检测到 {len(all_cycle_rising)} 个全部周期上涨币种，发送邮件提醒...")
        # 在新线程中发送邮件，避免阻塞主分析流程
        email_thread = threading.Thread(
            target=send_email_notification, 
            args=(all_cycle_rising,)
        )
        email_thread.daemon = True
        email_thread.start()
    else:
        logger.info("📧 没有检测到全部周期上涨币种，不发送邮件")

    return {
        'daily_rising': final_daily_rising,
        'short_term_active': short_term_active,
        'all_cycle_rising': all_cycle_rising,
        'analysis_time': analysis_time,
        'last_updated': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
        'next_analysis_time': get_next_analysis_time().strftime("%Y-%m-%d %H:%M:%S")
    }

def analyze_trends():
    """同步包装器，用于调用异步分析函数"""
    try:
        # 创建新的事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(analyze_trends_async())
        loop.run_until_complete(close_aio_session())
        loop.close()
        return result
    except Exception as e:
        logger.error(f"❌ 异步分析失败: {str(e)}")
        # 如果异步分析失败，回退到同步方式
        return analyze_trends_sync_fallback()

def analyze_trends_sync_fallback():
    """同步回退分析函数"""
    start_time = time.time()
    logger.info("🔄 使用同步回退分析...")
    
    symbols = get_high_volume_symbols()
    
    if not symbols:
        return data_cache

    short_term_active = []
    daily_rising = []
    all_cycle_rising = []
    
    # 限制同时分析10个币种
    symbol_batches = split_into_batches(symbols, 10)
    
    for batch in symbol_batches:
        for symbol in batch:
            try:
                # 短期活跃分析
                min5_oi = get_open_interest(symbol, '5m')
                daily_oi = get_open_interest(symbol, '1d')
                
                min5_series = min5_oi.get('series', [])
                daily_series = daily_oi.get('series', [])
                
                if len(min5_series) >= 30 and len(daily_series) >= 30:
                    min5_max = max(min5_series[-30:])
                    daily_avg = sum(daily_series[-30:]) / 30
                    
                    if min5_max > 0 and daily_avg > 0:
                        ratio = min5_max / daily_avg
                        
                        if ratio > 1.5:
                            short_term_active.append({
                                'symbol': symbol,
                                'oi': min5_series[-1],
                                'ratio': round(ratio, 2)
                            })
                
                # 日线上涨分析
                if len(daily_series) >= 30:
                    daily_status = is_latest_highest(daily_series)
                    
                    if daily_status:
                        daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
                        
                        daily_data = {
                            'symbol': symbol,
                            'oi': daily_series[-1],
                            'change': round(daily_change, 2),
                            'period_status': {'1d': True},
                            'period_count': 1
                        }
                        
                        # 全部周期分析
                        period_status = daily_data['period_status'].copy()
                        period_count = daily_data['period_count']
                        all_intervals_up = True
                        
                        for period in VALID_PERIODS:
                            if period == '1d':
                                continue
                                
                            oi_data = get_open_interest(symbol, period)
                            oi_series = oi_data.get('series', [])
                            
                            status = len(oi_series) >= 30 and is_latest_highest(oi_series)
                            period_status[period] = status
                            
                            if status:
                                period_count += 1
                            else:
                                all_intervals_up = False
                        
                        if all_intervals_up:
                            all_cycle_rising.append({
                                'symbol': symbol,
                                'oi': daily_data['oi'],
                                'change': daily_data['change'],
                                'period_count': period_count,
                                'period_status': period_status
                            })
                        else:
                            daily_data['period_status'] = period_status
                            daily_data['period_count'] = period_count
                            daily_rising.append(daily_data)
                            
            except Exception as e:
                logger.error(f"❌ 同步分析{symbol}失败: {str(e)}")
    
    # 排序结果
    daily_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)
    short_term_active.sort(key=lambda x: x.get('ratio', 0), reverse=True)
    all_cycle_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)

    analysis_time = time.time() - start_time
    logger.info(f"📊 同步分析完成: 日线上涨 {len(daily_rising)}个, 短期活跃 {len(short_term_active)}个, 全部周期上涨 {len(all_cycle_rising)}个")
    logger.info(f"⏱️ 总分析时间: {analysis_time:.2f}秒")

    # 发送邮件提醒（如果有全部周期上涨的币种）
    if all_cycle_rising:
        logger.info(f"📧 检测到 {len(all_cycle_rising)} 个全部周期上涨币种，发送邮件提醒...")
        email_thread = threading.Thread(
            target=send_email_notification, 
            args=(all_cycle_rising,)
        )
        email_thread.daemon = True
        email_thread.start()
    else:
        logger.info("📧 没有检测到全部周期上涨币种，不发送邮件")

    tz_shanghai = timezone(timedelta(hours=8))
    return {
        'daily_rising': daily_rising,
        'short_term_active': short_term_active,
        'all_cycle_rising': all_cycle_rising,
        'analysis_time': analysis_time,
        'last_updated': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
        'next_analysis_time': get_next_analysis_time().strftime("%Y-%m-%d %H:%M:%S")
    }

def get_open_interest(symbol, period, use_cache=True):
    """同步获取持仓量数据（用于回退）"""
    try:
        if not re.match(r"^[A-Z0-9]{1,10}USDT$", symbol):
            return {'series': [], 'timestamps': []}

        if period not in VALID_PERIODS:
            return {'series': [], 'timestamps': []}
        
        current_time = datetime.now(timezone.utc)
        cache_key = f"{symbol}_{period}"
        
        if use_cache and cache_key in oi_data_cache:
            cached_data = oi_data_cache[cache_key]
            if 'expiration' in cached_data and cached_data['expiration'] > current_time:
                return cached_data['data']

        logger.debug(f"📡 同步请求持仓量数据: {symbol} {period}")
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}

        headers = {'X-MBX-APIKEY': API_KEY} if API_KEY else {}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code != 200:
            return {'series': [], 'timestamps': []}

        data = response.json()
        
        if not isinstance(data, list) or len(data) == 0:
            return {'series': [], 'timestamps': []}
            
        data.sort(key=lambda x: x['timestamp'])
        oi_series = [float(item['sumOpenInterest']) for item in data]
        timestamps = [item['timestamp'] for item in data]

        if len(oi_series) < 30:
            return {'series': [], 'timestamps': []}
            
        oi_data = {
            'series': oi_series, 
            'timestamps': timestamps
        }
        
        expiration = current_time + timedelta(seconds=OI_CACHE_EXPIRATION)
        oi_data_cache[cache_key] = {
            'data': oi_data,
            'expiration': expiration
        }

        return oi_data
    except Exception as e:
        logger.error(f"❌ 同步获取{symbol}的{period}持仓量失败: {str(e)}")
        return {'series': [], 'timestamps': []}

# 阻力位支撑位分析函数
def calculate_support_resistance_levels(symbol, interval, klines):
    """计算支撑位和阻力位"""
    try:
        if not klines or len(klines) < 20:
            return {'resistance': [], 'support': []}
        
        # 提取收盘价
        closes = [float(k[4]) for k in klines]
        
        # 计算价格水平及其被测试次数
        price_levels = {}
        tolerance = 0.001
        
        for i in range(1, len(closes)-1):
            current_close = closes[i]
            
            # 寻找局部高点和低点
            is_local_high = closes[i] > closes[i-1] and closes[i] > closes[i+1]
            is_local_low = closes[i] < closes[i-1] and closes[i] < closes[i+1]
            
            # 四舍五入到合适的精度
            if current_close > 100:
                rounded_price = round(current_close, 1)
            elif current_close > 10:
                rounded_price = round(current_close, 2)
            elif current_close > 1:
                rounded_price = round(current_close, 3)
            else:
                rounded_price = round(current_close, 4)
            
            # 检查是否接近现有价格水平
            found_existing = False
            for existing_price in price_levels.keys():
                if abs(existing_price - rounded_price) / existing_price <= tolerance:
                    price_levels[existing_price]['count'] += 1
                    if is_local_high:
                        price_levels[existing_price]['resistance_tests'] += 1
                    if is_local_low:
                        price_levels[existing_price]['support_tests'] += 1
                    found_existing = True
                    break
            
            if not found_existing:
                price_levels[rounded_price] = {
                    'count': 1,
                    'resistance_tests': 1 if is_local_high else 0,
                    'support_tests': 1 if is_local_low else 0
                }
        
        # 获取当前价格
        current_price = closes[-1] if closes else 0
        
        # 分离阻力位和支撑位
        resistance_levels = []
        support_levels = []
        
        for price, data in price_levels.items():
            if data['resistance_tests'] > 0 or data['support_tests'] > 0:
                total_tests = data['resistance_tests'] + data['support_tests']
                strength = min(1.0, total_tests / 10.0)
                
                level_data = {
                    'price': price,
                    'strength': round(strength, 2),
                    'test_count': total_tests,
                    'resistance_tests': data['resistance_tests'],
                    'support_tests': data['support_tests'],
                    'distance_percent': round(((price - current_price) / current_price * 100), 2) if current_price > 0 else 0
                }
                
                if price > current_price and data['resistance_tests'] > 0:
                    resistance_levels.append(level_data)
                elif price < current_price and data['support_tests'] > 0:
                    support_levels.append(level_data)
        
        # 按被测试次数排序，只保留前3个
        resistance_levels.sort(key=lambda x: x['test_count'], reverse=True)
        support_levels.sort(key=lambda x: x['test_count'], reverse=True)
        
        return {
            'resistance': resistance_levels[:3],
            'support': support_levels[:3]
        }
        
    except Exception as e:
        logger.error(f"计算支撑阻力位失败 {symbol} {interval}: {str(e)}")
        return {'resistance': [], 'support': []}

def calculate_resistance_levels(symbol):
    """计算阻力位"""
    try:
        logger.info(f"📊 计算阻力位: {symbol}")
        now = time.time()
        
        cache_key = f"{symbol}_resistance"
        if cache_key in resistance_cache:
            cache_data = resistance_cache[cache_key]
            if cache_data['expiration'] > now:
                return cache_data['levels']
        
        if client is None and not init_client():
            return {'levels': {}, 'current_price': 0}
        
        try:
            ticker = client.futures_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
        except Exception:
            current_price = None
        
        interval_levels = {}
        
        for interval in RESISTANCE_INTERVALS:
            try:
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=100)
                
                if not klines or len(klines) < 20:
                    continue

                levels = calculate_support_resistance_levels(symbol, interval, klines)
                interval_levels[interval] = levels
                
            except Exception as e:
                logger.error(f"计算{symbol}在{interval}的阻力位失败: {str(e)}")

        levels = {
            'levels': interval_levels,
            'current_price': current_price or 0
        }
        
        resistance_cache[cache_key] = {
            'levels': levels,
            'expiration': now + RESISTANCE_CACHE_EXPIRATION
        }
        return levels
    except Exception as e:
        logger.error(f"计算{symbol}的阻力位失败: {str(e)}")
        return {'levels': {}, 'current_price': 0}

def analysis_worker():
    """分析工作线程"""
    global data_cache, current_data_cache, analysis_thread_running
    
    logger.info("🔧 数据分析线程启动")
    analysis_thread_running = True
    
    while analysis_thread_running:
        try:
            # 等待到下一个分析时间
            next_analysis = get_next_analysis_time()
            wait_seconds = max(5, (next_analysis - datetime.now(timezone.utc)).total_seconds())
            
            logger.info(f"⏳ 下次分析时间: {next_analysis.strftime('%H:%M:%S')}")
            logger.info(f"⏳ 等待时间: {wait_seconds:.1f} 秒")
            
            # 等待期间检查停止信号
            wait_start = time.time()
            while time.time() - wait_start < wait_seconds and analysis_thread_running:
                time.sleep(1)
            
            if not analysis_thread_running:
                break
                
            # 执行分析
            analysis_start = datetime.now(timezone.utc)
            logger.info(f"⏱️ 开始更新数据...")

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
                
                data_cache = new_data
                current_data_cache = new_data.copy()
                logger.info(f"✅ 数据更新成功")
            except Exception as e:
                logger.error(f"❌ 分析过程中出错: {str(e)}")
                data_cache = backup_cache
                current_data_cache = current_backup

            analysis_duration = (datetime.now(timezone.utc) - analysis_start).total_seconds()
            logger.info(f"⏱️ 分析耗时: {analysis_duration:.2f}秒")
            
        except Exception as e:
            logger.error(f"❌ 分析失败: {str(e)}")
            # 出错后等待一段时间再继续
            time.sleep(60)
    
    logger.info("🛑 分析线程已停止")

def start_background_threads():
    """启动后台线程"""
    global analysis_thread
    
    static_path = app.static_folder
    if not os.path.exists(static_path):
        os.makedirs(static_path)
    
    index_path = os.path.join(static_path, 'index.html')
    if not os.path.exists(index_path):
        with open(index_path, 'w') as f:
            f.write("<html><body><h1>持仓量分析服务正在启动...</h1></body></html>")
    
    global current_data_cache
    tz_shanghai = timezone(timedelta(hours=8))
    current_data_cache = {
        "last_updated": "等待首次分析",
        "daily_rising": [],
        "short_term_active": [],
        "all_cycle_rising": [],
        "analysis_time": 0,
        "next_analysis_time": get_next_analysis_time().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    # 延迟启动分析线程（避免阻塞应用启动）
    def delayed_start():
        time.sleep(10)  # 等待10秒让应用先启动
        
        logger.info("🔄 开始初始化分析组件...")
        
        # 初始化客户端
        if not init_client():
            logger.error("❌ 无法初始化客户端，部分功能可能不可用")
            # 即使客户端初始化失败，也继续启动分析线程，但会返回空数据
        else:
            logger.info("✅ 客户端初始化成功")
        
        # 启动分析线程
        global analysis_thread
        analysis_thread = threading.Thread(target=analysis_worker, name="AnalysisWorker")
        analysis_thread.daemon = True
        analysis_thread.start()
        
        logger.info("🔄 分析线程已启动，将在下一个5分钟周期+45秒后开始分析")
    
    start_thread = threading.Thread(target=delayed_start)
    start_thread.daemon = True
    start_thread.start()
    
    logger.info("✅ 后台线程启动成功")
    return True

# API路由
@app.route('/')
def index():
    try:
        return send_from_directory(app.static_folder, 'index.html')
    except Exception as e:
        logger.error(f"❌ 处理首页请求失败: {str(e)}")
        return "服务正在启动，请稍后刷新...", 200

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/api/data', methods=['GET'])
def get_data():
    global current_data_cache
    try:
        if not current_data_cache or current_data_cache.get("last_updated") == "从未更新":
            return jsonify({
                'last_updated': "数据生成中...",
                'daily_rising': [],
                'short_term_active': [],
                'all_cycle_rising': [],
                'analysis_time': 0,
                'next_analysis_time': "计算中..."
            })
        
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
            'next_analysis_time': get_next_analysis_time().strftime("%Y-%m-%d %H:%M:%S")
        })

@app.route('/api/resistance_levels/<symbol>', methods=['GET'])
def get_resistance_levels(symbol):
    try:
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            return jsonify({'error': 'Invalid symbol format'}), 400

        if not BINANCE_AVAILABLE:
            return jsonify({'error': 'Binance client not available'}), 503

        levels = calculate_resistance_levels(symbol)
        
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
            return jsonify({'error': 'Invalid symbol format'}), 400

        if period not in VALID_PERIODS:
            return jsonify({'error': 'Unsupported period'}), 400

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
        binance_status = 'not initialized'
        if client:
            try:
                client.get_server_time()
                binance_status = 'ok'
            except:
                binance_status = 'error'
        
        tz_shanghai = timezone(timedelta(hours=8))
        return jsonify({
            'status': 'healthy',
            'binance': binance_status,
            'analysis_thread_running': analysis_thread_running,
            'last_updated': current_data_cache.get('last_updated', 'N/A'),
            'next_analysis_time': current_data_cache.get('next_analysis_time', 'N/A'),
            'server_time': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
            'environment': 'render' if IS_RENDER else 'local'
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

@app.route('/api/status', methods=['GET'])
def status():
    """简化状态检查"""
    return jsonify({
        'status': 'running',
        'analysis_thread_running': analysis_thread_running,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/trigger-analysis', methods=['POST'])
def trigger_analysis():
    """手动触发分析（用于调试）"""
    try:
        logger.info("🔄 手动触发分析...")
        result = analyze_trends()
        
        global current_data_cache
        current_data_cache = {
            "last_updated": result['last_updated'],
            "daily_rising": result['daily_rising'],
            "short_term_active": result['short_term_active'],
            "all_cycle_rising": result['all_cycle_rising'],
            "analysis_time": result['analysis_time'],
            "next_analysis_time": result['next_analysis_time']
        }
        
        return jsonify({
            'status': 'success',
            'message': '分析完成',
            'last_updated': result['last_updated']
        })
    except Exception as e:
        logger.error(f"❌ 手动分析失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 10000))
    
    logger.info("=" * 50)
    logger.info(f"🚀 启动加密货币持仓量分析服务")
    logger.info(f"🌐 服务端口: {PORT}")
    logger.info(f"🏷️ 环境: {'Render' if IS_RENDER else 'Local'}")
    logger.info("=" * 50)
    
    if start_background_threads():
        logger.info("🚀 启动服务器...")
        
        # 如果在 Render 环境，使用更简单的启动方式
        if IS_RENDER:
            # 使用 Gunicorn 兼容的方式
            app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
        else:
            app.run(host='0.0.0.0', port=PORT, debug=False)
    else:
        logger.critical("🔥 无法启动服务")
