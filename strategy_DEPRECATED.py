import config
from datetime import datetime, timedelta
from greeks import calculate_delta, get_atm_strike
from colorama import init, Fore, Style
from trade_logger import TradeJournal
from base_strategy import BaseStrategy
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
        self.last_failed_entry_time = 0 # Unix timestamp to prevent rapid re-entry

    def log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
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
        self.logs.append(entry)

    def update(self, market_data, order_callback):
        """
        Main logic loop for Calendar PE Weekly.
        market_data: {spot_price, weekly_chain, monthly_chain, quotes, now, master_flags...}
        """
        spot_price = market_data.get('spot_price')
        # Mapping generic runner keys to strategy-specific usage
        weekly_chain = market_data.get('cw_chain', [])  # Current Weekly
        next_weekly_chain = market_data.get('nw_chain', []) # Next Weekly
        monthly_chain = market_data.get('m_chain', [])  # Monthly Target
        quotes = market_data.get('quotes', {})
        now = market_data.get('now', datetime.now())

        if not spot_price:
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
            if is_day_before and now.strftime("%H:%M") >= "15:00":
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
                # If expiry is tomorrow and time is >= EARLY_ROLLOVER_TIME
                if expiry_dt == (today + timedelta(days=1)):
                    if now.strftime("%H:%M") >= config.EARLY_ROLLOVER_TIME:
                        self.log(f"{Fore.YELLOW}T-1 ROLLOVER: Expiry tomorrow. Scaling out and rolling to next cycle.{Style.RESET_ALL}")
                        
                        # 1. Roll Weekly to Next Week
                        self.adjust_weekly_leg(spot_price, next_weekly_chain, order_callback)
                        
                        # 2. Check if Monthly also needs to roll (if new weekly is in a different month)
                        if self.weekly_position and self.monthly_position:
                            new_w_exp = datetime.strptime(self.weekly_position['expiry_dt'], '%Y-%m-%d').date()
                            curr_m_exp = datetime.strptime(self.monthly_position['expiry_dt'], '%Y-%m-%d').date()
                            
                            if new_w_exp.month != curr_m_exp.month or new_w_exp > curr_m_exp:
                                self.log(f"{Fore.CYAN}T-1 LINKED ROLL: New weekly cycle {new_w_exp.month} requires Monthly roll to next month.{Style.RESET_ALL}")
                                self.adjust_monthly_leg(spot_price, monthly_chain, 0.5, order_callback)
                        
                        has_acted = True
                        self.save_state()
                        return

        # 1. Update Deltas for existing positions
        self.update_deltas(spot_price, 
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
                self.enter_strategy(spot_price, weekly_chain, monthly_chain, order_callback=order_callback, market_data=market_data)
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
            self.adjust_weekly_leg(spot_price, weekly_chain, order_callback)
            has_acted = True
            self.save_state()

        # 3. Check Portfolio Risk
        w_ltp = None
        m_ltp = None
        if self.weekly_position:
            obj = quotes.get(self.weekly_position['instrument_key'])
            w_ltp = getattr(obj, 'last_price', None) if obj else None
        if self.monthly_position:
            obj = quotes.get(self.monthly_position['instrument_key'])
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
            if self.check_gap_risk(spot_price, w_ltp, m_ltp, order_callback):
                has_acted = True
                self.save_state()
                return

        adjustment_made = False
        
        # Prevent double adjustment if we just entered or rolled over in this cycle
        if not has_acted and (can_adjust or is_opening_window) and self.weekly_position and self.monthly_position:
            adjustment_made = self.check_adjustments(spot_price, weekly_chain, monthly_chain, order_callback=order_callback, is_opening_window=is_opening_window)
            
        if adjustment_made:
            has_acted = True
            self.save_state()

        # 5. PnL Summary Logging
        open_pnl = self.get_open_pnl(w_ltp, m_ltp)
        strategy_state = {
            'weekly': self.weekly_position,
            'monthly': self.monthly_position,
            'weekly_ltp': w_ltp,
            'monthly_ltp': m_ltp
        }
        self.journal.print_summary(open_pnl, strategy_state)

    def select_strike_by_delta(self, spot, chain, target_delta, option_type='p', tolerance=0.1):
        """
        Finds a strike in the option chain closest to the target delta.
        chain: list of dicts {'strike': K, 'iv': sigma, 'expiry': t_years}
        """
        best_strike = None
        min_diff = float('inf')
        
        for opt in chain:
            # ONLY consider options of the requested type (p or c)
            if opt.get('type') != option_type:
                continue
                
            # Use broker delta if available in opt, otherwise calculate
            abs_d = opt.get('delta')
            if abs_d is None:
                d = calculate_delta(option_type, spot, opt['strike'], opt['time_to_expiry'], self.risk_free_rate, opt['iv'])
                abs_d = abs(d)
            else:
                abs_d = abs(abs_d)
            
            diff = abs(abs_d - target_delta)
            
            if diff < min_diff:
                min_diff = diff
                best_strike = opt.copy() # Avoid modifying shared chain data
                best_strike['calculated_delta'] = abs_d
        
        # Check if within reasonable tolerance if needed, or just take best
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
        weekly_leg = self.select_strike_by_delta(spot, weekly_chain, config.ENTRY_WEEKLY_DELTA_TARGET)
        monthly_leg = self.select_strike_by_delta(spot, monthly_chain, config.ENTRY_MONTHLY_DELTA_TARGET)

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
                        'delta': monthly_leg['calculated_delta'],
                        'entry_spot': spot,
                        'instrument_key': monthly_leg['instrument_key'],
                        'entry_price': entry_price_m,
                        'type': monthly_leg.get('type', 'p'),
                        'expiry_dt': monthly_leg.get('expiry_dt')
                    }
                    self.journal.log_trade(monthly_leg['instrument_key'], 'BUY', config.ORDER_QUANTITY, entry_price_m, 'MONTHLY_ENTRY', expiry=monthly_leg.get('expiry_dt'))
                    self.log(f"ENTRY: BOUGHT Monthly Put | Strike: {monthly_leg['strike']} | Price: {entry_price_m} | Expiry: {monthly_leg['expiry_dt']} | Delta: {monthly_leg['calculated_delta']:.2f}")
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
                        'delta': weekly_leg['calculated_delta'],
                        'entry_spot': spot,
                        'instrument_key': weekly_leg['instrument_key'],
                        'entry_price': entry_price,
                        'type': weekly_leg.get('type', 'p'),
                        'expiry_dt': weekly_leg.get('expiry_dt')
                    }
                    self.journal.log_trade(weekly_leg['instrument_key'], 'SELL', config.ORDER_QUANTITY, entry_price, 'WEEKLY_ENTRY', expiry=weekly_leg.get('expiry_dt'))
                    self.log(f"ENTRY: SOLD Weekly Put | Strike: {weekly_leg['strike']} | Price: {entry_price} | Expiry: {weekly_leg['expiry_dt']} | Delta: {weekly_leg['calculated_delta']:.2f}")
                else:
                    reason = resp_w.get('message', 'Unknown Error')
                    self.log(f"CRITICAL ERROR: Weekly Sell Order FAILED - {reason}")
                    # EMERGENCY: Monthly was bought, but Weekly failed. Not strictly fatal for margin, but strategy is invalid.
                    self.log("EMERGENCY: Squaring off Monthly leg.")
                    resp_exit = order_callback(monthly_leg['instrument_key'], config.ORDER_QUANTITY, 'SELL', 'EMERGENCY_EXIT', expiry=monthly_leg.get('expiry_dt'))
                    exit_price = resp_exit.get('avg_price', 0.0)
                    pnl = (exit_price - self.monthly_position['entry_price']) * config.ORDER_QUANTITY
                    self.journal.log_trade(monthly_leg['instrument_key'], 'SELL', config.ORDER_QUANTITY, exit_price, 'EMERGENCY_EXIT', expiry=monthly_leg.get('expiry_dt'), pnl=pnl)
                    self.monthly_position = None
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

    def check_adjustments(self, spot, weekly_chain, monthly_chain, order_callback=None, is_opening_window=False):
        """
        Logic 3: Adjustment.
        is_opening_window: If True, we might trigger rolls even if delta hasn't hit thresholds if gap is high.
        """
        if not self.weekly_position or not self.monthly_position:
            return False

        adjustment_made = False
        
        # 0. Gap Forced Roll Check
        if is_opening_window:
            ref_spot = self.weekly_position.get('entry_spot', spot)
            gap_pct = abs(spot - ref_spot) / ref_spot * 100
            if gap_pct >= config.GAP_FORCED_ROLL_THRESHOLD_PCT:
                self.log(f"GAP FORCED ROLL: Market open gap {gap_pct:.2f}% exceeds {config.GAP_FORCED_ROLL_THRESHOLD_PCT}%. Repositioning.")
                self.adjust_weekly_leg(spot, weekly_chain, order_callback)
                # For Calendar, we usually adjust weekly. If monthly is also far, it will roll on its own delta check below.
                return True

        # 1. Weekly Put (Sell Leg) Adjustments
        # On a Market Fall: If delta increases to 0.80
        if self.weekly_position['delta'] >= config.WEEKLY_ADJ_TRIGGER_DELTA:
            self.log(f"WEEKLY ADJ (FALL): Delta is {self.weekly_position['delta']:.2f} >= {config.WEEKLY_ADJ_TRIGGER_DELTA}")
            self.adjust_weekly_leg(spot, weekly_chain, order_callback)
            adjustment_made = True

        # On a Market Rise: If delta drops to 0.10 or below
        elif self.weekly_position['delta'] <= config.WEEKLY_ADJ_TRIGGER_DELTA_LOW:
            self.log(f"WEEKLY ADJ (RISE): Delta is {self.weekly_position['delta']:.2f} <= {config.WEEKLY_ADJ_TRIGGER_DELTA_LOW}")
            self.adjust_weekly_leg(spot, weekly_chain, order_callback)
            adjustment_made = True

        # 2. Next-Month Put (Buy Leg) Adjustments
        # On a Sharp Market Fall: If delta reaches 0.90
        if self.monthly_position['delta'] >= config.MONTHLY_ADJ_TRIGGER_DELTA:
            self.log(f"MONTHLY ADJ (FALL): Delta is {self.monthly_position['delta']:.2f} >= {config.MONTHLY_ADJ_TRIGGER_DELTA}")
            self.adjust_monthly_leg(spot, monthly_chain, config.MONTHLY_ROLL_TARGET_DELTA_FALL, order_callback)
            adjustment_made = True

        # On a Sharp Market Rise: If delta drops to 0.10
        elif self.monthly_position['delta'] <= config.MONTHLY_ADJ_TRIGGER_DELTA_LOW:
            self.log(f"MONTHLY ADJ (RISE): Delta is {self.monthly_position['delta']:.2f} <= {config.MONTHLY_ADJ_TRIGGER_DELTA_LOW}")
            self.adjust_monthly_leg(spot, monthly_chain, config.MONTHLY_ROLL_TARGET_DELTA_RISE, order_callback)
            adjustment_made = True
        
        return adjustment_made

    def adjust_weekly_leg(self, spot, chain, order_callback):
        # 1. Select New Leg first to ensure we have a target
        new_leg = self.select_strike_by_delta(spot, chain, config.WEEKLY_ROLL_TARGET_DELTA)
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
                    'delta': new_leg['calculated_delta'],
                    'entry_spot': spot,
                    'instrument_key': new_leg['instrument_key'],
                    'entry_price': entry_price,
                    'type': new_leg.get('type', 'p'),
                    'expiry_dt': new_leg.get('expiry_dt')
                }
                self.journal.log_trade(new_leg['instrument_key'], 'SELL', config.ORDER_QUANTITY, entry_price, 'WEEKLY_ROLL_ENTRY', expiry=new_leg.get('expiry_dt'))
                self.log(f"ADJUSTMENT ENTRY: SOLD Weekly ATM Put | Strike: {new_leg['strike']} | Price: {entry_price} | Delta: {new_leg['calculated_delta']:.2f}")
            else:
                reason = resp_entry.get('message', 'Unknown Error')
                self.log(f"CRITICAL ERROR: Weekly Roll Entry FAILED - {reason}. Currently NAKED.")

    def adjust_monthly_leg(self, spot, chain, target_delta, order_callback):
        # 1. Select New Leg
        new_leg = self.select_strike_by_delta(spot, chain, target_delta)
        if not new_leg:
            self.log("ERROR: Could not find suitable new Monthly strike for adjustment. Skipping.")
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
                    'delta': new_leg['calculated_delta'],
                    'entry_spot': spot,
                    'instrument_key': new_leg['instrument_key'],
                    'entry_price': entry_price,
                    'type': new_leg.get('type', 'p'),
                    'expiry_dt': new_leg.get('expiry_dt')
                }
                self.journal.log_trade(new_leg['instrument_key'], 'BUY', config.ORDER_QUANTITY, entry_price, 'MONTHLY_ROLL_ENTRY', expiry=new_leg.get('expiry_dt'))
                self.log(f"ADJUSTMENT ENTRY: BOUGHT Monthly Put | Strike: {new_leg['strike']} | Price: {entry_price} | Delta: {new_leg['calculated_delta']:.2f}")
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

    def pull_from_broker(self, broker_positions, master_df=None):
        """
        Robustly identify weekly and monthly legs from broker portfolio.
        Expects: 1 Short Nifty Put (Weekly) and 1 Long Nifty Put (Monthly).
        """
        self.log(f"PULLING TRADES for {self.name}...")
        
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
                
            self.log(f"Synced Weekly: {d['token']} @ {self.weekly_position['strike']}")

        # Identify Monthly (Latest Expiry Buy Leg)
        # Identify Monthly (Latest Expiry Buy Leg)
        if buy_legs:
            # Sort by expiry descending (Monthly should be further out)
            buy_legs.sort(key=lambda x: str(x._parsed['expiry']) if x._parsed['expiry'] != 'N/A' else x._parsed['symbol'], reverse=True)
            m = buy_legs[0]
            d = m._parsed
            
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

            self.log(f"Synced Monthly: {d['token']} @ {self.monthly_position['strike']}")

            
        # Log to CSV for tracking if requested
        if self.weekly_position:
            # Check if recently logged to avoid duplicates? Ideally yes, but here we just log as SYNC event.
            # Only log if we just synced them.
            self.journal.log_trade(
                self.weekly_position['instrument_key'], 
                'SELL', 
                self.weekly_position['qty'], 
                self.weekly_position['entry_price'], 
                'SYNC_EXISTING', 
                expiry=self.weekly_position['expiry_dt'],
                check_duplicate=True
            )

        if self.monthly_position:
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

class WeeklyIronfly(BaseStrategy):
    def __init__(self, risk_free_rate=config.RISK_FREE_RATE):
        super().__init__("WeeklyIronfly")
        self.positions = [] # List of {'instrument_key': ..., 'qty': ..., 'side': ..., 'entry_price': ...}
        self.is_adjusted = False
        self.risk_free_rate = risk_free_rate
        self.journal = TradeJournal(filename="trade_log_ironfly.csv")

    def log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        colored_message = message.replace("SOLD", f"{Fore.RED}SOLD{Style.RESET_ALL}").replace("BOUGHT", f"{Fore.GREEN}BOUGHT{Style.RESET_ALL}")
        entry = f"[{timestamp}] [{self.name}] {colored_message}"
        print(entry)

    def update(self, market_data, order_callback):
        spot_price = market_data.get('spot_price')
        now = market_data.get('now', datetime.now())
        quotes = market_data.get('quotes', {})
        
        # Mapping generic runner keys
        cw_chain = market_data.get('cw_chain', []) # Current Weekly (this week)
        nw_chain = market_data.get('nw_chain', []) # Next Weekly (positioned week)
        m_chain = market_data.get('m_chain', [])   # Monthly

        if not spot_price: return

        # Recovery: Ensure all positions have expiry_dt
        if self.positions:
            for pos in self.positions:
                if not pos.get('expiry_dt') or pos.get('expiry_dt') == 'N/A':
                    # Try to find in any of the chains
                    for chain in [cw_chain, nw_chain, m_chain]:
                        match = next((x for x in chain if x['instrument_key'] == pos['instrument_key']), None)
                        if match:
                            pos['expiry_dt'] = match['expiry_dt']
                            break

        # Auto-Cleanup for Expired Positions
        if self.positions:
            today_str = now.strftime("%Y-%m-%d")
            valid_positions = []
            for pos in self.positions:
                if pos.get('expiry_dt') and pos['expiry_dt'] < today_str:
                    self.log(f"{Fore.RED}AUTO-CLEANUP: Ironfly leg {pos.get('strike')} {pos.get('type')} expired on {pos['expiry_dt']}. Clearing.{Style.RESET_ALL}")
                else:
                    valid_positions.append(pos)
            
            if len(valid_positions) < len(self.positions):
                self.positions = valid_positions
                self.save_state()

        has_acted = False

        # --- AUTO-EXIT BEFORE MONTHLY EXPIRY (SAFETY) ---
        if config.AUTO_EXIT_BEFORE_MONTHLY_EXPIRY_3PM:
            is_day_before = market_data.get('is_day_before_monthly_expiry', False)
            if is_day_before and now.strftime("%H:%M") >= "15:00":
                self.log(f"{Fore.MAGENTA}AUTO-EXIT: Day before Monthly Expiry (3 PM Trigger). Exiting all positions.{Style.RESET_ALL}")
                self.exit_all_positions(order_callback, reason="PRE_EXPIRY_EXIT")
                has_acted = True
                self.save_state()
                return

        # 1. Check Exit Timing (Only if we have positions and today is THAT position's expiry)
        if self.positions:
            is_pos_expiry_today = False
            # Check if Leg 2 (Main short) is expiring today
            leg2 = next((p for p in self.positions if 'IF_LEG2' in p.get('tag', '')), None)
            if leg2 and leg2.get('expiry_dt') and leg2['expiry_dt'] != 'N/A':
                try:
                    expiry_dt = datetime.strptime(leg2['expiry_dt'], '%Y-%m-%d').date()
                    if expiry_dt == now.date():
                        is_pos_expiry_today = True
                except:
                    is_pos_expiry_today = market_data.get('is_expiry_today', False)

            if (is_pos_expiry_today and now.strftime("%H:%M") >= config.IRONFLY_EXIT_TIME) or getattr(config, 'OVERRIDE_TIMING_CHECKS', False):
                self.log("EXPIRY EXIT: Timing trigger reached (or overridden). Squaring off all.")
                self.exit_all_positions(order_callback, reason="EXPIRY_TIME_EXIT")
                has_acted = True
                self.save_state()
                return

        # 2. Check Entry Timing (Current week's expiry at 12:00 PM for NEXT week's expiry)
        # Relaxed trigger: Enter if strategy is not fully positions (less than 3 legs)
        if len(self.positions) < 3:
            is_paper = config.TRADING_MODE == 'PAPER'
            expiry_skipped = market_data.get('expiry_skipped', False)
            
            # In LIVE mode, check if today is current week's expiry (to enter for next week)
            # In PAPER mode, we also follow this to ensure realistic simulation.
            is_entry_day = False
            current_weekly_expiry_str = 'N/A'
            next_weekly_expiry_str = 'N/A'
            
            if cw_chain:
                current_weekly_expiry_str = cw_chain[0].get('expiry_dt', 'N/A')
                try:
                    current_weekly_expiry = datetime.strptime(current_weekly_expiry_str, '%Y-%m-%d').date()
                    is_entry_day = (now.date() == current_weekly_expiry)
                except:
                    is_entry_day = (now.weekday() == config.IRONFLY_ENTRY_WEEKDAY)
            else:
                is_entry_day = (now.weekday() == config.IRONFLY_ENTRY_WEEKDAY)
            
            if nw_chain:
                next_weekly_expiry_str = nw_chain[0].get('expiry_dt', 'N/A')
            
            is_entry_time = now.strftime("%H:%M") >= config.IRONFLY_ENTRY_TIME
            
            # Entry condition: Correct day/time OR expiry skipped OR override True
            entry_trigger = (is_entry_day and is_entry_time) or expiry_skipped or getattr(config, 'OVERRIDE_TIMING_CHECKS', False)

            if not has_acted and entry_trigger:
                target_chain = cw_chain if expiry_skipped else nw_chain
                self.log(f"Ironfly Entry Triggered | Expiry Skipped: {expiry_skipped} | Targeting: {target_chain[0]['expiry_dt'] if target_chain else 'N/A'}")
                self.enter_strategy(spot_price, target_chain, order_callback, market_data=market_data)
                has_acted = True
                self.save_state()
            else:
                if now.second < 10 and now.minute % 5 == 0: # Log every 5 mins in the first 10s
                    self.log(f"{Fore.YELLOW}WAITING: Entry allowed on current weekly expiry ({current_weekly_expiry_str}) at {config.IRONFLY_ENTRY_TIME} for next week ({next_weekly_expiry_str}). Today is {now.strftime('%H:%M')}.{Style.RESET_ALL}")

        # 3. Monitor PNL and Adjustments
        if not has_acted and self.positions:
            total_pnl = self.calculate_total_pnl(quotes)
            pnl_pct = total_pnl / config.IRONFLY_CAPITAL
            
            # PnL Summary Logging
            strategy_state = {
                'positions': []
            }
            for pos in self.positions:
                q = quotes.get(pos['instrument_key'])
                ltp = getattr(q, 'last_price', None) if q else None
                strategy_state['positions'].append({**pos, 'ltp': ltp})
            
            self.journal.print_summary(total_pnl, strategy_state)

            # Target Hit
            if pnl_pct >= config.IRONFLY_TARGET_PERCENT:
                self.log(f"TARGET HIT: {pnl_pct*100:.2f}% profit. Exiting.")
                self.exit_all_positions(order_callback, reason="TARGET_HIT")
                has_acted = True
                self.save_state()
            
            # SL / Adjustment Trigger
            elif pnl_pct <= -config.IRONFLY_SL_PERCENT:
                if not self.is_adjusted:
                    if market_data.get('can_adjust', True):
                        self.log(f"ADJUSTMENT TRIGGER: {pnl_pct*100:.2f}% loss. Building Call Calendar.")
                        self.apply_adjustment(spot_price, nw_chain, cw_chain, order_callback)
                        has_acted = True
                        self.save_state()
                    else:
                        self.log(f"ADJUSTMENT PENDING: {pnl_pct*100:.2f}% loss. Waiting for next candle.")
                else:
                    self.log(f"STOP LOSS HIT: {pnl_pct*100:.2f}% loss (post-adjustment). Exiting.")
                    self.exit_all_positions(order_callback, reason="POST_ADJ_SL_HIT")
                    has_acted = True
                    self.save_state()

    def enter_strategy(self, spot, weekly_chain, order_callback, market_data=None):
        """
        Atomic entry for Put Butterfly:
        - Buy 1 Put at ATM-50
        - Sell 2 Puts at ATM-250  
        - Buy 1 Put at ATM-450
        """
        atm = round(spot / 50) * 50
        strikes = [
            atm + config.IRONFLY_LEG1_OFFSET,  # ATM-50
            atm + config.IRONFLY_LEG2_OFFSET,  # ATM-250
            atm + config.IRONFLY_LEG3_OFFSET   # ATM-450
        ]
        sides = ['BUY', 'SELL', 'BUY']
        qtys = [config.ORDER_QUANTITY, config.ORDER_QUANTITY * 2, config.ORDER_QUANTITY]
        tags = ['IF_LEG1', 'IF_LEG2', 'IF_LEG3']

        self.log(f"Constructing Put Butterfly @ Spot {spot:.2f} | ATM: {atm}")
        self.log(f"Target Strikes: Leg1={strikes[0]} (Buy 1), Leg2={strikes[1]} (Sell 2), Leg3={strikes[2]} (Buy 1)")
        
        # ATOMIC CHECK: Verify all legs exist in chain before placing any orders
        legs_data = []
        for i, strike in enumerate(strikes):
            opt = next((x for x in weekly_chain if x['strike'] == strike and x['type'] == 'p'), None)
            if not opt:
                self.log(f"ERROR: Cannot find Put option for Leg {i+1} at strike {strike}. Aborting entry.")
                return
            legs_data.append(opt)
        
        # RESET partial state but allow it to be repopulated by reconciliation
        self.positions = []
        if market_data:
            broker_positions = market_data.get('broker_positions', [])
            if broker_positions:
                self.pull_from_broker(broker_positions)

        self.log("All legs validated. Executing missing orders (BUY wings first for margin)...")
        
        # Priority Ordering: BUY legs first, then SELL
        execution_order = [0, 2, 1] # Indices of [Leg1, Leg2, Leg3] -> [Buy, Buy, Sell]
        
        for idx in execution_order:
            opt = legs_data[idx]
            side = sides[idx]
            qty = qtys[idx]
            tag = tags[idx]
            
            # RECONCILIATION: Check if this leg already exists
            # CRITICAL FIX: Match by TAG (structure), not just strike/side.
            # If we already have 'IF_LEG2', we don't want another one, even if strike is different.
            existing = next((p for p in self.positions if p.get('tag') == tag), None)
            
            if existing:
                self.log(f"RECONCILIATION: {tag} found (Strike: {existing['strike']}). Skipping order.")
                continue

            resp = order_callback(opt['instrument_key'], qty, side, tag, expiry=opt.get('expiry_dt'))
            if resp and resp.get('status') == 'success':
                price = resp.get('avg_price', opt.get('ltp', 0))
                # Add for persistence
                self.positions.append({
                    'instrument_key': opt['instrument_key'],
                    'qty': qty,
                    'side': side,
                    'entry_price': price,
                    'strike': opt['strike'],
                    'type': 'PE',
                    'tag': tag,
                    'expiry_dt': opt.get('expiry_dt', 'N/A')
                })
                self.journal.log_trade(opt['instrument_key'], side, qty, price, tag, expiry=opt.get('expiry_dt'))
            else:
                reason = resp.get('message', 'Unknown Error')
                self.log(f"CRITICAL ERROR: Leg {i+1} order failed - {reason}. Strategy may be incomplete!")
        
        if len(self.positions) == 3:
            self.log(f"{Fore.GREEN}Put Butterfly construction COMPLETE.{Style.RESET_ALL}")
        else:
            self.log(f"{Fore.RED}WARNING: Only {len(self.positions)}/3 legs executed!{Style.RESET_ALL}")

    def apply_adjustment(self, spot, current_week_chain, next_week_chain, order_callback):
        """
        Apply Call Calendar adjustment when market moves against Put Butterfly.
        
        Strategy: Move 100 points inward from Leg 1, then:
        - Sell Call at adjustment strike (same expiry as butterfly)
        - Buy Call at adjustment strike (next week's expiry)
        
        Example: If Leg 1 = Buy PE 25900
                 Adjustment strike = 25900 + 100 = 26000
                 Sell 1 CE 26000 (this week)
                 Buy 1 CE 26000 (next week)
        """
        # Check if next week data is available
        if not next_week_chain or len(next_week_chain) == 0:
            self.log(f"{Fore.RED}ERROR: Next week option data not available for adjustment. Skipping.{Style.RESET_ALL}")
            return
        
        # Move 100 points inward (higher strike for Puts) from Leg 1
        leg1 = next((p for p in self.positions if 'IF_LEG1' in p.get('tag', '')), None)
        if not leg1: 
            # Fallback if tags missing
            leg1 = self.positions[0] if self.positions else None
        
        if not leg1: return

        adj_strike = leg1['strike'] + config.IRONFLY_ADJ_INWARD_OFFSET
        
        self.log(f"Adjustment: Leg 1 strike = {leg1['strike']}, Moving +100 inward → {adj_strike}")
        
        # Find Call options at adjustment strike
        # Sell: Same expiry as butterfly (current_week_chain)
        # Buy: Next week's expiry (next_week_chain)
        ce_this_week = next((x for x in current_week_chain if x['strike'] == adj_strike and x['type'] == 'c'), None)
        ce_next_week = next((x for x in next_week_chain if x['strike'] == adj_strike and x['type'] == 'c'), None)

        if ce_this_week and ce_next_week:
            self.log(f"Executing Call Calendar @ Strike {adj_strike}")
            self.log(f"  → Sell CE {adj_strike} (This Week)")
            self.log(f"  → Buy CE {adj_strike} (Next Week)")
            
            # Buy Next Week CE (Buy first for margin)
            adj_qty = leg1.get('qty', config.ORDER_QUANTITY)
            resp_n = order_callback(ce_next_week['instrument_key'], adj_qty, 'BUY', 'IF_ADJ_CE_LONG', expiry=ce_next_week.get('expiry_dt'))
            # Sell This Week CE
            resp_w = order_callback(ce_this_week['instrument_key'], adj_qty, 'SELL', 'IF_ADJ_CE_SHORT', expiry=ce_this_week.get('expiry_dt'))
            
            if resp_w and resp_w.get('status') == 'success':
                price_w = resp_w.get('avg_price', ce_this_week.get('ltp', 0))
                self.positions.append({
                    'instrument_key': ce_this_week['instrument_key'],
                    'qty': adj_qty,
                    'side': 'SELL',
                    'entry_price': price_w,
                    'strike': adj_strike,
                    'type': 'CE',
                    'tag': 'IF_ADJ_CE_SHORT',
                    'expiry_dt': ce_this_week.get('expiry_dt', 'N/A')
                })
                self.journal.log_trade(ce_this_week['instrument_key'], 'SELL', adj_qty, price_w, 'IF_ADJ_CE_SHORT', expiry=ce_this_week.get('expiry_dt'))
            
            if resp_n and resp_n.get('status') == 'success':
                price_n = resp_n.get('avg_price', ce_next_week.get('ltp', 0))
                self.positions.append({
                    'instrument_key': ce_next_week['instrument_key'],
                    'qty': adj_qty,
                    'side': 'BUY',
                    'entry_price': price_n,
                    'strike': adj_strike,
                    'type': 'CE',
                    'tag': 'IF_ADJ_CE_LONG',
                    'expiry_dt': ce_next_week.get('expiry_dt', 'N/A')
                })
                self.journal.log_trade(ce_next_week['instrument_key'], 'BUY', adj_qty, price_n, 'IF_ADJ_CE_LONG', expiry=ce_next_week.get('expiry_dt'))
            
            self.is_adjusted = True
            self.log("Call Calendar Adjustment deployed.")
        else:
            self.log(f"ERROR: Could not find CE instruments for adjustment at strike {adj_strike}")
            if not ce_this_week:
                self.log(f"  Missing: CE {adj_strike} (This Week)")
            if not ce_next_week:
                self.log(f"  Missing: CE {adj_strike} (Next Week)")

    def pull_from_broker(self, broker_positions, master_df=None):
        """
        Robustly identify existing butterfly legs from broker portfolio.
        Allows for partial position discovery.
        """
        self.log(f"PULLING TRADES for {self.name}...")
        
        # Filter Nifty Puts Broadly
        nifty_puts = []
        for p in broker_positions:
            data = self._parse_position(p)
            
            if data['qty'] == 0: continue
            
            # Match NIFTY or indexing markers
            if 'NIFTY' in data['token'] or 'NIFTY' in data['symbol']:
                p._parsed = data
                nifty_puts.append(p)
        
        if not nifty_puts:
            return False
            
        sell_legs = [p for p in nifty_puts if p._parsed['qty'] < 0]
        buy_legs = [p for p in nifty_puts if p._parsed['qty'] > 0]
        
        self.positions = []
        
        # 1. Identify Main Short (Latest Expiry Sell Leg)
        if sell_legs:
            # Sort by qty desc (main legs should have higher qty or equal)
            sell_legs.sort(key=lambda x: abs(x._parsed['qty']), reverse=True)
            l2 = sell_legs[0]
            d = l2._parsed
            
            self.positions.append({
                'instrument_key': d['token'],
                'qty': abs(d['qty']),
                'side': 'SELL',
                'entry_price': d['sell_price'],
                'strike': d['strike'],
                'type': 'PE',
                'tag': 'IF_LEG2',
                'expiry_dt': d['expiry']
            })
            self.log(f"Synced IF_LEG2 (Sell): {d['strike']}")

        # 2. Identify Buy Hedges
        if buy_legs:
            # Sort by strike desc
            buy_legs.sort(key=lambda x: x._parsed['strike'], reverse=True)
            
            # Leg 1 (Higher strike)
            l1 = buy_legs[0]
            d1 = l1._parsed
            
            self.positions.append({
                'instrument_key': d1['token'],
                'qty': abs(d1['qty']),
                'side': 'BUY',
                'entry_price': d1['buy_price'],
                'strike': d1['strike'],
                'type': 'PE',
                'tag': 'IF_LEG1',
                'expiry_dt': d1['expiry']
            })
            self.log(f"Synced IF_LEG1 (Buy): {d1['strike']}")
            
            # Leg 3 (Lower strike) - only if we have at least 2 buy legs
            if len(buy_legs) >= 2:
                l3 = buy_legs[-1] # Lowest strike
                d3 = l3._parsed
                
                # Ensure it's not the same as l1
                if d3['token'] != d1['token']:
                    self.positions.append({
                        'instrument_key': d3['token'],
                        'qty': abs(d3['qty']),
                        'side': 'BUY',
                        'entry_price': d3['buy_price'],
                        'strike': d3['strike'],
                        'type': 'PE',
                        'tag': 'IF_LEG3',
                        'expiry_dt': d3['expiry']
                    })
                    self.log(f"Synced IF_LEG3 (Buy): {d3['strike']}")

        return len(self.positions) > 0


    def calculate_total_pnl(self, quotes):
        pnl = 0
        for pos in self.positions:
            q = quotes.get(pos['instrument_key'])
            ltp = getattr(q, 'last_price', None) if q else None
            
            # If LTP is missing, we use entry price (assume 0 PnL for that leg) to avoid crashing or misleading spikes
            if ltp is None:
                continue
                
            if pos['side'] == 'BUY':
                pnl += (ltp - pos['entry_price']) * pos['qty']
            else:
                pnl += (pos['entry_price'] - ltp) * pos['qty']
        return pnl

    def exit_all_positions(self, order_callback, reason):
        # Margin optimization: Close short legs first (BUY back) then long legs (SELL)
        sorted_positions = sorted(self.positions, key=lambda x: 0 if x['side'] == 'SELL' else 1)
        for pos in sorted_positions:
            exit_side = 'SELL' if pos['side'] == 'BUY' else 'BUY'
            order_callback(pos['instrument_key'], pos['qty'], exit_side, f"{reason}_EXIT", expiry=pos.get('expiry_dt'))
        self.positions = []
        self.is_adjusted = False

    def save_state(self):
        super().save_current_state({'positions': self.positions, 'is_adjusted': self.is_adjusted})

    def load_previous_state(self):
        state = super().load_previous_state()
        if state:
            self.positions = state.get('positions', [])
            self.is_adjusted = state.get('is_adjusted', False)
            if self.positions:
                self.log(f"{Fore.CYAN}RECOVERY: Loaded {len(self.positions)} existing positions.{Style.RESET_ALL}")
            return True
        return False
