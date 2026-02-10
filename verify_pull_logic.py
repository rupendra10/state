
import sys
from unittest.mock import MagicMock

# Mock upstox_client and other dependencies before importing strategy
sys.modules['upstox_client'] = MagicMock()
sys.modules['upstox_client.rest'] = MagicMock()
mock_config = MagicMock()
mock_config.TRADING_MODE = 'PAPER'
mock_config.RISK_FREE_RATE = 0.07
mock_config.ORDER_QUANTITY = 65
sys.modules['config'] = mock_config

# Import strategy after mocking
from strategy import CalendarPEWeekly, WeeklyIronfly

def test_calendar_pull():
    strat = CalendarPEWeekly()
    
    # Mock positions
    pos_sell = MagicMock()
    pos_sell.instrument_token = "NSE_FO|40476" # Contains 'NIFTY'? No, Real tokens are like 'NSE_FO|40476'
    # Actually, Upstox tokens for Nifty options usually look like 'NSE_FO|40476'
    # I should check if I should use 'NIFTY' or just 'NSE_FO'.
    # The current code uses 'NIFTY' in p.instrument_token.
    # In my experience, Upstox instrument_token for NIFTY options is like 'NSE_FO|41234'
    # Wait, 'NIFTY' might be in the TRADING_SYMBOL or the name. 
    # Let's check instrument_manager.py to see what instrument_key looks like.
    
    pos_sell.instrument_token = "NSE_FO|NIFTY2610626150PE" # Example format
    pos_sell.net_quantity = -65
    pos_sell.strike_price = 26150.0
    pos_sell.sell_avg_price = 76.5
    pos_sell.expiry = "2026-01-06"
    
    pos_buy = MagicMock()
    pos_buy.instrument_token = "NSE_FO|NIFTY2622426150PE"
    pos_buy.net_quantity = 65
    pos_buy.strike_price = 26150.0
    pos_buy.buy_avg_price = 401.95
    pos_buy.expiry = "2026-02-24"
    
    success = strat.pull_from_broker([pos_sell, pos_buy])
    print(f"Calendar Pull Success: {success}")
    if success:
        print(f"Weekly Strike: {strat.weekly_position['strike']}")
        print(f"Monthly Strike: {strat.monthly_position['strike']}")

def test_ironfly_pull():
    strat = WeeklyIronfly()
    
    # Mock positions (Buy 1 ATM-50, Sell 2 ATM-250, Buy 1 ATM-450)
    p1 = MagicMock()
    p1.instrument_token = "NSE_FO|NIFTY2610626100PE"
    p1.net_quantity = 65
    p1.strike_price = 26100.0 # Leg 1 (inner buy)
    p1.buy_avg_price = 100.0
    p1.expiry = "2026-01-06"
    
    p2 = MagicMock()
    p2.instrument_token = "NSE_FO|NIFTY2610625900PE"
    p2.net_quantity = -130
    p2.strike_price = 25900.0 # Leg 2 (sell 2)
    p2.sell_avg_price = 50.0
    p2.expiry = "2026-01-06"
    
    p3 = MagicMock()
    p3.instrument_token = "NSE_FO|NIFTY2610625700PE"
    p3.net_quantity = 65
    p3.strike_price = 25700.0 # Leg 3 (outer buy)
    p3.buy_avg_price = 20.0
    p3.expiry = "2026-01-06"
    
    success = strat.pull_from_broker([p1, p2, p3])
    print(f"Ironfly Pull Success: {success}")
    if success:
        for i, pos in enumerate(strat.positions):
            print(f"Leg {i+1}: {pos['tag']} | Strike: {pos['strike']} | Qty: {pos['qty']}")

if __name__ == "__main__":
    test_calendar_pull()
    print("-" * 20)
    test_ironfly_pull()
