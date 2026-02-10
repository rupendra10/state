import numpy as np
from scipy.stats import norm
from datetime import datetime, timedelta

def get_ist_now():
    """Returns current IST datetime (UTC+5:30)"""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def black_scholes_price(flag, S, K, t, r, sigma):
    # Clamp sigma to prevent overflow (max ~10 = 1000% IV)
    sigma = np.clip(sigma, 0.001, 10.0)
    if t <= 0:
        t = 0.0001
    
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)
    
    if flag == 'c':
        price = S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2)
    else:
        price = K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)
    
    return price

def _vega(S, K, t, r, sigma):
    # Clamp sigma to prevent overflow
    sigma = np.clip(sigma, 0.001, 10.0)
    if t <= 0:
        t = 0.0001
    
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    return S * norm.pdf(d1) * np.sqrt(t)

def calculate_implied_volatility(price, S, K, t, r, flag='p'):
    """
    Calculate Implied Volatility (IV) using Newton-Raphson method.
    """
    if t <= 0:
        return 0.001
    
    # Intrinsic check
    intrinsic = 0
    if flag == 'p':
        intrinsic = max(K - S, 0)
    else:
        intrinsic = max(S - K, 0)
        
    if price < intrinsic:
        return 0.001
        
    # Initial Guess
    sigma = 0.5
    
    # Newton-Raphson
    for i in range(100):
        bs_price = black_scholes_price(flag, S, K, t, r, sigma)
        diff = price - bs_price
        
        if abs(diff) < 1e-5:
            return sigma
            
        v = _vega(S, K, t, r, sigma)
        
        if v == 0:
            break
            
        sigma = sigma + diff / v
        
        # Clamp sigma during iterations to prevent overflow
        sigma = np.clip(sigma, 0.001, 10.0)
        
    return sigma

import config

def get_next_trading_day(start_date=None):
    """
    Returns the next valid trading date (skips Weekends and NSE_HOLIDAYS).
    """
    if start_date is None:
        start_date = datetime.now().date()
    
    next_day = start_date + timedelta(days=1)
    
    # Loop until we find a valid day
    while True:
        # Check Weekend (5=Sat, 6=Sun)
        if next_day.weekday() >= 5:
            next_day += timedelta(days=1)
            continue
            
        # Check Holiday
        if str(next_day) in config.NSE_HOLIDAYS:
            next_day += timedelta(days=1)
            continue
            
        return next_day
