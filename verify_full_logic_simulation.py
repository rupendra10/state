
import unittest
from unittest.mock import MagicMock
from datetime import datetime, date
import sys
import os

# Ensure we can import strategy
sys.path.append(os.getcwd())
import config
from strategies import CalendarPEWeekly

class MockOrderCallback:
    def __init__(self):
        self.orders = []
        
    def __call__(self, instrument_key, quantity, side, tag, expiry=None):
        self.orders.append({
            'key': instrument_key,
            'qty': quantity,
            'side': side,
            'tag': tag
        })
        return {'status': 'success', 'avg_price': 100.0} # Dummy price

class TestStrategyLogic(unittest.TestCase):
    def setUp(self):
        config.TRADING_MODE = 'PAPER' # Safety
        config.MAX_LOSS_VALUE = 15000
        self.strat = CalendarPEWeekly()
        self.callback = MockOrderCallback()
        
        # Dummy Market Data Structure
        self.spot = 26000
        self.market_data = {
            'spot_price': self.spot,
            'now': datetime(2026, 1, 1, 15, 30), # Normal time
            'cw_chain': [],
            'nw_chain': [],
            'm_chain': [],
            'quotes': {},
            'greeks': {},
            'can_enter_new_cycle': True,
            'can_adjust': True
        }
        
    def _create_option(self, key, strike, delta, price):
        return {
            'instrument_key': key,
            'strike': strike,
            'delta': delta,
            'ltp': price,
            'iv': 0.15,
            'time_to_expiry': 0.05,
            'expiry_dt': '2026-01-01',
            'type': 'p'
        }

    def test_adjustment_trigger_weekly_fall(self):
        print("\n--- Test: Weekly Adjustment on Market Fall ---")
        # Setup: Existing Positions
        self.strat.weekly_position = {'instrument_key': 'W1', 'strike': 26000, 'entry_price': 100, 'qty': 65, 'delta': 0.5, 'expiry_dt': '2026-01-08', 'type': 'p'}
        self.strat.monthly_position = {'instrument_key': 'M1', 'strike': 26000, 'entry_price': 200, 'qty': 65, 'delta': 0.5, 'expiry_dt': '2026-02-24', 'type': 'p'}
        
        # Scenario: Market Crashes, Weekly Delta goes to 0.85 (>= 0.80 trigger)
        self.market_data['greeks'] = {
            'W1': {'delta': -0.85}, 
            'M1': {'delta': -0.50}
        }
        
        # Mock Chain: Fall should target 0.50 (ATM)
        self.market_data['cw_chain'] = [
            self._create_option('W1', 26000, 0.85, 300), 
            self._create_option('W_ATM', 25500, 0.50, 150), # Correct Target (Fall)
            self._create_option('W_OTM', 25200, 0.45, 120) 
        ]
        
        self.strat.update(self.market_data, self.callback)
        
        # Verify: Old Weekly Exited, New Weekly Entered
        entries = [o for o in self.callback.orders if o['tag'] == 'WEEKLY_ROLL_ENTRY']
        
        self.assertTrue(len(entries) > 0, "Should have entered new weekly leg")
        self.assertEqual(entries[0]['key'], 'W_ATM', "Fall adjustment should target ATM (0.50)")
        print("✅ Weekly Adjustment (Fall -> 0.50) Correct")

    def test_adjustment_trigger_weekly_rise(self):
        print("\n--- Test: Weekly Adjustment on Market Rise (Delta Drop) ---")
        # Setup: Market Rallies, Puts become OTM. Delta drops to 0.05 (<= 0.10 trigger)
        self.strat.weekly_position = {'instrument_key': 'W1', 'strike': 26000, 'entry_price': 100, 'qty': 65, 'delta': 0.05, 'expiry_dt': '2026-01-08', 'type': 'p'}
        self.strat.monthly_position = {'instrument_key': 'M1', 'strike': 26000, 'entry_price': 200, 'qty': 65, 'delta': 0.30, 'expiry_dt': '2026-02-24', 'type': 'p'}
        
        self.market_data['greeks'] = {'W1': {'delta': -0.05}} 
        
        # Mock Chain: Rise should target 0.45 (Trend Safety)
        self.market_data['cw_chain'] = [
            self._create_option('W1', 26000, 0.05, 10), 
            self._create_option('W_ATM', 26500, 0.50, 150), 
            self._create_option('W_RALLY_TARGET', 26600, 0.45, 130) # Correct Target (Rise)
        ]
        
        self.strat.update(self.market_data, self.callback)
        
        # Verify
        entries = [o for o in self.callback.orders if o['tag'] == 'WEEKLY_ROLL_ENTRY']
        self.assertTrue(len(entries) > 0, "Should have rolled Weekly leg")
        self.assertEqual(entries[0]['key'], 'W_RALLY_TARGET', "Rise adjustment should target 0.45 (Trend Safety)")
        print("✅ Weekly Rise Adjustment (Rise -> 0.45) Correct")

    def test_gap_opening_forced_roll(self):
        print("\n--- Test: Gap Opening Forced Roll ---")
        # Setup: Market opens 2% lower than entry
        self.strat.weekly_position = {'instrument_key': 'W1', 'strike': 26000, 'entry_price': 100, 'qty': 65, 'delta': 0.60, 'entry_spot': 26000, 'expiry_dt': '2026-01-08', 'type': 'p'}
        self.strat.monthly_position = {'instrument_key': 'M1', 'strike': 26000, 'entry_price': 200, 'qty': 65, 'delta': 0.60, 'expiry_dt': '2026-02-24', 'type': 'p'}
        
        # Market Data at 9:15
        self.market_data['now'] = datetime(2026, 1, 1, 9, 15, 30)
        self.market_data['spot_price'] = 25400 # 600 pts gap down (> 1.25%)
        
        # Even if delta is 0.60 (below 0.80 trigger), gap logic should force roll
        self.market_data['greeks'] = {'W1': {'delta': -0.60}}
        
        self.market_data['cw_chain'] = [self._create_option('W_NEW', 25400, 0.50, 200)]
        
        self.strat.update(self.market_data, self.callback)
        
        entries = [o for o in self.callback.orders if o['tag'] == 'WEEKLY_ROLL_ENTRY']
        self.assertTrue(len(entries) > 0, "Should have forced roll due to Gap > 1.25%")
        print("✅ Gap Opening Protection Triggered Correctly")

    def test_max_loss_exit(self):
        print("\n--- Test: Max Loss Emergency Exit ---")
        # Setup: Deep Loss
        # Weekly: Sold @ 100, Current @ 400. Loss = (100-400)*65 = -19500
        # Monthly: Bought @ 200, Current @ 200. PnL = 0
        # Total = -19500 (Exceeds -15000 limit)
        
        self.strat.weekly_position = {'instrument_key': 'W1', 'strike': 26000, 'entry_price': 100, 'qty': 65, 'expiry_dt': '2026-01-08', 'type': 'p'}
        self.strat.monthly_position = {'instrument_key': 'M1', 'strike': 26000, 'entry_price': 200, 'qty': 65, 'expiry_dt': '2026-02-24', 'type': 'p'}
        
        # Mock Quotes
        w_quote = MagicMock()
        w_quote.last_price = 400.0
        m_quote = MagicMock()
        m_quote.last_price = 200.0
        
        self.market_data['quotes'] = {
            'W1': w_quote,
            'M1': m_quote
        }
        
        self.strat.update(self.market_data, self.callback)
        
        # Verify: Exit All
        exits = [o for o in self.callback.orders if 'EXIT_MAX_LOSS' in o['tag']]
        self.assertTrue(len(exits) >= 2, "Should have exited both legs due to Max Loss")
        print("✅ Max Loss Protection Triggered Correctly")

if __name__ == '__main__':
    unittest.main()
