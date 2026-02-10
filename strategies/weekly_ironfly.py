import config
from datetime import datetime
from colorama import init, Fore, Style
from trade_logger import TradeJournal
from base_strategy import BaseStrategy

# Initialize colorama for Windows support
init(autoreset=True)

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
