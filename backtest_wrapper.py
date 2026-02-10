import pandas as pd
import numpy as np
import config
from datetime import datetime, timedelta

class BacktestWrapper:
    def __init__(self, start_date=None, end_date=None):
        """
        Mock API Wrapper for Backtesting.
        Generates or loads historical data and serves it field-by-field.
        """
        self.current_time = None
        self.spot_data = pd.DataFrame() 
        self.options_data = pd.DataFrame()
        
        self.start_date = start_date if start_date else datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)
        self.end_date = end_date if end_date else self.start_date + timedelta(hours=6)

        print(f"Initializing Backtester...")
        try:
            # Try loading from CSV if exists
            # self.spot_data = pd.read_csv(f"{config.HISTORICAL_DATA_DIR}/spot.csv")
            # self.spot_data['timestamp'] = pd.to_datetime(self.spot_data['timestamp'])
             raise FileNotFoundError # Force dummy generation for now
        except Exception:
            print("No historical data found. Generating DUMMY data for simulation...")
            self.generate_dummy_data()

    def generate_dummy_data(self):
        """Generates sine wave spot data and corresponding option chains."""
        # 1. Generate Time Index (1 minute intervals)
        timestamps = pd.date_range(start=self.start_date, end=self.end_date, freq='1min')
        
        # 2. Generate Spot Price (Sine wave around 24000)
        t = np.linspace(0, 4*np.pi, len(timestamps))
        prices = 24000 + 100 * np.sin(t) + np.random.normal(0, 5, len(timestamps))
        
        self.spot_data = pd.DataFrame({
            'timestamp': timestamps,
            'close': prices
        }).set_index('timestamp')
        
        print(f"Generated {len(self.spot_data)} spot records.")

    def set_time(self, timestamp):
        self.current_time = timestamp
        
    def get_spot_price(self, instrument_key):
        """Returns the Spot Close price at the current_time."""
        if self.current_time in self.spot_data.index:
            return self.spot_data.loc[self.current_time]['close']
        
        # Fallback: get closest previous match
        try:
            # asof requires sorted index
            return self.spot_data.asof(self.current_time)['close']
        except:
            return 24000.0
        
    def get_option_chain_data(self, spot_price):
        """
        Generates a mock option chain around current spot price.
        Returns: cw_chain, nw_chain, m_chain
        """
        # Determine current/next expiries based on self.current_time
        # Simple Logic: Current week is this Thursday.
        
        today = self.current_time.date()
        days_ahead = 3 - today.weekday() # 3 = Thursday
        if days_ahead < 0: days_ahead += 7
        curr_expiry = today + timedelta(days=days_ahead)
        next_expiry = curr_expiry + timedelta(days=7)
        month_expiry = curr_expiry + timedelta(days=28) # Approximate

        def make_chain(expiry_date):
            chain = []
            atm = round(spot_price / 50) * 50
            strikes = range(atm - 500, atm + 550, 50)
            
            t_year = (expiry_date - today).days / 365.0
            if t_year <= 0: t_year = 0.001

            for k in strikes:
                # Mock Pricing (Black Scholes-ish or simple intrinsic + time)
                # Put Price
                intrinsic_p = max(0, k - spot_price)
                time_val = 100 * t_year # simplified
                ltp_p = intrinsic_p + time_val
                
                # Delta (simplified)
                # OTM Put (K < Spot): Delta -> 0
                # ITM Put (K > Spot): Delta -> -1
                # ATM Put: -0.5
                moneyness = (k - spot_price) / spot_price
                delta_p = -0.5 + (moneyness * 5) # Rough linear approx
                delta_p = max(-1.0, min(0.0, delta_p))

                chain.append({
                    'instrument_key': f"NIFTY|{expiry_date}|{k}|PE",
                    'strike': k,
                    'last_price': ltp_p, # This is 'ltp' usually
                    'ltp': ltp_p,
                    'expiry_dt': expiry_date.strftime("%Y-%m-%d"),
                    'time_to_expiry': t_year,
                    'iv': 0.15,
                    'delta': abs(delta_p), # Strategy expects abs delta for selection? No, usually raw, but strategy.py does abs(). 
                    # Wrapper should probably provide raw. strategies/calendar_pe_weekly line 388 uses abs(gd['delta']).
                    'calculated_delta': abs(delta_p),
                    'type': 'p'
                })
            return chain

        return make_chain(curr_expiry), make_chain(next_expiry), make_chain(month_expiry)

    def place_order(self, instrument_key, quantity, side, tag='', expiry=None):
        """Mock Order Placement."""
        price = 0.0
        # Try to parse strike to guess price or just use random/dummy
        print(f"[BACKTEST] {self.current_time} | {side} {quantity} | {instrument_key} | Tag: {tag}")
        return {'status': 'success', 'avg_price': 100.0, 'message': 'Backtest Fill'}
