import sys
import os
import pandas as pd
from datetime import datetime, date, timedelta
from unittest.mock import MagicMock

# Setup path to import strategy
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import config
from strategies.batman_strategy import BatmanStrategy

# Mock Config
config.TRADING_MODE = 'PAPER'
config.BATMAN_ENTRY_WEEKDAY = 2 # Wednesday
config.BATMAN_ENTRY_TIME = "10:00"
config.BATMAN_EXIT_TIME = "15:00"
config.ORDER_QUANTITY = 50

def run_simulation():
    print("=== STARTING BATMAN STRATEGY VERIFICATION ===")
    
    strat = BatmanStrategy()
    
    # ---------------------------------------------------------
    # SCENARIO 1: ENTRY (Wednesday 10:00 AM)
    # ---------------------------------------------------------
    print("\n[SCENARIO 1] Checking Entry on Wednesday 10:00 AM...")
    
    # Mock Data
    spot_price = 24000
    now = datetime(2026, 2, 4, 10, 0, 0) # Feb 4 2026 is a Wednesday
    expiry_date = date(2026, 2, 10) # Feb 10 2026 is Tuesday (Expiry)
    
    # Create Dummy Chain
    # Strikes: 23800, 23900 (Wings), 24100, 24200 (Cores) + Hedges
    chain = []
    strikes = range(23000, 25000, 50)
    for k in strikes:
        # Simple Delta approx
        # Call Delta: 0.5 + (Spot - Strike)/Spot * 5
        c_delta = 0.5 + (spot_price - k)/2000.0
        c_delta = max(0.01, min(0.99, c_delta))
        p_delta = c_delta - 1.0
        
        chain.append({'instrument_key': f"C_{k}", 'strike': k, 'type': 'c', 'delta': c_delta, 'ltp': 100, 'expiry_dt': '2026-02-10', 'time_to_expiry': 0.02})
        chain.append({'instrument_key': f"P_{k}", 'strike': k, 'type': 'p', 'delta': abs(p_delta), 'ltp': 100, 'expiry_dt': '2026-02-10', 'time_to_expiry': 0.02})
    
    market_data = {
        'spot_price': spot_price,
        'cw_chain': chain,
        'now': now,
        'quotes': {},
        'greeks': {}
    }
    
    # Mock Order Callback
    orders = []
    def mock_order(key, qty, side, tag, expiry):
        orders.append({'key': key, 'side': side, 'tag': tag, 'qty': qty})
        return {'status': 'success', 'avg_price': 100.0}
    
    # Run Update
    strat.update(market_data, mock_order)
    
    # Verify Entry Orders
    print(f"Orders Placed: {len(orders)}")
    for o in orders:
        print(f" - {o['side']} {o['qty']} {o['tag']} ({o['key']})")
        
    if len(orders) == 6: # 2 Hedges + 2 Wings + 2 Cores
        print("PASS: Entry Orders Placed Correctly.")
    else:
        print("FAIL: Incorrect Number of Entry Orders.")

    # ---------------------------------------------------------
    # SCENARIO 2: ADJUSTMENT (Market Falls, Call Core Profit)
    # ---------------------------------------------------------
    print("\n[SCENARIO 2] Checking Adjustment (Market Fall)...")
    
    # Clear orders
    orders = []
    
    # Simulate Spot Drop -> Call Deltas Drop
    spot_price = 23800 # Drop 200 pts
    
    # Update Chain Deltas
    for opt in chain:
        k = opt['strike']
        if opt['type'] == 'c':
             # New Delta
             c_delta = 0.5 + (spot_price - k)/2000.0
             opt['delta'] = max(0.01, min(0.99, c_delta))
             # Also update Greeks dict for update_deltas
             market_data['greeks'][opt['instrument_key']] = {'delta': opt['delta']}

    # Specifically set current Core (24200 CE) delta to something low to trigger
    # Original Entry: Spot 24000. CE Core = Spot+200 = 24200.
    # New Spot 23800. 24200 is 400 OTM.
    # Set 24200 CE Delta to 0.15 (Combined 0.30, Trigger < 0.40)
    for opt in chain:
        if opt['instrument_key'] == 'C_24200':
            opt['delta'] = 0.15
            market_data['greeks']['C_24200'] = {'delta': 0.15}
            print(f"DEBUG: Setting C_24200 Delta to {opt['delta']}")
            
    market_data['spot_price'] = spot_price
    market_data['now'] = now + timedelta(days=1) # Thursday
    
    strat.update(market_data, mock_order)
    
    # Should see Exit of C_24200 and Entry of new Core
    print(f"Orders Placed: {len(orders)}")
    entry_core = False
    exit_core = False
    for o in orders:
        print(f" - {o['side']} {o['qty']} {o['tag']} ({o['key']})")
        if "ADJ_EXIT_CE" in o['tag']: exit_core = True
        if "CE_CORE_ADJ" in o['tag']: entry_core = True
        
    if exit_core and entry_core:
        print("PASS: Adjustment Triggered and Executed.")
    else:
        print("FAIL: Adjustment Logic Failed.")

    # ---------------------------------------------------------
    # SCENARIO 3: EXIT (T-1 Day 3:00 PM)
    # ---------------------------------------------------------
    print("\n[SCENARIO 3] Checking T-1 Exit...")
    
    orders = []
    
    # Set time to T-1 (Monday Feb 9) at 15:00
    # Expiry is Tuesday Feb 10
    market_data['now'] = datetime(2026, 2, 9, 15, 0, 1) 
    
    strat.update(market_data, mock_order)
    
    print(f"Orders Placed: {len(orders)}")
    exit_all = False
    if len(orders) > 0 and all(["EXIT" in o['tag'] for o in orders]):
        exit_all = True
        
    if exit_all:
        print("PASS: T-1 Exit Triggered.")
        for o in orders:
             print(f" - {o['side']} {o['qty']} {o['tag']}")
    else:
        print("FAIL: T-1 Exit Not Triggered.")

    # ---------------------------------------------------------
    # SCENARIO 4: RECONCILIATION (Simulate Manual Exit)
    # ---------------------------------------------------------
    print("\n[SCENARIO 4] Checking Reconciliation (Manual Exit)...")
    
    # Reset State for clean test
    strat.positions = [
        {'instrument_key': 'C_24500', 'qty': 50, 'side': 'BUY', 'leg': 'CE_WING', 'strike': 24500, 'type': 'c'},
        {'instrument_key': 'C_24700', 'qty': 100, 'side': 'SELL', 'leg': 'CE_CORE', 'strike': 24700, 'type': 'c'}
    ]
    strat.save_state()
    
    print(f"Initial Positions: {len(strat.positions)}")
    
    # Simulate Broker Data: C_24500 is missing (Closed manually), C_24700 exists (qty 50 - partial exit)
    broker_positions = [
         {'instrument_token': 'C_24700', 'quantity': -50, 'net_quantity': -50}
    ]
    
    # Create market data with broker positions
    market_data['broker_positions'] = broker_positions
    
    # Run Update (which calls pull_from_broker)
    strat.update(market_data, mock_order)
    
    print(f"Final Positions: {len(strat.positions)}")
    
    # Checks
    has_wing = any(p['instrument_key'] == 'C_24500' for p in strat.positions)
    core = next((p for p in strat.positions if p['instrument_key'] == 'C_24700'), None)
    
    if not has_wing:
        print("PASS: C_24500 correctly removed (Reconciliation).")
    else:
        print("FAIL: C_24500 should have been removed.")
        
    if core and core['qty'] == 50:
         print("PASS: C_24700 qty updated to 50.")
    else:
         print(f"FAIL: C_24700 qty mismatch. Expected 50, Got {core['qty'] if core else 'None'}")


    # ---------------------------------------------------------
    # SCENARIO 5: STATE PERSISTENCE (Simulate Restart)
    # ---------------------------------------------------------
    print("\n[SCENARIO 5] Checking State Persistence/Restoral...")
    
    # 1. Setup State: 1 adj count, some positions
    strat.adjustment_count = 1
    strat.positions = [{'instrument_key': 'TEST_KEY', 'qty': 50, 'side': 'BUY', 'leg': 'TEST', 'strike': 10000, 'type': 'c'}]
    strat.save_state() # Writes to file
    
    # 2. "Restart" - Create new instance
    new_strat = BatmanStrategy()
    
    # 3. Load State
    loaded = new_strat.load_previous_state()
    
    if loaded:
        print("PASS: State Loaded.")
    else:
        print("FAIL: State Load Returned False.")
        
    if new_strat.adjustment_count == 1:
        print("PASS: Adjustment Count Restored.")
    else:
        print(f"FAIL: Adjustment Count Mismatch. Expected 1, Got {new_strat.adjustment_count}")
        
    if len(new_strat.positions) == 1 and new_strat.positions[0]['instrument_key'] == 'TEST_KEY':
        print("PASS: Positions Restored.")
    else:
        print("FAIL: Positions Not Restored Correctly.")

if __name__ == "__main__":
    run_simulation()

