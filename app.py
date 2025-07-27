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
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_from_directory, request
from binance.client import Client

# ���ø���ϸ����־����
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('app.log')
    ]
)
logger = logging.getLogger(__name__)
logger.info(f"�7�3 ��־��������Ϊ: {LOG_LEVEL}")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='/static')

# Binance API ����
API_KEY = os.environ.get('BINANCE_API_KEY', 'your_api_key_here')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'your_api_secret_here')
client = None

# ==================== ���ݻ��� ====================
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

# ʹ�ö��н����̼߳�ͨ��
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
ALL_PERIODS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']

def init_db():
    try:
        logger.debug("�0�0�1�5 ��ʼ��ʼ�����ݿ�...")
        conn = sqlite3.connect('data.db')
        c = conn.cursor()

        # �����Ƿ����
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_data'")
        table_exists = c.fetchone()

        if not table_exists:
            logger.info("�0�0�1�5 �������ݿ��...")
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
            logger.info("�7�3 ���ݿ�����ɹ�")
        else:
            # ����ṹ�Ƿ�����
            c.execute("PRAGMA table_info(crypto_data)")
            columns = [col[1] for col in c.fetchall()]
            if 'next_analysis_time' not in columns:
                logger.info("�0�0�1�5 �������ݿ��ṹ...")
                c.execute("ALTER TABLE crypto_data ADD COLUMN next_analysis_time TEXT")
                conn.commit()
                logger.info("�7�3 ���ݿ��ṹ���³ɹ�")
            else:
                logger.info("�7�3 ���ݿ���Ѵ����ҽṹ����")

        conn.close()
        logger.debug("�0�0�1�5 ���ݿ��ʼ�����")
    except Exception as e:
        logger.error(f"�7�4 ���ݿ��ʼ��ʧ��: {str(e)}")
        logger.error(traceback.format_exc())
        try:
            logger.warning("�9�9�1�5 ����ɾ���𻵵����ݿ��ļ�...")
            os.remove('data.db')
            logger.warning("�9�9�1�5 ɾ���𻵵����ݿ��ļ��������������ݿ�")
            init_db()
        except Exception as e2:
            logger.critical(f"�9�7 �޷��޸����ݿ�: {str(e2)}")
            logger.critical(traceback.format_exc())

def save_to_db(data):
    try:
        logger.debug("�9�4 ��ʼ�������ݵ����ݿ�...")
        conn = sqlite3.connect('data.db')
        c = conn.cursor()

        # ȷ�������
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_data'")
        if not c.fetchone():
            logger.warning("�7�2�1�5 ���ݿ�����ڣ����ڴ���...")
            init_db()

        resistance_json = json.dumps(resistance_cache)

        # ������������
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
        logger.info("�9�4 ���ݱ��浽���ݿ�ɹ�")
    except Exception as e:
        logger.error(f"�7�4 �������ݵ����ݿ�ʧ��: {str(e)}")
        logger.error(traceback.format_exc())

def get_last_valid_data():
    try:
        logger.debug("�9�3 ���Ի�ȡ�����Ч����...")
        conn = sqlite3.connect('data.db')
        c = conn.cursor()

        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_data'")
        if not c.fetchone():
            logger.warning("�7�2�1�5 ���ݿ������")
            return None

        c.execute("SELECT * FROM crypto_data ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        conn.close()

        if row:
            logger.debug(f"�9�3 �ҵ������Ч����: ID={row[0]}, ʱ��={row[1]}")
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
        logger.debug("�9�3 ���ݿ���û����Ч����")
        return None
    except Exception as e:
        logger.error(f"�7�4 ��ȡ�����Ч����ʧ��: {str(e)}")
        logger.error(traceback.format_exc())
        return None

def init_client():
    global client
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            logger.debug(f"�9�9 ���Գ�ʼ��Binance�ͻ��� (��{attempt+1}��)...")
            client_params = {
                'api_key': API_KEY, 
                'api_secret': API_SECRET,
                'requests_params': {'timeout': 30}
            }

            client = Client(**client_params)
            
            # ��������
            server_time = client.get_server_time()
            logger.info(f"�7�3 Binance�ͻ��˳�ʼ���ɹ���������ʱ��: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
            return True
        except Exception as e:
            logger.error(f"�7�4 ��ʼ��Binance�ͻ���ʧ��: {str(e)}")
            logger.error(traceback.format_exc())
            if attempt < max_retries - 1:
                logger.info(f"�9�4 {retry_delay}������Գ�ʼ���ͻ���...")
                time.sleep(retry_delay)
            else:
                logger.critical("�9�7 �޷���ʼ��Binance�ͻ��ˣ��Ѵﵽ������Դ���")
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
        # ��֤���ָ�ʽ
        if not re.match(r"^[A-Z]{3,15}USDT$", symbol):
            logger.warning(f"�7�2�1�5 ��Ч�ı�������: {symbol}")
            return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

        if period not in PERIOD_MINUTES:
            logger.warning(f"�7�2�1�5 ��֧�ֵ�����: {period}, ʹ��Ĭ��5m")
            period = '5m'

        current_time = datetime.now(timezone.utc)

        cache_key = f"{symbol}_{period}"
        if use_cache and oi_data_cache.get(cache_key):
            cached_data = oi_data_cache[cache_key]
            if 'next_update' in cached_data and cached_data['next_update'] > current_time:
                logger.debug(f"�9�4 ʹ�û�������: {symbol} {period}")
                return {
                    'series': cached_data['data']['series'].copy(),
                    'timestamps': cached_data['data']['timestamps'].copy(),
                    'cache_time': cached_data['data']['cache_time']
                }

        logger.info(f"�9�9 ����ֲ�������: symbol={symbol}, period={period}")
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}

        response = requests.get(url, params=params, timeout=15)
        logger.debug(f"�9�9 ��Ӧ״̬: {response.status_code}")

        if response.status_code != 200:
            logger.error(f"�7�4 ��ȡ{symbol}��{period}�ֲ���ʧ��: HTTP {response.status_code} - {response.text}")
            return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

        data = response.json()
        logger.debug(f"�9�9 ��Ӧ����: {data[:1]}...")

        if not isinstance(data, list):
            logger.error(f"�7�4 ��Ч�ĳֲ������ݸ�ʽ: {symbol} {period} - ��Ӧ: {data}")
            return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

        if len(data) == 0:
            logger.warning(f"�7�2�1�5 {symbol}��{period}�ֲ�������Ϊ��")
            return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

        data.sort(key=lambda x: x['timestamp'])
        oi_series = [float(item['sumOpenInterest']) for item in data]
        timestamps = [item['timestamp'] for item in data]
        cache_time = datetime.now(timezone.utc).isoformat()

        if len(oi_series) < 5:
            logger.warning(f"�7�2�1�5 {symbol}��{period}�ֲ������ݲ���: ֻ��{len(oi_series)}����")
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

        logger.info(f"�9�4 ��ȡ������: {symbol} {period} ({len(oi_series)}��)")
        return oi_data
    except Exception as e:
        logger.error(f"�7�4 ��ȡ{symbol}��{period}�ֲ���ʧ��: {str(e)}")
        logger.error(traceback.format_exc())
        return {'series': [], 'timestamps': [], 'cache_time': datetime.now(timezone.utc).isoformat()}

def is_latest_highest(oi_data):
    if len(oi_data) < 30:
        logger.debug("�ֲ������ݲ���30����")
        return False

    latest_value = oi_data[-1]
    prev_data = oi_series[-30:-1]

    if not prev_data:
        logger.debug("����ʷ�������ڱȽ�")
        return False

    result = latest_value > max(prev_data)
    logger.debug(f"�ֲ������¸߼��: ����ֵ={latest_value}, ��ʷ���ֵ={max(prev_data)}, ���={result}")
    return result

def calculate_resistance_levels(symbol):
    try:
        logger.debug(f"�9�6 ��������λ: {symbol}")
        
        # ȷ���ͻ����ѳ�ʼ��
        if client is None:
            logger.warning("�7�2�1�5 Binance�ͻ���δ��ʼ�����������³�ʼ��")
            if not init_client():
                logger.error("�7�4 �޷���ʼ��Binance�ͻ��ˣ��޷���������λ")
                return {}
        
        now = time.time()
        if symbol in resistance_cache:
            cache_data = resistance_cache[symbol]
            if cache_data['expiration'] > now:
                logger.debug(f"�9�6 ʹ�û��������λ����: {symbol}")
                return cache_data['levels']

        levels = {}
        logger.debug(f"�9�6 ��ʼ����{symbol}������λ")

        for interval in RESISTANCE_INTERVALS:
            try:
                logger.debug(f"�9�6 ��ȡK������: {symbol} {interval}")
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=100)
                
                if not klines or len(klines) < 10:
                    logger.warning(f"�7�2�1�5 {symbol}��{interval}��K�����ݲ���")
                    continue

                high_prices = [float(k[2]) for k in klines]
                low_prices = [float(k[3]) for k in klines]
                recent_high = max(high_prices[-10:])
                recent_low = min(low_prices[-10:])

                lookback = min(30, len(high_prices))
                recent_max = max(high_prices[-lookback:])
                recent_min = min(low_prices[-lookback:])

                if recent_max <= recent_min:
                    logger.warning(f"�7�2�1�5 {symbol}��{interval}������ߵ�͵͵���Ч")
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
                
                logger.debug(f"�9�6 {symbol}��{interval}������λ�������")
            except Exception as e:
                logger.error(f"����{symbol}��{interval}������λʧ��: {str(e)}")
                logger.error(traceback.format_exc())
                levels[interval] = {
                    'resistance': [],
                    'support': []
                }

        resistance_cache[symbol] = {
            'levels': levels,
            'expiration': now + RESISTANCE_CACHE_EXPIRATION
        }
        logger.info(f"�9�6 {symbol}������λ�������")
        return levels
    except Exception as e:
        logger.error(f"����{symbol}������λʧ��: {str(e)}")
        logger.error(traceback.format_exc())
        return {}

def analyze_symbol(symbol):
    try:
        logger.info(f"�9�3 ��ʼ��������: {symbol}")
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

        # 1. ��ȡ���ֲ߳�������
        logger.debug(f"�9�6 ��ȡ���ֲ߳���: {symbol}")
        daily_oi = get_open_interest(symbol, '1d', use_cache=True)
        symbol_result['oi_data']['1d'] = daily_oi
        daily_series = daily_oi['series']
        
        logger.debug(f"�9�6 ���ֲ߳������ݳ���: {len(daily_series)}")

        # 2. ���������������
        if len(daily_series) >= 30:
            daily_up = is_latest_highest(daily_series)
            logger.debug(f"�9�6 �������Ǽ��: {daily_up}")

            if daily_up:
                daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
                logger.debug(f"�9�6 ���߱仯: {daily_change:.2f}%")
                
                symbol_result['daily_rising'] = {
                    'symbol': symbol,
                    'oi': daily_series[-1],
                    'change': round(daily_change, 2),
                    'period_count': 1
                }
                symbol_result['rising_periods'].append('1d')
                symbol_result['period_status']['1d'] = True
                symbol_result['period_count'] = 1

                # 3. ȫ���ڷ���
                logger.debug(f"�9�6 ��ʼȫ���ڷ���: {symbol}")
                all_intervals_up = True
                for period in ALL_PERIODS:
                    if period == '1d':
                        continue

                    logger.debug(f"�9�6 ��������: {period}")
                    oi_data = get_open_interest(symbol, period, use_cache=True)
                    symbol_result['oi_data'][period] = oi_data
                    oi_series = oi_data['series']
                    
                    if len(oi_series) < 30:
                        logger.debug(f"�9�6 ���ݲ���: {symbol} {period}ֻ��{len(oi_series)}����")
                        status = False
                    else:
                        status = is_latest_highest(oi_series)
                        
                    symbol_result['period_status'][period] = status
                    logger.debug(f"�9�6 ����״̬: {period} = {status}")

                    if status:
                        symbol_result['rising_periods'].append(period)
                        symbol_result['period_count'] += 1
                    else:
                        all_intervals_up = False

                if all_intervals_up:
                    logger.debug(f"�9�6 ȫ��������: {symbol}")
                    symbol_result['all_cycle_rising'] = {
                        'symbol': symbol,
                        'oi': daily_series[-1],
                        'change': round(daily_change, 2),
                        'periods': symbol_result['rising_periods'],
                        'period_status': symbol_result['period_status'],
                        'period_count': symbol_result['period_count']
                    }

        # 4. ���ڻ�Ծ�ȷ���
        logger.debug(f"�9�6 �������ڻ�Ծ��: {symbol}")
        min5_oi = get_open_interest(symbol, '5m', use_cache=True)
        symbol_result['oi_data']['5m'] = min5_oi
        min5_series = min5_oi['series']
        
        if len(min5_series) >= 30 and len(daily_series) >= 30:
            min5_max = max(min5_series[-30:])
            daily_avg = sum(daily_series[-30:]) / 30
            logger.debug(f"�9�6 �������ֵ: {min5_max}, �վ�ֵ: {daily_avg}")

            if min5_max > 0 and daily_avg > 0:
                ratio = min5_max / daily_avg
                logger.debug(f"�9�6 ���ڻ�Ծ����: {ratio:.2f}")
                
                if ratio > 1.5:
                    logger.debug(f"�9�6 ���ڻ�Ծ: {symbol} ����={ratio:.2f}")
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

        logger.info(f"�7�3 ��ɷ�������: {symbol}")
        return symbol_result
    except Exception as e:
        logger.error(f"�7�4 ����{symbol}ʱ����: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            'symbol': symbol,
            'period_status': {p: False for p in ALL_PERIODS},
            'period_count': 0,
            'oi_data': {}
        }

def analyze_trends():
    start_time = time.time()
    logger.info("�9�3 ��ʼ������������...")
    symbols = get_high_volume_symbols()
    
    if not symbols:
        logger.warning("�7�2�1�5 û���ҵ��߽���������")
        return data_cache

    logger.info(f"�9�3 ��ʼ���� {len(symbols)} ������")

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
            logger.error(f"�7�4 ����{symbol}ʱ����: {str(e)}")

        if processed % max(1, total_symbols // 10) == 0 or processed == total_symbols:
            logger.info(f"�7�7 ��������: {processed}/{total_symbols} ({int(processed/total_symbols*100)}%)")

    daily_rising.sort(key=lambda x: (x.get('period_count', 0), x.get('change', 0)), reverse=True)
    short_term_active.sort(key=lambda x: (x.get('period_count', 0), x.get('ratio', 0)), reverse=True)
    all_cycle_rising.sort(key=lambda x: (x.get('period_count', 0), x.get('change', 0)), reverse=True)

    logger.info(f"�9�6 �������: �������� {len(daily_rising)}��, ���ڻ�Ծ {len(short_term_active)}��, ȫ���������� {len(all_cycle_rising)}��")
    analysis_time = time.time() - start_time
    logger.info(f"�7�3 �������: ��ʱ{analysis_time:.2f}��")

    return {
        'daily_rising': daily_rising,
        'short_term_active': short_term_active,
        'all_cycle_rising': all_cycle_rising,
        'analysis_time': analysis_time
    }

def get_high_volume_symbols():
    # ȷ���ͻ����ѳ�ʼ��
    if client is None:
        logger.warning("�7�2�1�5 Binance�ͻ���δ��ʼ�����������³�ʼ��")
        if not init_client():
            logger.error("�7�4 �޷�����API")
            return []

    try:
        logger.info("�9�6 ��ȡ�߽���������...")
        tickers = client.futures_ticker()
        filtered = [
            t for t in tickers if float(t.get('quoteVolume', 0)) > 10000000
            and t.get('symbol', '').endswith('USDT')
        ]
        logger.info(f"�9�6 �ҵ� {len(filtered)} ���߽���������")
        logger.debug(f"�9�6 ǰ5������: {[t['symbol'] for t in filtered[:5]]}")
        return [t['symbol'] for t in filtered]
    except Exception as e:
        logger.error(f"�7�4 ��ȡ�߽���������ʧ��: {str(e)}")
        logger.error(traceback.format_exc())
        return []

def analysis_worker():
    global data_cache, current_data_cache
    logger.info("�9�9 ���ݷ����߳�����")
    init_db()

    initial_data = get_last_valid_data()
    if initial_data:
        logger.info("�9�1 ������ʷ����")
        data_cache = initial_data
        current_data_cache = data_cache.copy()
        logger.debug(f"�9�1 ���ص�����: {json.dumps(initial_data, indent=2)}")
    else:
        logger.info("�9�5 û����ʷ���ݣ��������״η���")

    while True:
        try:
            task = analysis_queue.get()
            if task == "STOP":
                logger.info("�0�5 �յ�ֹͣ�źţ����������߳�")
                break

            analysis_start = datetime.now(timezone.utc)
            logger.info(f"�7�5�1�5 ��ʼ�������� ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')})...")

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
                
                logger.debug(f"�9�6 �������: {json.dumps(new_data, indent=2)}")

                data_cache = new_data
                save_to_db(new_data)
                current_data_cache = new_data.copy()
                logger.info(f"�7�3 ���ݸ��³ɹ�")
            except Exception as e:
                logger.error(f"�7�4 ���������г���: {str(e)}")
                logger.error(traceback.format_exc())
                data_cache = backup_cache
                current_data_cache = current_backup
                logger.info("�9�4 �ָ���ʷ����")

            analysis_end = datetime.now(timezone.utc)
            analysis_duration = (analysis_end - analysis_start).total_seconds()
            logger.info(f"�7�5�1�5 ������ʱ: {analysis_duration:.2f}��")
            
            # ��¼��һ�η���ʱ��
            next_time = get_next_update_time('5m')
            wait_seconds = (next_time - analysis_end).total_seconds()
            logger.info(f"�7�7 �´η������� {wait_seconds:.1f} ��� ({next_time.strftime('%Y-%m-%d %H:%M:%S')})")
            
            logger.info("=" * 50)

        except Exception as e:
            logger.error(f"�7�4 ����ʧ��: {str(e)}")
            logger.error(traceback.format_exc())
        analysis_queue.task_done()

def schedule_analysis():
    logger.info("�7�4 ��ʱ��������������")
    now = datetime.now(timezone.utc)
    current_minute = now.minute
    next_minute = ((current_minute // 5) + 1) * 5
    if next_minute >= 60:
        next_minute = 0
        next_time = now.replace(hour=now.hour + 1, minute=next_minute, second=0, microsecond=0)
    else:
        next_time = now.replace(minute=next_minute, second=0, microsecond=0)

    initial_wait = (next_time - now).total_seconds()
    logger.info(f"�7�7 �״η������� {initial_wait:.1f} ���ʼ ({next_time.strftime('%Y-%m-%d %H:%M:%S')})...")
    time.sleep(initial_wait)

    while True:
        analysis_start = datetime.now(timezone.utc)
        logger.info(f"�9�0 ������ʱ�������� ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')})")
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
            logger.warning("�7�2�1�5 ������ʱ������������ʼ��һ�η���")
        elif wait_time > 300:
            adjusted_wait = max(60, wait_time - 120)
            logger.info(f"�7�7 �����ȴ�ʱ��: {wait_time:.1f}�� -> {adjusted_wait:.1f}��")
            wait_time = adjusted_wait

        logger.info(f"�7�7 �´η������� {wait_time:.1f} ��� ({next_time.strftime('%Y-%m-%d %H:%M:%S')})")
        time.sleep(wait_time)

# API·��
@app.route('/')
def index():
    try:
        logger.debug("�9�4 ������ҳ����")
        static_path = app.static_folder
        if static_path is None:
            return "��̬�ļ�·��δ����", 500

        index_path = os.path.join(static_path, 'index.html')
        if not os.path.exists(index_path):
            return "index.html not found", 404
        return send_from_directory(static_path, 'index.html', mimetype='text/html')
    except Exception as e:
        logger.error(f"�7�4 ������ҳ����ʧ��: {str(e)}")
        return "Internal Server Error", 500

@app.route('/static/<path:filename>')
def static_files(filename):
    try:
        logger.debug(f"�9�7 ����̬�ļ�: {filename}")
        static_path = app.static_folder
        if static_path is None:
            return "��̬�ļ�·��δ����", 500

        return send_from_directory(static_path, filename)
    except Exception as e:
        logger.error(f"�7�4 ����̬�ļ�����ʧ��: {str(e)}")
        return "File not found", 404

@app.route('/api/data', methods=['GET'])
def get_data():
    global current_data_cache
    try:
        logger.debug("�9�9 ����/api/data")
        data = {
            'last_updated': current_data_cache.get('last_updated', "") or "",
            'daily_rising': current_data_cache.get('daily_rising', []) or [],
            'short_term_active': current_data_cache.get('short_term_active', []) or [],
            'all_cycle_rising': current_data_cache.get('all_cycle_rising', []) or [],
            'analysis_time': current_data_cache.get('analysis_time', 0),
            'next_analysis_time': current_data_cache.get('next_analysis_time', "") or ""
        }
        logger.debug(f"�9�9 ��������: {json.dumps(data, indent=2)}")
        return jsonify(data)
    except Exception as e:
        logger.error(f"�7�4 ��ȡ����ʧ��: {str(e)}")
        last_data = get_last_valid_data()
        if last_data:
            return jsonify(last_data)
        return jsonify({'error': str(e)}), 500

# ����λAPI�˵�
@app.route('/api/resistance_levels/<symbol>', methods=['GET'])
def get_resistance_levels(symbol):
    try:
        # ��֤���ָ�ʽ
        if not re.match(r"^[A-Z]{3,15}USDT$", symbol):
            logger.warning(f"�7�2�1�5 ��Ч�ı�������: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        logger.info(f"�9�6 ��ȡ����λ����: {symbol}")
        levels = calculate_resistance_levels(symbol)
        
        if not levels:
            logger.warning(f"�7�2�1�5 δ�ҵ�����λ����: {symbol}")
            return jsonify({'error': 'Resistance levels not found'}), 404
            
        return jsonify(levels)
    except Exception as e:
        logger.error(f"�7�4 ��ȡ����λ����ʧ��: {symbol}, {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# �������˵�
@app.route('/health', methods=['GET'])
def health_check():
    try:
        # ������ݿ�����
        conn = sqlite3.connect('data.db')
        c = conn.cursor()
        c.execute("SELECT 1")
        conn.close()
        
        # ���Binance����
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
    # ȷ����̬�ļ��д���
    static_path = app.static_folder
    if not os.path.exists(static_path):
        os.makedirs(static_path)
    
    # ȷ�� index.html ����
    index_path = os.path.join(static_path, 'index.html')
    if not os.path.exists(index_path):
        with open(index_path, 'w') as f:
            f.write("<html><body><h1>�뽫ǰ���ļ�����staticĿ¼</h1></body></html>")
    
    # ��ʼ�����ݿ�
    init_db()
    
    # ��ʼ���ͻ���
    if not init_client():
        logger.critical("�7�4 �޷���ʼ���ͻ���")
        return False
    
    # ������̨�߳�
    worker_thread = threading.Thread(target=analysis_worker, name="AnalysisWorker")
    worker_thread.daemon = True
    worker_thread.start()
    
    scheduler_thread = threading.Thread(target=schedule_analysis, name="AnalysisScheduler")
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    logger.info("�7�3 ��̨�߳������ɹ�")
    return True

if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 9600))
    
    logger.info("=" * 50)
    logger.info(f"�0�4 �������ܻ��ҳֲ�����������")
    logger.info(f"�9�7 API��Կ: {API_KEY[:5]}...{API_KEY[-3:]}")
    logger.info(f"�9�4 ����˿�: {PORT}")
    logger.info("=" * 50)
    
    if start_background_threads():
        logger.info("�0�4 ����������...")
        app.run(host='0.0.0.0', port=PORT, debug=False)
    else:
        logger.error("�7�4 ��������ʧ��")
