import config
from datetime import datetime, timedelta
from greeks import calculate_delta, get_atm_strike
from colorama import init, Fore, Style
from trade_logger import TradeJournal, EventLogger
from base_strategy import BaseStrategy
from utils import get_ist_now
import re

# Initialize colorama for Windows support
init(autoreset=True)

class CalendarPEWeekly(BaseStrategy):
    def __init__(self, risk_free_rate=config.RISK_FREE_RATE):
        super().__init__("CalendarPEWeekly")
        # State
        self.weekly_position = None  # {'type': 'sell', 'strike': K, 'expiry': T, 'entry_price': P, 'delta': D}
        self.monthly_position = None # {'type': 'buy',  'strike': K, 'expiry': T, 'entry_price': P, 'delta': D}
        self.last_rollover_date = None  # Track last Monday rollover to prevent duplicates
        self.last_process_date = None  # To detect market open across days
        
        self.risk_free_rate = risk_free_rate
        self.logs = []
        self.journal = TradeJournal(filename="trade_log_calendar.csv")
        self.event_logger = EventLogger()
        self.last_failed_entry_time = 0 # Unix timestamp to prevent rapid re-entry

    def log(self, message):
        timestamp = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Color specific keywords in message
        colored_message = message.replace("SOLD", f"{Fore.RED}SOLD{Style.RESET_ALL}")
        colored_message = colored_message.replace("BOUGHT", f"{Fore.GREEN}BOUGHT{Style.RESET_ALL}")
        colored_message = colored_message.replace("ENTRY", f"{Fore.YELLOW}ENTRY{Style.RESET_ALL}")
        colored_message = colored_message.replace("ADJUSTMENT", f"{Fore.MAGENTA}ADJUSTMENT{Style.RESET_ALL}")
        
        # Color Dates
        date_pattern = r"(\d{4}-\d{2}-\d{2})"
        colored_message = re.sub(date_pattern, rf"{Fore.CYAN}\1{Style.RESET_ALL}", colored_message)

        entry = f"[{timestamp}] [{self.name}] {colored_message}"
        print(entry)
        self.event_logger.log(f"[{self.name}] {message}") # Writes stripped clean msg to file
        self.logs.append(entry)

    def _is_expiry_tomorrow(self, expiry_date_str):
        """Checks if the given expiry date is exactly tomorrow."""
        if not expiry_date_str or expiry_date_str == 'N/A':
            return False
            
        try:
            today = datetime.datetime.now().date()
            expiry = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
            return expiry == (today + datetime.timedelta(days=1))
        except:
            return False

    def update(self, market_data, order_callback):
        """
        Main logic loop for Calendar PE Weekly.
        market_data: {spot_price, weekly_chain, monthly_chain, quotes, now, master_flags...}
        """
        # --- 0. Reconciliation on Every Loop (Silent) ---
        # Ensures that if we manually closed something, the algo knows about it immediately.
        # This prevents "Double Entry" or "Ghost Position" issues.
        if market_data.get('broker_positions') is not None:
             self.pull_from_broker(market_data.get('broker_positions'), master_df=market_data.get('master_df'), silent=True)

        spot = market_data.get('spot_price')
        # Standard Chains (Current Week, Current Month)
        weekly_chain = market_data.get('cw_chain', [])
        monthly_chain = market_data.get('m_chain', [])
        
        # Next Week Chain (For Day-Before-Expiry Logic)
        next_weekly_chain = market_data.get('nw_chain', [])

        quote_map = market_data.get('quotes', {})
        now = market_data.get('now')
        is_opening_window = market_data.get('is_opening_window', False)

        if spot is None:
            return


        
        # 0. Recovery: If positions exist but expiry_dt is missing or N/A (old state), try to find it in chains
        if self.weekly_position:
            exp = self.weekly_position.get('expiry_dt')
            if not exp or exp == 'N/A':
                match = next((x for x in weekly_chain if x['instrument_key'] == self.weekly_position['instrument_key']), None)
                if match:
                    self.weekly_position['expiry_dt'] = match['expiry_dt']
        
        if self.monthly_position:
            exp = self.monthly_position.get('expiry_dt')
            if not exp or exp == 'N/A':
                # Check monthly chain first, then next weekly (sometimes calendars use diff weeks)
                match = next((x for x in monthly_chain if x['instrument_key'] == self.monthly_position['instrument_key']), None)
                if not match:
                    # Fallback check in current/next weekly chains in case of poor classification
                    match = next((x for x in weekly_chain + next_weekly_chain if x['instrument_key'] == self.monthly_position['instrument_key']), None)
                if match:
                    self.monthly_position['expiry_dt'] = match['expiry_dt']

        # 0.1 Auto-Cleanup for Expired Positions
        today_str = now.strftime("%Y-%m-%d")
        if self.weekly_position and self.weekly_position.get('expiry_dt'):
            if self.weekly_position['expiry_dt'] < today_str:
                self.log(f"{Fore.RED}AUTO-CLEANUP: Weekly leg {self.weekly_position['strike']} Put expired on {self.weekly_position['expiry_dt']}. Clearing state.{Style.RESET_ALL}")
                self.weekly_position = None
                self.save_state()
        if self.monthly_position and self.monthly_position.get('expiry_dt'):
            if self.monthly_position['expiry_dt'] < today_str:
                self.log(f"{Fore.RED}AUTO-CLEANUP: Monthly leg {self.monthly_position['strike']} Put expired on {self.monthly_position['expiry_dt']}. Clearing state.{Style.RESET_ALL}")
                self.monthly_position = None
                self.save_state()

        has_acted = False


        # --- AUTO-EXIT BEFORE MONTHLY EXPIRY (SAFETY) ---
        if config.AUTO_EXIT_BEFORE_MONTHLY_EXPIRY_3PM:
            is_day_before = market_data.get('is_day_before_monthly_expiry', False)
            trigger_date = market_data.get('monthly_expiry_trigger_date') # Date object

            if is_day_before and now.strftime("%H:%M") >= "15:00":
                # SAFETY CHECK: Only exit if our positions are actually expiring on/before this date
                # If we have already rolled to next month, DON'T exit.
                should_exit = False
                
                # Check Weekly Leg
                if self.weekly_position:
                    w_exp_str = self.weekly_position.get('expiry_dt', 'N/A')
                    if w_exp_str != 'N/A' and trigger_date:
                        try:
                            w_exp = datetime.strptime(w_exp_str, '%Y-%m-%d').date()
                            if w_exp <= trigger_date:
                                should_exit = True
                                self.log(f"AUTO-EXIT TRIGGER: Weekly Leg expires ({w_exp}) on/before Monthly End ({trigger_date}).")
                            else:
                                self.log(f"AUTO-EXIT SKIP: Weekly Leg expiry ({w_exp}) is AFTER Monthly End ({trigger_date}). Safe.")
                        except:
                             should_exit = True # Fallback to safe exit on error
                    else:
                        should_exit = True # Exit if no expiry data
                
                # Check Monthly Leg (Logic: If weekly is safe, usually we are safe, but check mostly for completeness)
                # Actually, if Weekly is safe (Feb), but Monthly is Jan (mismatch), we should probably exit Monthly?
                # But let's assume if Weekly is safe, the 'Strategy' is safe from being "Left Behind".
                
                if should_exit:
                    self.log(f"{Fore.MAGENTA}AUTO-EXIT: Day before Monthly Expiry (3 PM Trigger). Exiting all positions.{Style.RESET_ALL}")
                    self.exit_all_positions(order_callback, reason="PRE_EXPIRY_EXIT")
                    has_acted = True
                    self.save_state()
                    return

        # --- T-1 ROLLOVER PROTECTION ---
        if config.ROLLOVER_ON_T1_ENABLED and self.weekly_position:
            expiry_dt_str = self.weekly_position.get('expiry_dt')
            if expiry_dt_str and expiry_dt_str != 'N/A':
                expiry_dt = datetime.strptime(expiry_dt_str, '%Y-%m-%d').date()
                today = now.date()
                # If expiry is the Next Trading Day (T-1 Check)
                from utils import get_next_trading_day
                next_trading_day = get_next_trading_day(today)
                if expiry_dt == next_trading_day:
                    if now.strftime("%H:%M") >= config.EARLY_ROLLOVER_TIME:
                        self.log(f"{Fore.YELLOW}T-1 ROLLOVER: Expiry is Next Trading Day ({next_trading_day}). Scaling out.{Style.RESET_ALL}")
                        
                        # 1. Roll Weekly to Next Week
                        # T-1 Rollover is effectively a new entry for next week, so use Entry Target (0.50)
                        self.adjust_weekly_leg(spot, next_weekly_chain, config.ENTRY_WEEKLY_DELTA_TARGET, order_callback)
                        
                        # Linked Roll REMOVED based on user request. 
                        # Monthly leg will now ONLY roll if its own specific Delta triggers are hit (in check_adjustments).
                        
                        has_acted = True
                        self.save_state()
                        return

        # 1. Update Deltas for existing positions
        self.update_deltas(spot, 
                           market_data,
                           current_time_to_expiry_weekly=weekly_chain[0]['time_to_expiry'] if weekly_chain else 0.01,
                           current_time_to_expiry_monthly=monthly_chain[0]['time_to_expiry'] if monthly_chain else 0.01,
                           weekly_iv=0.15, monthly_iv=0.15)

        # 2. Check Entry Logic
        # Relaxed trigger: Enter if either leg is missing (allows reconciliation of partial entries)
        if not has_acted and not (self.weekly_position and self.monthly_position):
            # Re-entry cooldown: Wait at least 5 minutes after a failed entry attempt
            import time
            if time.time() - self.last_failed_entry_time < 300: # 300s = 5 mins
                if now.second < 10 and now.minute % 5 == 0:
                    self.log(f"{Fore.YELLOW}RE-ENTRY COOLDOWN: Waiting for stability after last timeout/failure...{Style.RESET_ALL}")
                return

            can_enter = market_data.get('can_enter_new_cycle', True)
            
            # In LIVE mode with strict entry, check if today is monthly expiry
            if config.TRADING_MODE == 'LIVE' and config.STRICT_MONTHLY_EXPIRY_ENTRY:
                # Get monthly expiry from market_data
                monthly_chain = market_data.get('m_chain', [])
                if monthly_chain:
                    # Extract expiry date from first option in monthly chain
                    monthly_expiry_str = monthly_chain[0].get('expiry_dt', '')
                    try:
                        monthly_expiry_date = datetime.strptime(monthly_expiry_str, '%Y-%m-%d').date()
                        is_monthly_expiry_today = (now.date() == monthly_expiry_date)
                        current_time_str = now.strftime("%H:%M")
                        
                        if getattr(config, 'OVERRIDE_TIMING_CHECKS', False):
                            can_enter = True
                        elif is_monthly_expiry_today and current_time_str >= config.ENTRY_TIME_HHMM:
                            can_enter = True
                        else:
                            can_enter = False
                    except:
                        can_enter = False
            
            if can_enter:
                self.enter_strategy(spot, weekly_chain, monthly_chain, order_callback=order_callback, market_data=market_data)
                has_acted = True
                self.save_state()
            else:
                if now.second < 10 and now.minute % 5 == 0: # Log every 5 mins in the first 10s
                    if config.TRADING_MODE == 'LIVE' and config.STRICT_MONTHLY_EXPIRY_ENTRY:
                        monthly_chain = market_data.get('m_chain', [])
                        if monthly_chain:
                            monthly_expiry_str = monthly_chain[0].get('expiry_dt', 'N/A')
                            self.log(f"{Fore.YELLOW}[LIVE MODE] WAITING: Entry allowed only on Monthly Expiry ({monthly_expiry_str}) at {config.ENTRY_TIME_HHMM}. Today is {now.strftime('%Y-%m-%d %H:%M')}.{Style.RESET_ALL}")
                        else:
                            self.log(f"{Fore.YELLOW}[LIVE MODE] WAITING: Entry allowed only on Monthly Expiry Day at {config.ENTRY_TIME_HHMM}.{Style.RESET_ALL}")
                    else:
                        self.log("WAITING: Cycle entry conditions not yet met.")
        
        elif not has_acted and not self.weekly_position:
            # Re-entry after rollover or leg specific exit
            self.log("Re-entering Weekly Leg")
            self.adjust_weekly_leg(spot, weekly_chain, order_callback)
            has_acted = True
            self.save_state()

        # 3. Check Portfolio Risk
        w_ltp = None
        m_ltp = None
        if self.weekly_position:
            obj = quote_map.get(self.weekly_position['instrument_key'])
            w_ltp = getattr(obj, 'last_price', None) if obj else None
        if self.monthly_position:
            obj = quote_map.get(self.monthly_position['instrument_key'])
            m_ltp = getattr(obj, 'last_price', None) if obj else None

        if not has_acted and self.check_portfolio_risk(w_ltp, m_ltp, order_callback):
            has_acted = True
            self.save_state()
            return

        # 4. Check Adjustments (ONLY on candle marks)
        can_adjust = market_data.get('can_adjust', True)
        
        # --- OPENING GAP PROTECTION ---
        is_opening_window = False
        if config.GAP_PROTECTION_ENABLED:
            current_time = now.strftime("%H:%M")
            if "09:15" <= current_time <= f"09:{15 + config.OPENING_VOLATILITY_WINDOW_MINS:02d}":
                is_opening_window = True
                
        if is_opening_window and self.weekly_position and self.monthly_position:
            # Bypass candle delay and check for extreme gaps
            if self.check_gap_risk(spot, w_ltp, m_ltp, order_callback):
                has_acted = True
                self.save_state()
                return

        adjustment_made = False
        
        # Prevent double adjustment if we just entered or rolled over in this cycle
        if not has_acted and (can_adjust or is_opening_window) and self.weekly_position and self.monthly_position:
            adjustment_made = self.check_adjustments(spot, weekly_chain, monthly_chain, order_callback=order_callback, is_opening_window=is_opening_window, next_weekly_chain=next_weekly_chain, now=now)
            
        if adjustment_made:
            has_acted = True
            self.save_state()

        # 5. PnL Summary Logging
        open_pnl = self.get_open_pnl(w_ltp, m_ltp)
        
        # Calculate TOTAL Broker P&L (Realized + Unrealized) from raw broker positions
        broker_total_pnl = 0.0
        broker_positions = market_data.get('broker_positions', [])
        if broker_positions:
            for p in broker_positions:
                # Formula: (SellValue - BuyValue) + (NetQty * LTP)
                # Upstox API fields: sell_value, buy_value, net_quantity, last_price
                # Note: last_price might be 0 if not updated, use 0 in that case
                sv = float(getattr(p, 'sell_value', 0.0))
                bv = float(getattr(p, 'buy_value', 0.0))
                nq = int(getattr(p, 'net_quantity', 0))
                ltp = float(getattr(p, 'last_price', 0.0))
                
                # If LTP is 0 (closed or error), check if we can get it from quote_map if net_qty != 0
                if ltp == 0.0 and nq != 0:
                    key = getattr(p, 'instrument_token', None)
                    if key and key in quote_map:
                        ltp = quote_map[key].last_price
                
                pnl = (sv - bv) + (nq * ltp)
                broker_total_pnl += pnl

        strategy_state = {
            'weekly': self.weekly_position,
            'monthly': self.monthly_position,
            'weekly_ltp': w_ltp,
            'monthly_ltp': m_ltp
        }
        self.journal.print_summary(open_pnl, strategy_state, broker_pnl=broker_total_pnl)

    def select_strike_by_delta(self, spot, chain, target_delta, option_type='p', tolerance=0.1, force_round=False, force_atm=False):
        """
        Finds a strike in the option chain closest to the target delta.
        chain: list of dicts {'strike': K, 'iv': sigma, 'expiry': t_years}
        force_round: If True, STRICTLY limits search to strikes divisible by 100.
        force_atm: If True, IGNORES delta and finds strike closest to Round-100 Spot.
        """
        best_strike = None
        min_diff = float('inf')
        
        # Filter candidates by type first
        candidates = [opt for opt in chain if opt.get('type') == option_type]
        
        # 0. Round Strike Optimization (Liquidity)
        # If enabled key is True globaly OR forced locally
        use_round_strikes = getattr(config, 'PREFER_ROUND_STRIKES', False) or force_round or force_atm # force_atm implies round 100
        
        if use_round_strikes:
            round_candidates = []
            for opt in candidates:
                try:
                    if int(float(opt['strike'])) % 100 == 0:
                        round_candidates.append(opt)
                except:
                    pass
            
            if round_candidates:
                candidates = round_candidates
            elif force_round or force_atm:
                self.log(f"WARNING: No Round-100 strikes found. Force Round/ATM is ON.")
                return None

        # 1. SPECIAL MODE: Force ATM (Distance from Spot)
        if force_atm:
             # Find closest strike to SPOT (rounded to 100)
             target_strike = round(spot / 100) * 100
             for opt in candidates:
                 diff = abs(float(opt['strike']) - target_strike)
                 if diff < min_diff:
                     min_diff = diff
                     best_strike = opt
             
             if best_strike and min_diff > 200:
                  self.log(f"WARNING: Closest ATM strike ({best_strike['strike']}) is far from Target ({target_strike}).")
             
             return best_strike

        # 2. STANDARD MODE: Delta Based
        min_diff = float('inf') # Reset
        for opt in candidates:
            # Prefer strikes with non-zero IV if possible
            if opt.get('iv', 0) <= 0:
                continue
                
            delta = opt.get('delta')
            if delta is None:
                 delta = opt.get('calculated_delta', 0.5)
            diff = abs(abs(delta) - target_delta)
            
            if diff < min_diff:
                min_diff = diff
                best_strike = opt
        
        return best_strike
    def enter_strategy(self, spot, weekly_chain, monthly_chain, order_callback=None, market_data=None):
        self.log(f"Attempting Atomic Entry at Spot: {spot}")
        
        # RESET partial state but allow it to be repopulated by reconciliation
        self.weekly_position = None
        self.monthly_position = None

        # 0. MANDATORY RECONCILIATION: Check if legs already exist (ghost positions)
        if market_data:
            broker_positions = market_data.get('broker_positions', [])
            if broker_positions:
                self.pull_from_broker(broker_positions)

        if self.weekly_position and self.monthly_position:
            self.log("RECONCILIATION: Strategy already fully entered. Skipping fresh entry.")
            return

        # 1. Select Legs
        weekly_leg = self.select_strike_by_delta(spot, weekly_chain, config.ENTRY_WEEKLY_DELTA_TARGET, force_round=False)
        monthly_leg = self.select_strike_by_delta(spot, monthly_chain, config.ENTRY_MONTHLY_DELTA_TARGET, force_round=True, force_atm=True)

        if not weekly_leg or not monthly_leg:
            self.log("ERROR: Could not find suitable strikes for both legs. Aborting entry.")
            return

        # 2. Execute Entry (BUY Monthly first for margin, then SELL Weekly)
        if order_callback:
            # Check if Monthly Hedge already exists (Reconciliation)
            if self.monthly_position:
                self.log(f"RECONCILIATION: Monthly leg {self.monthly_position['instrument_key']} already exists. Skipping Buy order.")
                # Keep existing state, do NOT overwrite
            else:
                resp_m = order_callback(monthly_leg['instrument_key'], config.ORDER_QUANTITY, 'BUY', 'MONTHLY_ENTRY', expiry=monthly_leg.get('expiry_dt'))
            
                if resp_m and (resp_m.get('status') == 'success'):
                    # Capture execution price
                    entry_price_m = resp_m.get('avg_price', monthly_leg.get('ltp', 0.0))
                    self.monthly_position = {
                        'leg': 'monthly_buy',
                        'strike': monthly_leg['strike'],
                        'expiry': monthly_leg['time_to_expiry'],
                        'iv': monthly_leg['iv'],
                        'delta': monthly_leg.get('delta', monthly_leg.get('calculated_delta', 0.5)),
                        'entry_spot': spot,
                        'instrument_key': monthly_leg['instrument_key'],
                        'entry_price': entry_price_m,
                        'type': monthly_leg.get('type', 'p'),
                        'expiry_dt': monthly_leg.get('expiry_dt')
                    }
                    self.journal.log_trade(monthly_leg['instrument_key'], 'BUY', config.ORDER_QUANTITY, entry_price_m, 'MONTHLY_ENTRY', expiry=monthly_leg.get('expiry_dt'))
                    self.log(f"ENTRY: BOUGHT Monthly Put | Strike: {monthly_leg['strike']} | Price: {entry_price_m} | Expiry: {monthly_leg['expiry_dt']} | Delta: {monthly_leg.get('delta', 0):.2f}")
                else:
                    reason_m = resp_m.get('message', 'Unknown Error')
                    self.log(f"CRITICAL ERROR: Monthly Buy Order FAILED - {reason_m}. Aborting entry.")
                    import time
                    self.last_failed_entry_time = time.time()
                    return

            # 2. Sell Weekly
            if self.weekly_position:
                self.log(f"RECONCILIATION: Weekly leg {self.weekly_position['instrument_key']} already exists. Skipping Sell order.")
                # Keep existing state
            else:
                resp_w = order_callback(weekly_leg['instrument_key'], config.ORDER_QUANTITY, 'SELL', 'WEEKLY_ENTRY', expiry=weekly_leg.get('expiry_dt'))
            
                if resp_w and (resp_w.get('status') == 'success'):
                    # Capture execution price
                    entry_price = resp_w.get('avg_price', weekly_leg.get('ltp', 0.0))
                    self.weekly_position = {
                        'leg': 'weekly_sell',
                        'strike': weekly_leg['strike'],
                        'expiry': weekly_leg['time_to_expiry'],
                        'iv': weekly_leg['iv'],
                        'delta': weekly_leg.get('delta', weekly_leg.get('calculated_delta', 0.5)),
                        'entry_spot': spot,
                        'instrument_key': weekly_leg['instrument_key'],
                        'entry_price': entry_price,
                        'type': weekly_leg.get('type', 'p'),
                        'expiry_dt': weekly_leg.get('expiry_dt')
                    }
                    self.journal.log_trade(weekly_leg['instrument_key'], 'SELL', config.ORDER_QUANTITY, entry_price, 'WEEKLY_ENTRY', expiry=weekly_leg.get('expiry_dt'))
                    self.log(f"ENTRY: SOLD Weekly Put | Strike: {weekly_leg['strike']} | Price: {entry_price} | Expiry: {weekly_leg['expiry_dt']} | Delta: {weekly_leg.get('delta', 0):.2f}")
                else:
                    reason = resp_w.get('message', 'Unknown Error')
                    self.log(f"CRITICAL ERROR: Weekly Sell Order FAILED - {reason}")
                    # EMERGENCY: Monthly was bought, but Weekly failed. 
                    # CHANGE (Stability): Do NOT square off Monthly. It serves as a Long Hedge.
                    # We will simply retry the Weekly Entry on the next loop or manual intervention.
                    self.log("WARNING: Retaining Monthly leg as hedge despite Weekly Entry failure.")
                    # self.log("EMERGENCY: Squaring off Monthly leg.")
                    # resp_exit = order_callback(monthly_leg['instrument_key'], config.ORDER_QUANTITY, 'SELL', 'EMERGENCY_EXIT', expiry=monthly_leg.get('expiry_dt'))
                    # exit_price = resp_exit.get('avg_price', 0.0)
                    # pnl = (exit_price - self.monthly_position['entry_price']) * config.ORDER_QUANTITY
                    # self.journal.log_trade(monthly_leg['instrument_key'], 'SELL', config.ORDER_QUANTITY, exit_price, 'EMERGENCY_EXIT', expiry=monthly_leg.get('expiry_dt'), pnl=pnl)
                    # self.monthly_position = None
                    import time
                    self.last_failed_entry_time = time.time()
                return
        # else:
        #     self.log("DEV NOTE: No order_callback provided, entry skipped.")

    def update_deltas(self, spot, market_data, current_time_to_expiry_weekly, current_time_to_expiry_monthly, weekly_iv, monthly_iv):
        """
        Update deltas for existing positions. Prefer broker greeks if available.
        """
        greeks = market_data.get('greeks', {})
        
        if self.weekly_position:
            key = self.weekly_position['instrument_key']
            source = "CALC"
            broker_iv = weekly_iv # Default fallback
            
            if key in greeks:
                gd = greeks[key]
                if gd.get('delta') is not None:
                    self.weekly_position['delta'] = abs(gd['delta'])
                    source = "BROKER"
                elif gd.get('iv') is not None and gd.get('iv') > 0:
                     broker_iv = gd['iv']
                     source = "CALC(BrokerIV)"
            
            if source != "BROKER":
                p_type = self.weekly_position.get('type', 'p')
                d = calculate_delta(p_type, spot, self.weekly_position['strike'], current_time_to_expiry_weekly, self.risk_free_rate, broker_iv)
                self.weekly_position['delta'] = abs(d)
                
            self.weekly_position['expiry'] = current_time_to_expiry_weekly
            self.log(f"Weekly Delta Update: {self.weekly_position['delta']:.4f} ({source}) | Key: {key}")
        
        if self.monthly_position:
            key = self.monthly_position['instrument_key']
            source = "CALC"
            broker_iv = monthly_iv # Default fallback
            
            if key in greeks:
                gd = greeks[key]
                if gd.get('delta') is not None:
                    self.monthly_position['delta'] = abs(gd['delta'])
                    source = "BROKER"
                elif gd.get('iv') is not None and gd.get('iv') > 0:
                     broker_iv = gd['iv']
                     source = "CALC(BrokerIV)"
            
            if source != "BROKER":
                p_type = self.monthly_position.get('type', 'p')
                d = calculate_delta(p_type, spot, self.monthly_position['strike'], current_time_to_expiry_monthly, self.risk_free_rate, broker_iv)
                self.monthly_position['delta'] = abs(d)
                
            self.monthly_position['expiry'] = current_time_to_expiry_monthly
            self.log(f"Monthly Delta Update: {self.monthly_position['delta']:.4f} ({source}) | Key: {key}")

    def check_adjustments(self, spot, weekly_chain, monthly_chain, order_callback=None, is_opening_window=False, next_weekly_chain=None, now=None):
        """
        Logic 3: Adjustment.
        is_opening_window: If True, we might trigger rolls even if delta hasn't hit thresholds if gap is high.
        """
        if not self.weekly_position or not self.monthly_position:
            return False

        # --- DATA INTEGRITY SAFETY CHECK ---
        # Prevent "Rise" adjustment triggering on invalid 0.0 delta from broker
        if self.weekly_position['delta'] < 0.1:
            if spot < self.weekly_position['strike']:
                self.log(f"WARNING: Suspicious Weekly Delta ({self.weekly_position['delta']}) for ITM Put (Strike {self.weekly_position['strike']} > Spot {spot}). Ignoring Adjustment.")
                return False
        
        if self.monthly_position['delta'] < 0.1:
            if spot < self.monthly_position['strike']:
                self.log(f"WARNING: Suspicious Monthly Delta ({self.monthly_position['delta']}) for ITM Put (Strike {self.monthly_position['strike']} > Spot {spot}). Ignoring Adjustment.")
                return False

        adjustment_made = False
        
        # 0. Gap Forced Roll Check
        if is_opening_window:
            ref_spot = self.weekly_position.get('entry_spot', spot)
            gap_pct = abs(spot - ref_spot) / ref_spot * 100
            if gap_pct >= config.GAP_FORCED_ROLL_THRESHOLD_PCT:
                self.log(f"GAP FORCED ROLL: Market open gap {gap_pct:.2f}% exceeds {config.GAP_FORCED_ROLL_THRESHOLD_PCT}%. Repositioning.")
                # Gap Roll is an emergency reset -> Use FALL target (0.50) for safety/max premium
                self.adjust_weekly_leg(spot, weekly_chain, config.WEEKLY_ROLL_TARGET_DELTA_FALL, order_callback)
                # For Calendar, we usually adjust weekly. If monthly is also far, it will roll on its own delta check below.
                return True

        # 1. Weekly Put (Sell Leg) Adjustments
        # On a Market Fall: If delta increases to 0.80
        if self.weekly_position['delta'] >= config.WEEKLY_ADJ_TRIGGER_DELTA:
            self.log(f"WEEKLY ADJ (FALL): Delta is {self.weekly_position['delta']:.2f} >= {config.WEEKLY_ADJ_TRIGGER_DELTA}")
            self.adjust_weekly_leg(spot, weekly_chain, config.WEEKLY_ROLL_TARGET_DELTA_FALL, order_callback)
            adjustment_made = True

        # On a Market Rise: If delta drops to 0.10 or below
        elif self.weekly_position['delta'] <= config.WEEKLY_ADJ_TRIGGER_DELTA_LOW:
            self.log(f"WEEKLY ADJ (RISE): Delta is {self.weekly_position['delta']:.2f} <= {config.WEEKLY_ADJ_TRIGGER_DELTA_LOW}")
            self.adjust_weekly_leg(spot, weekly_chain, config.WEEKLY_ROLL_TARGET_DELTA_RISE, order_callback)
            adjustment_made = True

        # 2. Next-Month Put (Buy Leg) Adjustments
        # On a Sharp Market Fall: If delta reaches 0.90
        if self.monthly_position['delta'] >= config.MONTHLY_ADJ_TRIGGER_DELTA:
            self.log(f"MONTHLY ADJ (FALL): Delta is {self.monthly_position['delta']:.2f} >= {config.MONTHLY_ADJ_TRIGGER_DELTA}")
            self.adjust_monthly_leg(spot, monthly_chain, config.MONTHLY_ROLL_TARGET_DELTA_FALL, order_callback, force_atm=True)
            adjustment_made = True

        # On a Sharp Market Rise: If delta drops to 0.10
        elif self.monthly_position['delta'] <= config.MONTHLY_ADJ_TRIGGER_DELTA_LOW:
            self.log(f"MONTHLY ADJ (RISE): Delta is {self.monthly_position['delta']:.2f} <= {config.MONTHLY_ADJ_TRIGGER_DELTA_LOW}")
            self.adjust_monthly_leg(spot, monthly_chain, config.MONTHLY_ROLL_TARGET_DELTA_RISE, order_callback, force_atm=False)
            adjustment_made = True
        
        return adjustment_made

    def adjust_weekly_leg(self, spot, chain, target_delta, order_callback):
        # 1. Select New Leg first to ensure we have a target
        new_leg = self.select_strike_by_delta(spot, chain, target_delta)
        if not new_leg:
            self.log("ERROR: Could not find suitable new Weekly strike for adjustment. Skipping.")
            return

        # 2. Exit Existing
        if order_callback and self.weekly_position:
            qty = self.weekly_position.get('qty', config.ORDER_QUANTITY)
            resp_exit = order_callback(self.weekly_position['instrument_key'], qty, 'BUY', 'WEEKLY_EXIT_ADJ', expiry=self.weekly_position.get('expiry_dt'))
            if (resp_exit and resp_exit.get('status') == 'success'):
                # PNL = (Entry - Exit) for Sell side
                exit_price = resp_exit.get('avg_price', 0.0)
                pnl = (self.weekly_position['entry_price'] - exit_price) * qty
                self.journal.log_trade(self.weekly_position['instrument_key'], 'BUY', qty, exit_price, 'WEEKLY_EXIT_ADJ', expiry=self.weekly_position.get('expiry_dt'), pnl=pnl)
            else:
                reason = resp_exit.get('message', 'Unknown Error')
                self.log(f"CRITICAL ERROR: Weekly Exit Order FAILED - {reason}. Aborting adjustment to prevent double selling.")
                return
        
        self.weekly_position = None
        self.save_state() # Atomic Save: Ensure we don't think we're still short if we crash/stop here
        
        # 3. Enter New
        if order_callback:
            resp_entry = order_callback(new_leg['instrument_key'], config.ORDER_QUANTITY, 'SELL', 'WEEKLY_ROLL_ENTRY', expiry=new_leg.get('expiry_dt'))
            if resp_entry and (resp_entry.get('status') == 'success'):
                entry_price = resp_entry.get('avg_price', new_leg.get('ltp', 0.0))
                self.weekly_position = {
                    'leg': 'weekly_sell',
                    'strike': new_leg['strike'],
                    'expiry': new_leg['time_to_expiry'],
                    'iv': new_leg['iv'],
                    'delta': new_leg.get('delta', new_leg.get('calculated_delta', 0.5)),
                    'entry_spot': spot,
                    'instrument_key': new_leg['instrument_key'],
                    'entry_price': entry_price,
                    'type': new_leg.get('type', 'p'),
                    'expiry_dt': new_leg.get('expiry_dt')
                }
                self.journal.log_trade(new_leg['instrument_key'], 'SELL', config.ORDER_QUANTITY, entry_price, 'WEEKLY_ROLL_ENTRY', expiry=new_leg.get('expiry_dt'))
                self.log(f"ADJUSTMENT ENTRY: SOLD Weekly ATM Put | Strike: {new_leg['strike']} | Price: {entry_price} | Delta: {new_leg.get('delta', 0):.2f}")
            else:
                reason = resp_entry.get('message', 'Unknown Error')
                self.log(f"CRITICAL ERROR: Weekly Roll Entry FAILED - {reason}. Currently NAKED.")

    def adjust_monthly_leg(self, spot, chain, target_delta, order_callback, force_atm=False):
        # 1. Select New Leg (Force Round 100s + Optional ATM)
        new_leg = self.select_strike_by_delta(spot, chain, target_delta, force_round=True, force_atm=force_atm)
        if not new_leg:
            self.log(f"ERROR: Could not find suitable new Monthly strike (Round 100) for adjustment. Skipping.")
            return

        # Capture old position details for exit
        old_pos = self.monthly_position
        old_key = old_pos['instrument_key'] if old_pos else None
        qty = old_pos.get('qty', config.ORDER_QUANTITY) if old_pos else config.ORDER_QUANTITY
        old_expiry = old_pos.get('expiry_dt') if old_pos else 'N/A'
        old_price = old_pos.get('entry_price', 0.0) if old_pos else 0.0

        # 2. Enter New (Buy first for margin)
        if order_callback:
            resp_entry = order_callback(new_leg['instrument_key'], config.ORDER_QUANTITY, 'BUY', 'MONTHLY_ROLL_ENTRY', expiry=new_leg.get('expiry_dt'))
            if resp_entry and (resp_entry.get('status') == 'success'):
                entry_price = resp_entry.get('avg_price', new_leg.get('ltp', 0.0))
                self.monthly_position = {
                    'leg': 'monthly_buy',
                    'strike': new_leg['strike'],
                    'expiry': new_leg['time_to_expiry'],
                    'iv': new_leg['iv'],
                    'delta': new_leg.get('delta', new_leg.get('calculated_delta', 0.5)),
                    'entry_spot': spot,
                    'instrument_key': new_leg['instrument_key'],
                    'entry_price': entry_price,
                    'type': new_leg.get('type', 'p'),
                    'expiry_dt': new_leg.get('expiry_dt')
                }
                self.journal.log_trade(new_leg['instrument_key'], 'BUY', config.ORDER_QUANTITY, entry_price, 'MONTHLY_ROLL_ENTRY', expiry=new_leg.get('expiry_dt'))
                self.log(f"ADJUSTMENT ENTRY: BOUGHT Monthly Put | Strike: {new_leg['strike']} | Price: {entry_price} | Delta: {new_leg.get('delta', 0):.2f}")
            else:
                reason = resp_entry.get('message', 'Unknown Error')
                self.log(f"CRITICAL ERROR: Monthly Roll Entry FAILED - {reason}. Aborting adjustment.")
                return

        # 3. Exit Existing
        if order_callback and old_key:
            resp_exit = order_callback(old_key, qty, 'SELL', 'MONTHLY_EXIT_ADJ', expiry=old_expiry)
            if (resp_exit and resp_exit.get('status') == 'success'):
                # PNL = (Exit - Entry) for Buy side
                exit_price = resp_exit.get('avg_price', 0.0)
                pnl = (exit_price - old_price) * qty
                self.journal.log_trade(old_key, 'SELL', qty, exit_price, 'MONTHLY_EXIT_ADJ', expiry=old_expiry, pnl=pnl)
            else:
                reason = resp_exit.get('message', 'Unknown Error')
                self.log(f"CRITICAL ERROR: Monthly Exit Order FAILED - {reason}. You now have TWO monthly hedges.")
        
        # Whether exit succeeded or failed, if we entered a new one, we need to update our pointer or risk double management.
        # But here we assume we swap them. 
        # Actually logic above replaces self.monthly_position with new one at Step 2. 
        # But we need to make sure we don't keep the old one in a separate variable if we had one.
        self.save_state() # Atomic Save

    def check_portfolio_risk(self, weekly_ltp, monthly_ltp, order_callback):
        """
        Calculates Net PNL and checks against Max Loss threshold.
        """
        # Skip if disabled
        if config.MAX_LOSS_VALUE <= 0:
            return False

        if not self.weekly_position or not self.monthly_position:
            return False

        # If LTP is missing, we can't accurately check risk, so we skip
        if weekly_ltp is None or monthly_ltp is None:
            return False

        # PNL = (Entry - Current) for Sell side
        weekly_pnl = (self.weekly_position['entry_price'] - weekly_ltp) * config.ORDER_QUANTITY
        # PNL = (Current - Entry) for Buy side
        monthly_pnl = (monthly_ltp - self.monthly_position['entry_price']) * config.ORDER_QUANTITY
        
        total_pnl = weekly_pnl + monthly_pnl

        if total_pnl <= -abs(config.MAX_LOSS_VALUE):
            self.log(f"{Fore.RED}CRITICAL: Max Loss Hit ({total_pnl:.2f}).{Style.RESET_ALL}")
            self.exit_all_positions(order_callback, reason="MAX_LOSS")
            return True
        return False

    def exit_all_positions(self, order_callback, reason="MANUAL"):
        """
        Forcefully squares off all open legs.
        """
        self.log(f"{Fore.MAGENTA}INITIATING TOTAL STRATEGY EXIT: {reason}{Style.RESET_ALL}")
        
        if self.weekly_position and order_callback:
            qty = self.weekly_position.get('qty', config.ORDER_QUANTITY)
            resp = order_callback(self.weekly_position['instrument_key'], qty, 'BUY', f'EXIT_{reason}', expiry=self.weekly_position.get('expiry_dt'))
            if resp and resp.get('status') == 'success':
                exit_price = resp.get('avg_price', 0.0)
                pnl = (self.weekly_position['entry_price'] - exit_price) * qty
                self.journal.log_trade(self.weekly_position['instrument_key'], 'BUY', qty, exit_price, f'EXIT_{reason}', expiry=self.weekly_position.get('expiry_dt'), pnl=pnl)
                self.log(f"Exited Weekly: {self.weekly_position['strike']} Put | Price: {exit_price} | PnL: {pnl:.2f}")
            
        if self.monthly_position and order_callback:
            qty = self.monthly_position.get('qty', config.ORDER_QUANTITY)
            resp = order_callback(self.monthly_position['instrument_key'], qty, 'SELL', f'EXIT_{reason}', expiry=self.monthly_position.get('expiry_dt'))
            if resp and resp.get('status') == 'success':
                exit_price = resp.get('avg_price', 0.0)
                pnl = (exit_price - self.monthly_position['entry_price']) * qty
                self.journal.log_trade(self.monthly_position['instrument_key'], 'SELL', qty, exit_price, f'EXIT_{reason}', expiry=self.monthly_position.get('expiry_dt'), pnl=pnl)
                self.log(f"Exited Monthly: {self.monthly_position['strike']} Put | Price: {exit_price} | PnL: {pnl:.2f}")
            
        self.weekly_position = None
        self.monthly_position = None

    def get_open_pnl(self, weekly_ltp, monthly_ltp):
        """Calculates current unrealized P&L."""
        w_pnl = 0.0
        m_pnl = 0.0
        if self.weekly_position and weekly_ltp is not None:
            qty = self.weekly_position.get('qty', config.ORDER_QUANTITY)
            w_pnl = (self.weekly_position['entry_price'] - weekly_ltp) * qty
        if self.monthly_position and monthly_ltp is not None:
            qty = self.monthly_position.get('qty', config.ORDER_QUANTITY)
            m_pnl = (monthly_ltp - self.monthly_position['entry_price']) * qty
        return w_pnl + m_pnl

    def check_gap_risk(self, spot, w_ltp, m_ltp, order_callback):
        """
        Logic for mitigating risks from opening gaps.
        Called during the first few minutes of the market session.
        """
        if not self.weekly_position or not self.monthly_position:
            return False

        # 1. Extreme PnL Check
        total_pnl = self.get_open_pnl(w_ltp, m_ltp)
        max_loss = config.MAX_LOSS_VALUE
        
        if total_pnl <= -abs(max_loss * config.GAP_EMERGENCY_EXIT_PNL_PCT):
            self.log(f"{Fore.RED}GAP PROTECT: Critical PnL ({total_pnl:.2f}) detected during opening window. EMERGENCY EXIT.{Style.RESET_ALL}")
            self.exit_all_positions(order_callback, reason="GAP_EMERGENCY_EXIT")
            return True

        # 2. Forced Roll Check (Extreme Spot Gap)
        # Use entry_spot of the weekly leg as reference
        ref_spot = self.weekly_position.get('entry_spot', spot)
        gap_pct = abs(spot - ref_spot) / ref_spot * 100
        
        if gap_pct >= config.GAP_FORCED_ROLL_THRESHOLD_PCT:
            self.log(f"{Fore.YELLOW}GAP PROTECT: Major Spot Gap ({gap_pct:.2f}%) detected. Forcing roll to ATM.{Style.RESET_ALL}")
            # Triggering via standard adjustment flow but bypassing delta checks
            # We return False here to let update() proceed to check_adjustments which now has is_opening_window bypass
            # However, we can also just call the roll directly for more 'force'
            return False # Let check_adjustments handle it with its bypass

        return False

    def save_state(self):
        """Saves current state to persistent storage."""
        state = {
            'weekly': self.weekly_position,
            'monthly': self.monthly_position,
            'last_rollover_date': self.last_rollover_date
        }
        super().save_current_state(state)

    def load_previous_state(self):
        """Loads state from persistent storage."""
        state = super().load_previous_state()
        
        if state:
            self.weekly_position = state.get('weekly')
            self.monthly_position = state.get('monthly')
            self.last_rollover_date = state.get('last_rollover_date')  # Load rollover tracking
            
            if self.weekly_position or self.monthly_position:
                log_msg = f"{Fore.CYAN}RECOVERY: Loaded existing positions:{Style.RESET_ALL}"
                if self.weekly_position:
                    exp = self.weekly_position.get('expiry_dt','N/A')
                    log_msg += f"\n  - Weekly: {self.weekly_position['strike']} Put (Expiry: {exp})"
                if self.monthly_position:
                    exp = self.monthly_position.get('expiry_dt','N/A')
                    log_msg += f"\n  - Monthly: {self.monthly_position['strike']} Put (Expiry: {exp})"
                self.log(log_msg)
                return True
        return False

    def _parse_position(self, p):
        """
        Robustly extract attributes from Upstox PositionData object.
        Handles missing keys and schema variations.
        """
        # Quantity
        qty = getattr(p, 'net_quantity', getattr(p, 'quantity', 0))
        
        # Prices
        # Logic: 
        # 1. If 'average_price' (Holdings) is available (non-zero), use it.
        # 2. Else, calculate Break-Even Price from 'buy_value' and 'sell_value' / net_qty.
        #    This accounts for intraday scalps affecting the cost basis of the remaining position.
        
        avg_price = getattr(p, 'average_price', 0.0)
        try: avg_price = float(avg_price)
        except: avg_price = 0.0

        buy_val = getattr(p, 'buy_value', 0.0)
        sell_val = getattr(p, 'sell_value', 0.0)
        try: buy_val = float(buy_val)
        except: buy_val = 0.0
        try: sell_val = float(sell_val)
        except: sell_val = 0.0

        calculated_price = 0.0
        if qty != 0:
            calculated_price = abs(buy_val - sell_val) / abs(qty)

        final_price = 0.0
        if avg_price > 0:
            final_price = avg_price
        elif calculated_price > 0:
            final_price = calculated_price
        else:
            # Fallbacks
            if qty > 0:
                final_price = getattr(p, 'day_buy_price', 0.0)
                if final_price == 0.0: final_price = getattr(p, 'buy_price', 0.0)
            else:
                final_price = getattr(p, 'day_sell_price', 0.0)
                if final_price == 0.0: final_price = getattr(p, 'sell_price', 0.0)
        
        # Assign to buy/sell price based on direction
        buy_price = 0.0
        sell_price = 0.0
        
        if qty > 0:
            buy_price = final_price
        elif qty < 0:
            sell_price = final_price

        # Token & Symbol
        token = str(getattr(p, 'instrument_token', '')).upper()
        symbol = str(getattr(p, 'trading_symbol', getattr(p, 'tradingsymbol', ''))).upper()
        
        # Strike & Expiry (Fallback if missing)
        strike = getattr(p, 'strike_price', 0.0)
        expiry = str(getattr(p, 'expiry', 'N/A'))
        
        # Safe float conversion
        try: strike = float(strike)
        except: strike = 0.0

        # FALLBACK: If strike is 0.0, attempt to parse from trading_symbol
        # Expected formats: 'NIFTY26JAN26150PE', 'NIFTY 23 JAN 26150 PE'
        if strike == 0.0 and symbol:
            import re
            # Regex to find the strike price (5 digits) followed by PE/CE
            # Example: NIFTY26010626150PE or NIFTY26JAN26150PE
            # Looking for 5 digits before PE/CE
            match = re.search(r'(\d{5})(?:PE|CE)', symbol)
            if match:
                try:
                    strike = float(match.group(1))
                except:
                    pass

        return {
            'qty': qty,
            'buy_price': buy_price,
            'sell_price': sell_price,
            'token': token,
            'symbol': symbol,
            'strike': strike,
            'expiry': expiry,
            'obj': p # Keep original object for reference
        }

    def pull_from_broker(self, broker_positions, master_df=None, silent=False):
        """
        Robustly identify weekly and monthly legs from broker portfolio.
        Expects: 1 Short Nifty Put (Weekly) and 1 Long Nifty Put (Monthly).
        If existing legs are NOT found in broker_positions, they are cleared (Reconciliation).
        """
        if not silent:
            self.log(f"PULLING TRADES for {self.name}...")
            
        if broker_positions is None:
            if not silent:
                self.log(f"{Fore.RED}WARNING: Broker positions data is None (API Error). Skipping reconciliation.{Style.RESET_ALL}")
            return False
        
        # Filter Nifty Puts Broadly
        # Many Upstox tokens contain Nifty but some might be raw tokens.
        # We check trading_symbol if available or token string.
        # Preserve existing state to recover expiry dates if needed
        existing_weekly = self.weekly_position
        existing_monthly = self.monthly_position
        
        nifty_puts = []
        for p in broker_positions:
            data = self._parse_position(p)
            
            if data['qty'] == 0: continue
            
            # Match NIFTY or indexing markers
            if 'NIFTY' in data['token'] or 'NIFTY' in data['symbol']:
                # Attach parsed data to object for easier sorting later
                p._parsed = data 
                
                # Try to resolve N/A expiry from Master DF
                if p._parsed['expiry'] == 'N/A' and master_df is not None:
                    try:
                        key = p._parsed['token'] # token is the instrument_key here
                        # Check if key exists in master_df['instrument_key']
                        match = master_df[master_df['instrument_key'] == key]
                        if not match.empty:
                            p._parsed['expiry'] = str(match.iloc[0]['expiry_dt'])
                    except Exception as e:
                        print(f"Error resolving expiry from master: {e}")

                nifty_puts.append(p)
        
        if not nifty_puts:
            return False
            
        # Group by side
        sell_legs = [p for p in nifty_puts if p._parsed['qty'] < 0]
        buy_legs = [p for p in nifty_puts if p._parsed['qty'] > 0]
        
        if not sell_legs and not buy_legs:
            return False

        # Identify Weekly (Earliest Expiry Sell Leg)
        if sell_legs:
            # Sort by expiry if available, else trading_symbol. Use parsed data.
            sell_legs.sort(key=lambda x: str(x._parsed['expiry']) if x._parsed['expiry'] != 'N/A' else x._parsed['symbol'])
            w = sell_legs[0]
            d = w._parsed
            
            # Detect Change
            is_new = False
            if not self.weekly_position or self.weekly_position['instrument_key'] != d['token']:
                is_new = True
            
            self.weekly_position = {
                'instrument_key': d['token'],
                'qty': abs(d['qty']),
                'side': 'SELL',
                'strike': d['strike'],
                'entry_price': d['sell_price'],
                'expiry_dt': d['expiry'],
                'type': 'p'
            }
            # RESTORE EXPIRY if Missing
            if (self.weekly_position['expiry_dt'] == 'N/A' and 
                existing_weekly and 
                existing_weekly.get('instrument_key') == d['token'] and
                existing_weekly.get('expiry_dt') != 'N/A'):
                
                self.weekly_position['expiry_dt'] = existing_weekly['expiry_dt']
                
            if is_new or not silent:
                self.log(f"Synced Weekly: {d['token']} @ {self.weekly_position['strike']}")
        else:
            # CRITICAL: Weekly leg was thought to exist, but is NOT in broker positions.
            if self.weekly_position:
                self.log(f"{Fore.RED}RECONCILIATION: Weekly position {self.weekly_position['instrument_key']} not found in broker. Clearing state.{Style.RESET_ALL}")
                self.weekly_position = None

        # Identify Monthly (Latest Expiry Buy Leg)
        if buy_legs:
            # Sort by expiry descending (Monthly should be further out)
            # STABILITY FIX: If we generally have multiple monthly legs, prefer the one we already track.
            def monthly_sort_key(x):
                # Primary: Is this our existing leg? (Push to front)
                is_existing = 1 if (self.monthly_position and x._parsed['token'] == self.monthly_position['instrument_key']) else 0
                # Secondary: Expiry (Later is better)
                expiry_val = str(x._parsed['expiry']) if x._parsed['expiry'] != 'N/A' else x._parsed['symbol']
                return (is_existing, expiry_val)

            buy_legs.sort(key=monthly_sort_key, reverse=True)
            m = buy_legs[0]
            d = m._parsed
            
            # Detect Change
            is_new = False
            if not self.monthly_position or self.monthly_position['instrument_key'] != d['token']:
                is_new = True

            self.monthly_position = {
                'instrument_key': d['token'],
                'qty': abs(d['qty']),
                'side': 'BUY',
                'strike': d['strike'],
                'entry_price': d['buy_price'],
                'expiry_dt': d['expiry'],
                'type': 'p'
            }
            # RESTORE EXPIRY if Missing
            if (self.monthly_position['expiry_dt'] == 'N/A' and 
                existing_monthly and 
                existing_monthly.get('instrument_key') == d['token'] and
                existing_monthly.get('expiry_dt') != 'N/A'):
                
                self.monthly_position['expiry_dt'] = existing_monthly['expiry_dt']

            if is_new or not silent:
                self.log(f"Synced Monthly: {d['token']} @ {self.monthly_position['strike']}")
        else:
            # CRITICAL: Monthly leg was thought to exist, but is NOT in broker positions.
            if self.monthly_position:
                self.log(f"{Fore.RED}RECONCILIATION: Monthly position {self.monthly_position['instrument_key']} not found in broker. Clearing state.{Style.RESET_ALL}")
                self.monthly_position = None
            
        # Log to CSV for tracking if requested
        # Only log if it's a fresh sync event, not every loop
        if not silent and self.weekly_position:
            self.journal.log_trade(
                self.weekly_position['instrument_key'], 
                'SELL', 
                self.weekly_position['qty'], 
                self.weekly_position['entry_price'], 
                'SYNC_EXISTING', 
                expiry=self.weekly_position['expiry_dt'],
                check_duplicate=True
            )

        if not silent and self.monthly_position:
            self.journal.log_trade(
                self.monthly_position['instrument_key'], 
                'BUY', 
                self.monthly_position['qty'], 
                self.monthly_position['entry_price'], 
                'SYNC_EXISTING', 
                expiry=self.monthly_position['expiry_dt'],
                check_duplicate=True
            )
            
        return True
