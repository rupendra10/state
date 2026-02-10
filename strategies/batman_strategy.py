import config
from datetime import datetime, timedelta, date, time
from greeks import calculate_delta
from colorama import init, Fore, Style
from trade_logger import TradeJournal, EventLogger
from base_strategy import BaseStrategy
from utils import get_ist_now, get_next_trading_day
import re
import math

# Initialize colorama
init(autoreset=True)

class BatmanStrategy(BaseStrategy):
    def __init__(self, risk_free_rate=config.RISK_FREE_RATE):
        super().__init__("BatmanStrategy")
        # State Structure
        # positions = [] 
        # Each position: {'leg': 'call_wing'/'call_core'/'call_hedge'/'put_wing'/'put_core'/'put_hedge', 
        #                 'strike': K, 'qty': Q, 'type': 'ce'/'pe', 
        #                 'entry_price': P, 'delta': D, 'expiry_dt': 'YYYY-MM-DD', 'instrument_key': ...}
        self.positions = []
        
        self.adjustment_count = 0
        self.last_adjustment_date = None
        
        self.risk_free_rate = risk_free_rate
        self.journal = TradeJournal(filename="trade_log_batman.csv")
        self.event_logger = EventLogger()
        self.last_process_date = None

    def log(self, message):
        timestamp = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
        colored_message = message.replace("SOLD", f"{Fore.RED}SOLD{Style.RESET_ALL}").replace("BOUGHT", f"{Fore.GREEN}BOUGHT{Style.RESET_ALL}")
        colored_message = colored_message.replace("ENTRY", f"{Fore.YELLOW}ENTRY{Style.RESET_ALL}").replace("ADJUSTMENT", f"{Fore.MAGENTA}ADJUSTMENT{Style.RESET_ALL}")
        entry = f"[{timestamp}] [{self.name}] {colored_message}"
        print(entry)
        self.event_logger.log(f"[{self.name}] {message}")

    def update(self, market_data, order_callback):
        """
        Main logic loop.
        """
        # 0. Reconciliation
        if market_data.get('broker_positions') is not None:
             self.pull_from_broker(market_data.get('broker_positions'), master_df=market_data.get('master_df'), silent=True)

        spot = market_data.get('spot_price')
        cw_chain = market_data.get('cw_chain', [])
        now = market_data.get('now')
        
        if spot is None: return

        # 0.1 Auto-Cleanup (Expiry)
        today_str = now.strftime("%Y-%m-%d")
        active_positions = []
        for p in self.positions:
            if p.get('expiry_dt') and p.get('expiry_dt') < today_str:
                self.log(f"{Fore.RED}AUTO-CLEANUP: Leg {p['instrument_key']} expired. Removing.{Style.RESET_ALL}")
            else:
                active_positions.append(p)
        
        if len(active_positions) < len(self.positions):
            self.positions = active_positions
            self.save_state()

        has_acted = False

        # --- 1. EXIT LOGIC (T-1 Day or Max Adjustments) ---
        if self.positions:
            # A. Check Max Adjustments Forced Exit
            if self.adjustment_count > config.BATMAN_MAX_ADJUSTMENTS:
                 self.log(f"{Fore.RED}FORCED EXIT: Max Adjustments ({self.adjustment_count}) exceeded.{Style.RESET_ALL}")
                 self.exit_all_positions(order_callback, reason="MAX_ADJUSTMENTS")
                 has_acted = True
                 self.save_state()
                 return

            # B. Check T-1 Exit
            # We look at the expiry of our positions (all should be same weekly expiry)
            # Pick first position to check expiry
            first_pos = self.positions[0]
            expiry_dt_str = first_pos.get('expiry_dt')
            if expiry_dt_str and expiry_dt_str != 'N/A':
                expiry_dt = datetime.strptime(expiry_dt_str, '%Y-%m-%d').date()
                today = now.date()
                
                # Holiday-aware T-1
                next_trading_day_date = get_next_trading_day(today)
                
                # If NEXT trading day is Expiry, AND we are past the exit time on TODAY (T-1), Exit.
                if next_trading_day_date == expiry_dt:
                    if now.strftime("%H:%M") >= config.BATMAN_EXIT_TIME:
                        self.log(f"{Fore.MAGENTA}EXIT TRIGGER: T-1 (Day Before Expiry) at {config.BATMAN_EXIT_TIME}. Exiting Strategy.{Style.RESET_ALL}")
                        self.exit_all_positions(order_callback, reason="T-1_EXIT")
                        has_acted = True
                        self.save_state()
                        # Reset Adjustment Count on Strategy Exit
                        self.adjustment_count = 0
                        return

        # 2. Update Deltas
        self.update_deltas(spot, market_data)

        # 3. Entry Logic
        if not has_acted and not self.positions:
            # Check Timing
            # Weekday: Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
            if now.weekday() == config.BATMAN_ENTRY_WEEKDAY:
                current_time = now.strftime("%H:%M")
                if current_time >= config.BATMAN_ENTRY_TIME and current_time < "15:00":
                    self.enter_strategy(spot, cw_chain, order_callback)
                    has_acted = True
                    self.save_state()
        
        # 4. Adjustment Logic
        # "Adjustments are rule-based and focus on the winning leg"
        if not has_acted and self.positions:
            if self.check_adjustments(spot, cw_chain, order_callback):
                has_acted = True
                self.save_state()

        # 5. PnL Logging
        self.log_pnl_summary(spot, market_data)

    def select_strike_by_distance(self, spot, chain, distance, option_type, is_buy=True):
        """
        Finds strike at spot + distance (CE) or spot - distance (PE).
        """
        target_strike = round((spot + distance) / 50) * 50 if option_type == 'CE' else round((spot - distance) / 50) * 50
        
        # Find exact or closest
        best_opt = None
        min_diff = float('inf')
        
        candidates = [opt for opt in chain if opt['type'] == ('c' if option_type == 'CE' else 'p')]
        
        for opt in candidates:
            diff = abs(opt['strike'] - target_strike)
            if diff < min_diff:
                min_diff = diff
                best_opt = opt
        
        return best_opt

    def select_strike_by_delta(self, chain, target_delta, option_type):
        """
        Finds strike closest to target delta.
        """
        best_opt = None
        min_diff = float('inf')
        
        candidates = [opt for opt in chain if opt['type'] == ('c' if option_type == 'CE' else 'p')]
        
        for opt in candidates:
            # Filter bad data
            if opt.get('delta') is None and opt.get('calculated_delta') is None: continue
            
            curr_delta = abs(opt.get('delta', opt.get('calculated_delta')))
            diff = abs(curr_delta - target_delta)
            
            if diff < min_diff:
                min_diff = diff
                best_opt = opt
        
        return best_opt

    def enter_strategy(self, spot, chain, order_callback):
        self.log(f"Attempting Batman Strategy Entry at Spot {spot}")
        
        # Structure:
        # CE Side: Buy ATM+100 (Wing), Sell 2x ATM+200 (Core), Buy 1x Far OTM (Hedge)
        # PE Side: Buy ATM-100 (Wing), Sell 2x ATM-200 (Core), Buy 1x Far OTM (Hedge)
        
        dist_wing = config.BATMAN_WING_DIST
        dist_core = config.BATMAN_CORE_DIST
        
        # Identify Legs
        ce_wing = self.select_strike_by_distance(spot, chain, dist_wing, 'CE')
        ce_core = self.select_strike_by_distance(spot, chain, dist_core, 'CE')
        ce_hedge = self.select_strike_by_delta(chain, config.BATMAN_HEDGE_DELTA, 'CE')
        
        pe_wing = self.select_strike_by_distance(spot, chain, dist_wing, 'PE')
        pe_core = self.select_strike_by_distance(spot, chain, dist_core, 'PE')
        pe_hedge = self.select_strike_by_delta(chain, config.BATMAN_HEDGE_DELTA, 'PE')
        
        if not (ce_wing and ce_core and ce_hedge and pe_wing and pe_core and pe_hedge):
            self.log("ERROR: Could not find all required strikes for Batman Entry.")
            return

        # Execute Orders (Buy Hedges -> Buy Wings -> Sell Cores to manage margin)
        # Buy Hedges
        self.place_entry_order(ce_hedge, 1, 'BUY', 'CE_HEDGE', order_callback)
        self.place_entry_order(pe_hedge, 1, 'BUY', 'PE_HEDGE', order_callback)
        
        # Buy Wings
        self.place_entry_order(ce_wing, 1, 'BUY', 'CE_WING', order_callback)
        self.place_entry_order(pe_wing, 1, 'BUY', 'PE_WING', order_callback)
        
        # Sell Cores (2 Lots)
        self.place_entry_order(ce_core, 2, 'SELL', 'CE_CORE', order_callback)
        self.place_entry_order(pe_core, 2, 'SELL', 'PE_CORE', order_callback)
        
        self.adjustment_count = 0

    def place_entry_order(self, opt, qty_mult, side, tag, order_callback):
        if not order_callback: return
        
        qty = config.ORDER_QUANTITY * qty_mult
        resp = order_callback(opt['instrument_key'], qty, side, f"BATMAN_ENTRY_{tag}", expiry=opt.get('expiry_dt'))
        
        if resp and resp.get('status') == 'success':
            price = resp.get('avg_price', opt.get('ltp', 0.0))
            self.positions.append({
                'leg': tag,
                'strike': opt['strike'],
                'qty': qty,
                'type': opt['type'],
                'side': side,
                'entry_price': price,
                'delta': opt.get('delta', 0.5), # Initial approx
                'expiry_dt': opt.get('expiry_dt'),
                'instrument_key': opt['instrument_key']
            })
            self.journal.log_trade(opt['instrument_key'], side, qty, price, f"ENTRY_{tag}", expiry=opt.get('expiry_dt'))
            self.log(f"ENTRY: {side} {qty} {tag} ({opt['strike']}) @ {price}")
        else:
             self.log(f"ERROR: Entry Failed for {tag} - {resp.get('message')}")

    def update_deltas(self, spot, market_data):
        greeks = market_data.get('greeks', {})
        chain = market_data.get('cw_chain', []) # Fallback for calc
        
        for p in self.positions:
            key = p['instrument_key']
            if key in greeks:
               val = greeks[key].get('delta')
               if val is not None:
                   p['delta'] = abs(val) # Store absolute delta for simplicity
            else:
                # Recalculate if needed (using chain data or just keep old)
                pass

    def check_adjustments(self, spot, chain, order_callback):
        # Trigger: Combined Delta of SELL legs (Cores) drops to 0.35 - 0.40
        ce_sold_delta = 0.0
        pe_sold_delta = 0.0
        
        ce_sold_legs = [p for p in self.positions if p['type'] == 'c' and 'CORE' in p['leg']] 
        pe_sold_legs = [p for p in self.positions if p['type'] == 'p' and 'CORE' in p['leg']]
        
        for p in ce_sold_legs:
            # Assuming 'qty' is total qty, and we know lot size. 
            # Strategy logic check combined delta of sold legs.
            # Assuming delta per lot logic as discussed.
            num_lots = p['qty'] / config.ORDER_QUANTITY
            ce_sold_delta += (p['delta'] * num_lots)
            
        for p in pe_sold_legs:
            num_lots = p['qty'] / config.ORDER_QUANTITY
            pe_sold_delta += (p['delta'] * num_lots)
            
        trigger = config.BATMAN_ADJ_TRIGGER_COMBINED_DELTA # 0.40
        
        # Check CE Side (Market likely fell, Call delta dropped - WINNING SIDE)
        if ce_sold_legs and ce_sold_delta < trigger:
            self.log(f"ADJUSTMENT TRIGGER: CE Core Combined Delta {ce_sold_delta:.2f} < {trigger}. Winning Leg Adjustment.")
            return self.perform_adjustment(ce_sold_legs, 'CE', spot, chain, order_callback)
            
        # Check PE Side (Market likely rose, Put delta dropped - WINNING SIDE)
        if pe_sold_legs and pe_sold_delta < trigger:
            self.log(f"ADJUSTMENT TRIGGER: PE Core Combined Delta {pe_sold_delta:.2f} < {trigger}. Winning Leg Adjustment.")
            return self.perform_adjustment(pe_sold_legs, 'PE', spot, chain, order_callback)

        return False

    def perform_adjustment(self, sold_legs, side, spot, chain, order_callback):
        # 1. Check Max Adjustments
        if self.adjustment_count >= config.BATMAN_MAX_ADJUSTMENTS:
            self.log(f"{Fore.RED}MAX ADJUSTMENTS REACHED ({self.adjustment_count}). FORCE EXITING STRATEGY.{Style.RESET_ALL}")
            self.exit_all_positions(order_callback, reason="MAX_ADJ_LIMIT")
            return True # true means acted
            
        self.adjustment_count += 1
        self.log(f"Executing Adjustment #{self.adjustment_count} for {side} Side.")
        
        # 2. Square Off Sold Lots (The Winning Side)
        for p in sold_legs:
            resp = order_callback(p['instrument_key'], p['qty'], 'BUY', f"ADJ_EXIT_{side}", expiry=p.get('expiry_dt'))
            if resp and resp.get('status') == 'success':
                 self.positions.remove(p)
                 price = resp.get('avg_price', 0.0)
                 self.journal.log_trade(p['instrument_key'], 'BUY', p['qty'], price, f"ADJ_EXIT_{side}", expiry=p.get('expiry_dt'))
                 self.log(f"Square Off {side} Core: {p['strike']} @ {price}")
            else:
                 self.log(f"ERROR: Adjustment Exit Failed - {resp.get('message')}")
        
        # 3. Enter New Sold Lots (Target Combined Delta 0.70 => 0.35 per lot)
        target_per_lot = config.BATMAN_ADJ_TARGET_COMBINED_DELTA / 2.0
        
        new_opt = self.select_strike_by_delta(chain, target_per_lot, side)
        if new_opt:
             # Sell 2 lots
             self.place_entry_order(new_opt, 2, 'SELL', f"{side}_CORE_ADJ", order_callback)
             self.log(f"Rolled to New {side} Core: {new_opt['strike']} (Delta {new_opt.get('delta', 0):.2f})")
        else:
             self.log(f"ERROR: Could not find new strike for adjustment!")
             
        # "Do Not Move the Buy Leg": logic ends here.
        return True

    def exit_all_positions(self, order_callback, reason="MANUAL"):
        self.log(f"EXITING ALL POSITIONS: {reason}")
        for p in self.positions[:]: # Copy list
            # Usually: Core = SELL -> BUY to exit. Wing/Hedge = BUY -> SELL to exit.
            current_side = 'SELL' if ('CORE' in p['leg'] or 'SELL' in p['leg'].upper()) else 'BUY'
            
            # If we currently HOLD a SELL position (Short), we need to BUY.
            # If we currently HOLD a BUY position (Long), we need to SELL.
            
            # Wait, my logic in enter_strategy puts 'type' as 'ce'/'pe'.
            # It does NOT explicitly store "Side" (Buy/Sell) in a simple field, but inferred from 'leg' name.
            # Wings/Hedges are BOUGHT. Cores are SOLD.
            
            is_currently_long = (p['leg'].endswith('HEDGE') or p['leg'].endswith('WING'))
            is_currently_short = ('CORE' in p['leg'])
            
            exit_side = 'SELL' if is_currently_long else 'BUY'
            
            resp = order_callback(p['instrument_key'], p['qty'], exit_side, f"EXIT_{reason}", expiry=p.get('expiry_dt'))
            if resp and resp.get('status') == 'success':
                price = resp.get('avg_price', 0.0)
                self.journal.log_trade(p['instrument_key'], exit_side, p['qty'], price, f"EXIT_{reason}", expiry=p.get('expiry_dt'))
                self.positions.remove(p)
                self.log(f"Exited {p['leg']}: {p['strike']} @ {price}")
            else:
                self.log(f"ERROR: Exit Failed for {p['leg']}")
        
        self.adjustment_count = 0
        self.save_state()

    def log_pnl_summary(self, spot, market_data):
        quote_map = market_data.get('quotes', {})
        total_pnl = 0.0
        
        for p in self.positions:
            key = p['instrument_key']
            ltp = 0.0
            if key in quote_map:
                ltp = quote_map[key].last_price
            
            if ltp > 0:
                entry = p['entry_price']
                if 'CORE' in p['leg']: # Short
                     pnl = (entry - ltp) * p['qty']
                else: # Long
                     pnl = (ltp - entry) * p['qty']
                total_pnl += pnl
        
        self.journal.print_summary(total_pnl, {'positions': self.positions, 'adj_count': self.adjustment_count})
        
    def save_state(self):
        state = {
            'positions': self.positions,
            'adjustment_count': self.adjustment_count
        }
        super().save_current_state(state) # Saves to json

    def load_previous_state(self):
        """
        Loads state from persistent storage and restores class attributes.
        """
        state = super().load_previous_state()
        
        if state:
            self.positions = state.get('positions', [])
            self.adjustment_count = state.get('adjustment_count', 0)
            
            self.log(f"{Fore.CYAN}RECOVERY: Loaded {len(self.positions)} positions. Adj Count: {self.adjustment_count}{Style.RESET_ALL}")
            return True
        return False

    def pull_from_broker(self, broker_positions, master_df=None, silent=False):
        """
        Reconcile tracked positions with Broker/API positions.
        Primary Goal: Detect manual exits or discrepancies.
        """
        if not self.positions:
            return False
            
        if not broker_positions:
            if not silent:
                self.log(f"{Fore.RED}WARNING: Broker positions Empty/None. Assuming all positions closed? Skipping to be safe.{Style.RESET_ALL}")
            # Identify if it's a real empty list vs None (Error)
            if broker_positions is None: return False
            # If it is [], it means really no positions.
        
        # Create a map of Broker Positions by Token for fast lookup
        broker_map = {}
        if broker_positions:
            for p in broker_positions:
                # Upstox returns 'instrument_token' or similar. 
                # wrapper.py usually normalizes to object with attributes or dict
                # Let's assume wrapper returns list of objects/dicts that have 'instrument_token' or 'instrument_key'
                # wrapper.py get_positions() returns list of dicts usually?
                # Let's check wrapper.py if unsure, but standardizing on 'instrument_token' is common.
                # However, our system uses 'instrument_key' (NSE_FO|...)
                
                # Handling variation in Upstox response vs our normalized dict
                p_key = p.get('instrument_token') or p.get('instrument_key') or p.get('token')
                if p_key:
                    broker_map[p_key] = p
        
        # Iterate over OUR tracked positions
        active_positions = []
        changes_detected = False
        
        for tracked in self.positions:
            key = tracked['instrument_key']
            
            if key in broker_map:
                broker_p = broker_map[key]
                # Check Quantity
                # Broker quantity is Net Intraday + Delivery? usually 'quantity' or 'net_quantity'
                b_qty = broker_p.get('quantity')
                if b_qty is None: b_qty = broker_p.get('net_quantity', 0)
                
                # Check Side (Net Qty > 0 BUY, < 0 SELL)
                # Our tracked['qty'] is always positive, with tracked['side'] or inferred side.
                # tracked position structure: {'qty': 50, 'side': 'BUY', ...}
                
                # Convert Broker Net Qty to absolute and side
                b_net_qty = int(b_qty)
                b_side = 'BUY' if b_net_qty > 0 else 'SELL'
                b_abs_qty = abs(b_net_qty)
                
                if b_abs_qty == 0:
                    # Position Closed in Broker
                    self.log(f"{Fore.MAGENTA}RECONCILIATION: Position {tracked['leg']} ({key}) found CLOSED in broker. Removing.{Style.RESET_ALL}")
                    changes_detected = True
                    continue # Do not add to active_positions
                
                # Update Quantity if partial
                if b_abs_qty != tracked['qty']:
                    self.log(f"{Fore.YELLOW}RECONCILIATION: Qty Mismatch for {key}. Algo: {tracked['qty']}, Broker: {b_abs_qty}. Updating.{Style.RESET_ALL}")
                    tracked['qty'] = b_abs_qty
                    changes_detected = True
                    
                # Check Side Consistency (Rare flip)
                # If we think we are Long, but broker says Short?
                # tracked['side'] was added recently.
                if 'side' in tracked and tracked['side'] != b_side:
                     self.log(f"{Fore.RED}CRITICAL: Side Mismatch for {key}. Algo: {tracked['side']}, Broker: {b_side}. Updating.{Style.RESET_ALL}")
                     tracked['side'] = b_side
                     changes_detected = True
                
                active_positions.append(tracked)
                
            else:
                # Tracked position NOT found in broker map
                # Implies it was closed externally or expired?
                self.log(f"{Fore.MAGENTA}RECONCILIATION: Position {tracked['leg']} ({key}) NOT FOUND in broker. Assuming Closed.{Style.RESET_ALL}")
                changes_detected = True
                # Do not add to active_positions
        
        if changes_detected:
            self.positions = active_positions
            self.save_state()
            return True
            
        return False 
