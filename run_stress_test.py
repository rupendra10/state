from backtest_wrapper import BacktestWrapper
from strategies.calendar_pe_weekly import CalendarPEWeekly
import pandas as pd
import numpy as np
import config
from datetime import datetime, timedelta
from colorama import init, Fore, Style

init(autoreset=True)

def run_stress_test():
    print(f"{Fore.RED}=== BLACK SWAN STRESS TEST: 5% GAP DOWN OPEN ==={Style.RESET_ALL}")
    
    # 1. Setup Wrapper
    wrapper = BacktestWrapper()
    
    # 2. GENERATE CUSTOM SCENARIO DATA
    # Day 1: Normal (Spot ~24000) -> Entry
    # Day 2: Gap Down -5% (Spot ~22800) -> Crash
    
    start_d1 = datetime(2025, 1, 1, 15, 0) # Late entry on Day 1
    end_d1 = datetime(2025, 1, 1, 15, 30)
    
    start_d2 = datetime(2025, 1, 2, 9, 15) # Market Open Day 2
    end_d2 = datetime(2025, 1, 2, 9, 20)  # First 5 mins
    
    timestamps = pd.date_range(start=start_d1, end=end_d1, freq='1min').union(
                 pd.date_range(start=start_d2, end=end_d2, freq='1min'))
    
    prices = []
    for ts in timestamps:
        if ts.day == 1:
            prices.append(24000 + np.random.normal(0, 5))
        else:
            # GAP DOWN 5%
            prices.append(22800 + np.random.normal(0, 5))
            
    wrapper.spot_data = pd.DataFrame({'timestamp': timestamps, 'close': prices}).set_index('timestamp')
    print(f"Scenario: Day 1 Spot ~24000. Day 2 Opens ~22800 (-1200 pts / -5%).")

    # 3. Initialize Strategy
    config.MAX_LOSS_VALUE = 25000 # Example Limit
    strategy = CalendarPEWeekly()

    # 4. CUSTOM ORDER CALLBACK (Critical for PnL calculation)
    # We need to capture the price from the current market data to simulate realistic fills
    current_quotes = {} # Will update in loop

    def accurate_fill_callback(instrument_key, quantity, side, tag, expiry=None):
        # Look up LTP
        fill_price = 0.0
        if instrument_key in current_quotes:
            fill_price = current_quotes[instrument_key].last_price
        else:
            # Fallback if key missing (shouldn't happen if logic is correct)
            fill_price = 100.0 
            
        print(f"[{wrapper.current_time}] ORDER: {side} {quantity} {instrument_key} @ {fill_price:.2f} | {tag}")
        return {'status': 'success', 'avg_price': fill_price}

    # 5. SIMULATION LOOP
    for ts in timestamps:
        wrapper.set_time(ts)
        spot = wrapper.get_spot_price("NIFTY")
        
        if ts == start_d2:
            print(f"\n{Fore.RED}!!! DAY 2 MARKET OPEN - CRASH DETECTED (Spot: {spot:.2f}) !!!{Style.RESET_ALL}")

        cw, nw, m = wrapper.get_option_chain_data(spot)
        
        # Build Quotes map for this tick
        class QuoteObj:
             def __init__(self, val): self.last_price = val
        
        current_quotes = {}
        for chain in [cw, nw, m]:
            for opt in chain:
                current_quotes[opt['instrument_key']] = QuoteObj(opt['last_price'])
                
        market_data = {
            'spot_price': spot,
            'cw_chain': cw, 'nw_chain': nw, 'm_chain': m,
            'quotes': current_quotes, 'now': ts,
            'can_enter_new_cycle': True,
            'can_adjust': True, # Always allow adj for stress test
            'broker_positions': [], 
            'greeks': {}
        }
        
        strategy.update(market_data, order_callback=accurate_fill_callback)
        
        # Check if exited
        if ts.day == 2 and strategy.weekly_position is None and strategy.monthly_position is None:
            print(f"\n{Fore.GREEN}SUCCESS: Strategy successfully exited all positions.{Style.RESET_ALL}")
            break

if __name__ == "__main__":
    run_stress_test()
