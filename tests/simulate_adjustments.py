
import sys
import os
import time
from datetime import datetime, timedelta

# Add parent directory to path to import strategy
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from strategies import CalendarPEWeekly
import config

# Mock Data
MOCK_SPOT = 26000
MOCK_WEEKLY_KEY = "NSE_FO|WEEKLY_PE"
MOCK_MONTHLY_KEY = "NSE_FO|MONTHLY_PE"

# Mock Order Callback
def mock_order_callback(instrument_key, qty, side, tag, expiry='N/A'):
    print(f"  [MOCK EXECUTION] {side} {qty} {instrument_key} (Tag: {tag})")
    return {'status': 'success', 'avg_price': 100.0, 'order_id': 'mock_123'}

def run_simulation():
    print("="*60)
    print("STARTING ADJUSTMENT LOGIC SIMULATION")
    print("="*60)
    
    # 1. Setup Strategy
    strat = CalendarPEWeekly()
    
    # Manually Inject State (As if we already have positions)
    strat.weekly_position = {
        'instrument_key': MOCK_WEEKLY_KEY,
        'strike': 26000,
        'type': 'p',
        'qty': 75,
        'entry_price': 100,
        'expiry_dt': '2026-01-10', # Future date
        'delta': 0.50 # Neutral Start
    }
    strat.monthly_position = {
        'instrument_key': MOCK_MONTHLY_KEY,
        'strike': 26000,
        'type': 'p',
        'qty': 75,
        'entry_price': 200,
        'expiry_dt': '2026-02-24',
        'delta': 0.50 # Neutral Start
    }
    
    # Mock Chains (Needed for selecting new strikes)
    # We provide a "Perfect" ATM option for it to find when it rolls
    target_weekly_chain = [{
        'strike': 26000, 'type': 'p', 'instrument_key': 'NSE_FO|NEW_WEEKLY_ATM', 
        'ltp': 100, 'iv': 0.15, 'time_to_expiry': 0.02, 'delta': 0.50, 'expiry_dt': '2026-01-10', 'calculated_delta': 0.50
    }]
    target_monthly_chain_atm = [{
        'strike': 26000, 'type': 'p', 'instrument_key': 'NSE_FO|NEW_MONTHLY_ATM', 
        'ltp': 200, 'iv': 0.15, 'time_to_expiry': 0.1, 'delta': 0.50, 'expiry_dt': '2026-02-24', 'calculated_delta': 0.50
    }]
    target_monthly_chain_otm = [{
        'strike': 25000, 'type': 'p', 'instrument_key': 'NSE_FO|NEW_MONTHLY_OTM', 
        'ltp': 50, 'iv': 0.15, 'time_to_expiry': 0.1, 'delta': 0.35, 'expiry_dt': '2026-02-24', 'calculated_delta': 0.35
    }]
    
    # Merge chains for simplicity
    w_chain = target_weekly_chain
    m_chain = target_monthly_chain_atm + target_monthly_chain_otm

    # ==========================================
    # TEST CASE 1: NO ACTION (Delta Neutral)
    # ==========================================
    print("\nTest Case 1: Normal Market (Deltas 0.50)")
    strat.weekly_position['delta'] = 0.50
    strat.monthly_position['delta'] = 0.50
    
    acted = strat.check_adjustments(MOCK_SPOT, w_chain, m_chain, mock_order_callback)
    if not acted:
        print("  -> PASSED: No adjustment triggered.")
    else:
        print("  -> FAILED: Unexpected adjustment!")

    # ==========================================
    # TEST CASE 2: WEEKLY LEG (MARKET FALL) -> 0.81 Delta
    # ==========================================
    print("\nTest Case 2: Market Fall (Weekly Delta 0.81)")
    strat.weekly_position['delta'] = 0.81 # Trigger is 0.80
    
    acted = strat.check_adjustments(MOCK_SPOT, w_chain, m_chain, mock_order_callback)
    if acted:
        print("  -> PASSED: Triggered Weekly Roll.")
        # Reset for next test
        strat.weekly_position['delta'] = 0.50 
        strat.weekly_position['instrument_key'] = MOCK_WEEKLY_KEY # Pretend we are back
    else:
        print("  -> FAILED: Did not trigger adjustment!")

    # ==========================================
    # TEST CASE 3: WEEKLY LEG (MARKET RISE) -> 0.09 Delta
    # ==========================================
    print("\nTest Case 3: Market Rise (Weekly Delta 0.09)")
    strat.weekly_position['delta'] = 0.09 # Trigger is 0.10
    
    acted = strat.check_adjustments(MOCK_SPOT, w_chain, m_chain, mock_order_callback)
    if acted:
        print("  -> PASSED: Triggered Weekly Roll.")
        strat.weekly_position['delta'] = 0.50
        strat.weekly_position['instrument_key'] = MOCK_WEEKLY_KEY
    else:
        print("  -> FAILED: Did not trigger adjustment!")

    # ==========================================
    # TEST CASE 4: MONTHLY LEG (MARKET FALL) -> 0.91 Delta
    # ==========================================
    print("\nTest Case 4: Market Crash (Monthly Delta 0.91)")
    strat.monthly_position['delta'] = 0.91 # Trigger is 0.90
    
    acted = strat.check_adjustments(MOCK_SPOT, w_chain, m_chain, mock_order_callback)
    if acted:
        print("  -> PASSED: Triggered Monthly Roll.")
        strat.monthly_position['delta'] = 0.50
    else:
        print("  -> FAILED: Did not trigger adjustment!")

    # ==========================================
    # TEST CASE 5: T-1 ROLLOVER (Date Logic Only)
    # ==========================================
    print("\nTest Case 5: T-1 Rollover Logic")
    # We must construct a specific scenario for 'update' method to catch the T-1
    # Mocking Now as 'Day Before Expiry' at 15:01
    
    import datetime as dt_module
    mock_now = datetime(2026, 1, 5, 15, 1, 0) # e.g. Monday
    
    # Expiry is Tomorrow (Jan 6)
    strat.weekly_position['expiry_dt'] = '2026-01-06'
    
    # We need to construct a fake market_data object
    market_data = {
        'spot_price': MOCK_SPOT,
        'now': mock_now,
        'cw_chain': w_chain,
        'nw_chain': w_chain, # Just reuse
        'm_chain': m_chain,
        'quotes': {},
        'is_day_before_monthly_expiry': False
    }
    
    # We override the strat's log to see output
    old_log = strat.log
    strat.log = lambda x: print(f"  [LOG] {x}")
    
    try:
        strat.update(market_data, mock_order_callback)
        print("  -> PASSED: If [LOG] shows 'T-1 ROLLOVER', test successful.")
    except Exception as e:
        print(f"  -> FAILED: Exception in update loop: {e}")
    finally:
        strat.log = old_log

if __name__ == "__main__":
    run_simulation()
