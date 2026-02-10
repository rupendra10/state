from strategy import CalendarPEWeekly
import unittest

class MockPosition:
    def __init__(self, qty, buy_val, sell_val, avg_price=0.0, day_buy=0.0, day_sell=0.0):
        self.net_quantity = qty
        self.quantity = qty
        self.buy_value = buy_val
        self.sell_value = sell_val
        self.average_price = avg_price
        self.day_buy_price = day_buy
        self.day_sell_price = day_sell
        
        # Standard fields
        self.trading_symbol = "TEST"
        self.instrument_token = "TEST_TOKEN"
        self.strike_price = 0.0
        self.expiry = "N/A"

class TestPriceLogic(unittest.TestCase):
    def setUp(self):
        self.strat = CalendarPEWeekly()

    def test_long_break_even(self):
        # Case from user: Long 65, Buy Val 79153.75, Sell Val 52565.5
        # Expected: (79153.75 - 52565.5) / 65 = 409.05
        p = MockPosition(qty=65, buy_val=79153.75, sell_val=52565.5, day_buy=405.92)
        parsed = self.strat._parse_position(p)
        
        print(f"Long Calculated: {parsed['buy_price']}")
        self.assertAlmostEqual(parsed['buy_price'], 409.05, places=2)
        self.assertEqual(parsed['sell_price'], 0.0)

    def test_short_break_even(self):
        # Case: Short 65. Sell Val 4663.75, Buy Val 0.
        # Expected: 4663.75 / 65 = 71.75
        p = MockPosition(qty=-65, buy_val=0.0, sell_val=4663.75, day_sell=71.75)
        parsed = self.strat._parse_position(p)
        
        print(f"Short Calculated: {parsed['sell_price']}")
        self.assertAlmostEqual(parsed['sell_price'], 71.75, places=2)
        self.assertEqual(parsed['buy_price'], 0.0)

    def test_holdings_priority(self):
        # Case: Holdings exist (average_price > 0). Should ignore Intraday values if they diverge?
        # Actually logic says if avg_price > 0, use it.
        p = MockPosition(qty=50, buy_val=10000, sell_val=0, avg_price=100.0)
        parsed = self.strat._parse_position(p)
        
        print(f"Holding Priority: {parsed['buy_price']}")
        self.assertEqual(parsed['buy_price'], 100.0)

if __name__ == '__main__':
    unittest.main()
