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

# ... [前面的代码保持不变，只修改阻力位计算函数和API响应] ...

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
        
        # 获取当前价格
        try:
            ticker = client.futures_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            logger.info(f"📊 {symbol}当前价格: {current_price}")
        except Exception as e:
            logger.error(f"❌ 获取{symbol}当前价格失败: {str(e)}")
            current_price = None
        
        global_resistances = []
        global_supports = []
        
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
                
                # 计算近期高点和低点
                lookback = min(30, len(high_prices))
                recent_high = max(high_prices[-lookback:])
                recent_low = min(low_prices[-lookback:])
                
                if recent_high <= recent_low:
                    logger.warning(f"⚠️ {symbol}在{interval}的最近高点和低点无效")
                    continue
                
                # 计算斐波那契回撤位
                fib_levels = {
                    '0.236': recent_high - (recent_high - recent_low) * 0.236,
                    '0.382': recent_high - (recent_high - recent_low) * 0.382,
                    '0.5': (recent_high + recent_low) / 2,
                    '0.618': recent_high - (recent_high - recent_low) * 0.618,
                    '0.786': recent_high - (recent_high - recent_low) * 0.786,
                    '1.0': recent_high,
                    '1.272': recent_high + (recent_high - recent_low) * 0.272,
                    '1.618': recent_high + (recent_high - recent_low) * 0.618
                }
                
                # 只保留当前价格附近的水平
                if current_price:
                    # 阻力位：高于当前价格，取最接近的3个
                    resistances = [p for p in fib_levels.values() if p > current_price]
                    resistances.sort(key=lambda p: abs(p - current_price))
                    
                    # 支撑位：低于当前价格，取最接近的3个
                    supports = [p for p in fib_levels.values() if p < current_price]
                    supports.sort(key=lambda p: abs(p - current_price), reverse=True)
                    
                    # 添加整数位
                    base = 10 ** (math.floor(math.log10(current_price)) - 1)
                    integer_level = round(current_price / base) * base
                    
                    # 添加整数位阻力/支撑
                    if integer_level > current_price:
                        resistances.append(integer_level)
                    else:
                        supports.append(integer_level)
                    
                    # 添加近期高点和低点
                    resistances.append(recent_high)
                    supports.append(recent_low)
                    
                    # 去重并排序
                    resistances = sorted(set(resistances))
                    supports = sorted(set(supports), reverse=True)
                    
                    # 取每个周期最有效的3个阻力和支撑
                    best_resistances = resistances[:3] if resistances else []
                    best_supports = supports[:3] if supports else []
                    
                    # 添加到全局列表
                    global_resistances.extend(best_resistances)
                    global_supports.extend(best_supports)
                    
                    logger.info(f"📊 {symbol}在{interval}的有效阻力位: {best_resistances}, 支撑位: {best_supports}")
            except Exception as e:
                logger.error(f"计算{symbol}在{interval}的阻力位失败: {str(e)}")
                logger.error(traceback.format_exc())

        # 全局排序：按有效性（距离当前价格）排序
        if current_price:
            global_resistances = sorted(set(global_resistances), key=lambda p: abs(p - current_price))
            global_supports = sorted(set(global_supports), key=lambda p: abs(p - current_price))
        
        # 取全局最优的3个阻力和支撑
        top_resistances = global_resistances[:3] if global_resistances else []
        top_supports = global_supports[:3] if global_supports else []
        
        logger.info(f"📊 {symbol}全局最优阻力位: {top_resistances}, 支撑位: {top_supports}")
        
        levels = {
            'resistance': top_resistances,
            'support': top_supports
        }
        
        resistance_cache[symbol] = {
            'levels': levels,
            'expiration': now + RESISTANCE_CACHE_EXPIRATION
        }
        return levels
    except Exception as e:
        logger.error(f"计算{symbol}的阻力位失败: {str(e)}")
        return {'resistance': [], 'support': []}
