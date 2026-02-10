import sys
import os
import time
import pandas as pd
from datetime import datetime, timedelta
import config

# Add current dir to path
sys.path.append(os.getcwd())

from backtest_wrapper import BacktestWrapper
from strategies import CalendarPEWeekly, WeeklyIronfly

def run_backtest():
    print("=== STARTING BACKTEST SIMULATION ===")
    
    # 1. Initialize Wrapper (Generates Dummy Data internally if file missing)
    wrapper = BacktestWrapper(
        start_date=datetime(2025, 1, 1, 9, 15),
        end_date=datetime(2025, 1, 5, 15, 30) 
    )
    
    # 2. Initialize Strategy
    # Choose strategy to test
    strategy = CalendarPEWeekly() 
    print(f"Strategy: {strategy.name}")

    # 3. Simulation Loop
    # Iterate over the generated spot data index
    timestamps = wrapper.spot_data.index
    
    total_steps = len(timestamps)
    print(f"Simulating {total_steps} minutes...")
    
    for i, ts in enumerate(timestamps):
        wrapper.set_time(ts)
        
        # 4. Fetch Data
        spot = wrapper.get_spot_price("NIFTY")
        cw, nw, m = wrapper.get_option_chain_data(spot)
        
        # Mock Quotes (Strategy uses this for LTP updates)
        # We need to map instrument_key -> Object with last_price
        class QuoteObj:
            def __init__(self, val): self.last_price = val
            
        quotes = {}
        # Populate quotes from chains
        for chain in [cw, nw, m]:
            for opt in chain:
                quotes[opt['instrument_key']] = QuoteObj(opt['last_price'])
                
        # 5. Build Market Data Packet
        market_data = {
            'spot_price': spot,
            'cw_chain': cw,
            'nw_chain': nw,
            'm_chain': m,
            'quotes': quotes,
            'now': ts,
            'can_enter_new_cycle': True, # Always allow for backtest
            'can_adjust': (ts.minute % 5 == 0), # 5-min candle trigger
            'broker_positions': [], # Mock broker positions (could implement tracking)
            'greeks': {} # We provided pre-calculated in chain
        }
        
        # 6. Run Strategy Update
        # We pass wrapper.place_order as the callback
        strategy.update(market_data, order_callback=wrapper.place_order)
        
        if i % 100 == 0:
            print(f"Processed {i}/{total_steps} steps... Spot: {spot:.2f}")

    print("\n=== BACKTEST COMPLETE ===")
    # Print Strategy Summary
    # The strategy logs trade to CSV, we could print pnl here if we tracked it in wrapper
    print("Check trade_log_calendar.csv for results.")

if __name__ == "__main__":
    run_backtest()
