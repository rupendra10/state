import csv
import os
import json
from datetime import datetime
from colorama import Fore, Style
import config
import logging
from logging.handlers import RotatingFileHandler
from collections import deque
import git_utils

class EventLogger:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EventLogger, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        self.log_file = "event_log.txt"
        self.max_lines = 500
        self.max_bytes = 5 * 1024 * 1024 # 5 MB
        
        # 1. Truncate on Startup (Keep last N lines)
        self._truncate_file()

        # 2. Setup Rotating Handler
        self.logger = logging.getLogger("AlgoEventLogger")
        self.logger.setLevel(logging.INFO)
        
        # Avoid adding multiple handlers if re-initialized
        if not self.logger.handlers:
            handler = RotatingFileHandler(
                self.log_file, 
                maxBytes=self.max_bytes, 
                backupCount=1
            )
            formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def _truncate_file(self):
        """Keeps only the last 500 lines of the file on startup."""
        if not os.path.exists(self.log_file):
            return
            
        try:
            with open(self.log_file, 'r') as f:
                lines = deque(f, maxlen=self.max_lines)
            
            # Only write back if we actually truncated
            if len(lines) > 0:
                with open(self.log_file, 'w') as f:
                    f.writelines(lines)
        except Exception as e:
            print(f"Error truncating log file: {e}")

    def log(self, message, print_to_console=False):
        # Strip color codes for file logging
        clean_msg = self._remove_ansi_colors(message)
        self.logger.info(clean_msg)
        
        if print_to_console:
            print(message)

    def _remove_ansi_colors(self, text):
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)


class TradeJournal:
    def __init__(self, filename="trade_log.csv"):
        # Add trading mode to filename
        mode = config.TRADING_MODE.lower()
        # Insert mode before .csv extension
        base_name = filename.replace('.csv', '')
        self.filename = f"{base_name}_{mode}.csv"
        self.headers = ['timestamp', 'instrument_key', 'side', 'qty', 'price', 'expiry', 'tag', 'pnl']
        self._initialize_file()
        self.closed_pnl = 0.0
        self._calculate_fixed_pnl()

    def _initialize_file(self):
        if not os.path.exists(self.filename):
            with open(self.filename, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.headers)
                writer.writeheader()

    def _calculate_fixed_pnl(self):
        """Pre-calculate P&L from historical closed trades if file exists."""
        # Simple implementation: sum of all 'pnl' columns that are numeric
        try:
            with open(self.filename, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row['pnl'] and row['pnl'] != 'None':
                        self.closed_pnl += float(row['pnl'])
        except Exception:
            pass

    def log_trade(self, instrument_key, side, qty, price, tag, expiry='N/A', pnl=None, check_duplicate=False):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        date_today = datetime.now().strftime("%Y-%m-%d")
        
        # Deduplication Logic
        if check_duplicate:
            try:
                # Optimized for small files. For large files, cache this.
                if os.path.exists(self.filename):
                    with open(self.filename, 'r') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            # Check entries from TODAY
                            if row['timestamp'].startswith(date_today):
                                if (row['instrument_key'] == instrument_key and 
                                    row['tag'] == tag):
                                    # Already Logged Today
                                    return
            except Exception as e:
                print(f"Log dedupe error: {e}")

        try:
            with open(self.filename, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.headers)
                writer.writerow({
                    'timestamp': timestamp,
                    'instrument_key': instrument_key,
                    'side': side,
                    'qty': qty,
                    'price': price,
                    'expiry': expiry,
                    'tag': tag,
                    'pnl': pnl
                })
        except PermissionError:
            print(f"{Fore.RED}WARNING: Could not write to log file {self.filename}. File might be open in another application.{Style.RESET_ALL}")
        except Exception as e:
            print(f"Log write error: {e}")
        if pnl is not None:
            self.closed_pnl += pnl

        # Sync Log to Git
        git_utils.sync_push(self.filename)

    def print_summary(self, open_pnl, strategy_state, broker_pnl=None):
        manual_adj = getattr(config, 'MANUAL_PNL_OFFSET', 0.0)
        total_pnl = self.closed_pnl + open_pnl + manual_adj
        pnl_color = Fore.GREEN if total_pnl >= 0 else Fore.RED
        
        print("\n" + "="*50)
        print(f"{Fore.CYAN}TRADE SESSION SUMMARY{Style.RESET_ALL}")
        print("="*50)
        print(f"Closed P&L:    {Fore.WHITE}INR {self.closed_pnl:,.2f}{Style.RESET_ALL}")
        print(f"Open P&L:      {Fore.WHITE}INR {open_pnl:,.2f}{Style.RESET_ALL}")
        if manual_adj != 0:
            adj_col = Fore.GREEN if manual_adj >= 0 else Fore.RED
            print(f"Manual Adj:    {adj_col}INR {manual_adj:,.2f}{Style.RESET_ALL}")
            
        print(f"Total P&L:     {pnl_color}INR {total_pnl:,.2f}{Style.RESET_ALL}")
        
        if broker_pnl is not None:
             bpnl_color = Fore.GREEN if broker_pnl >= 0 else Fore.RED
             print("-" * 50)
             print(f"DTO P&L (Broker): {bpnl_color}INR {broker_pnl:,.2f}{Style.RESET_ALL}")

        print("-" * 50)
        if broker_pnl is not None:
             bpnl_color = Fore.GREEN if broker_pnl >= 0 else Fore.RED
             print("-" * 50)
             print(f"DTO P&L (Broker): {bpnl_color}INR {broker_pnl:,.2f}{Style.RESET_ALL}")

        print("-" * 50)
        
        if strategy_state.get('weekly'):
            w = strategy_state['weekly']
            inst_type = 'Call' if w.get('type') == 'c' else 'Put'
            # Robust expiry display
            expiry_val = w.get('expiry_dt') or w.get('expiry', 'N/A')
            if isinstance(expiry_val, float):
                expiry_str = f"T~{expiry_val:.4f}y"
            else:
                expiry_str = str(expiry_val)
                
            strike = w.get('strike', 'N/A')
            entry = w.get('entry_price', 'N/A')
            ltp_raw = strategy_state.get('weekly_ltp')
            ltp = f"{ltp_raw:,.2f}" if ltp_raw is not None else "N/A"
            
            # Leg PnL
            leg_pnl = 0.0
            if ltp_raw is not None and entry != 'N/A':
                leg_pnl = (entry - ltp_raw) * config.ORDER_QUANTITY
            pnl_col = Fore.GREEN if leg_pnl >= 0 else Fore.RED
            
            # Delta
            delta = w.get('delta')
            delta_str = f"{delta:.2f}" if isinstance(delta, (int, float)) else "N/A"

            print(f"OPEN Weekly:   {Fore.YELLOW}{strike} {inst_type}{Style.RESET_ALL} @ {entry} (Expiry: {expiry_str} | Current: {ltp} | Delta: {delta_str} | {pnl_col}PnL: {leg_pnl:,.2f}{Style.RESET_ALL})")
            
        if strategy_state.get('monthly'):
            m = strategy_state['monthly']
            inst_type = 'Call' if m.get('type') == 'c' else 'Put'
            # Robust expiry display
            expiry_val = m.get('expiry_dt') or m.get('expiry', 'N/A')
            if isinstance(expiry_val, float):
                expiry_str = f"T~{expiry_val:.4f}y"
            else:
                expiry_str = str(expiry_val)
                
            strike = m.get('strike', 'N/A')
            entry = m.get('entry_price', 'N/A')
            ltp_raw = strategy_state.get('monthly_ltp')
            ltp = f"{ltp_raw:,.2f}" if ltp_raw is not None else "N/A"

            # Leg PnL
            leg_pnl = 0.0
            if ltp_raw is not None and entry != 'N/A':
                leg_pnl = (ltp_raw - entry) * config.ORDER_QUANTITY
            pnl_col = Fore.GREEN if leg_pnl >= 0 else Fore.RED

            # Delta
            delta = m.get('delta')
            delta_str = f"{delta:.2f}" if isinstance(delta, (int, float)) else "N/A"

            print(f"OPEN Monthly:  {Fore.YELLOW}{strike} {inst_type}{Style.RESET_ALL} @ {entry} (Expiry: {expiry_str} | Current: {ltp} | Delta: {delta_str} | {pnl_col}PnL: {leg_pnl:,.2f}{Style.RESET_ALL})")
        
        # Generic Position Support
        if strategy_state.get('positions'):
            # Extract expiry date from first position for header
            first_pos = strategy_state['positions'][0] if strategy_state['positions'] else {}
            expiry_info = first_pos.get('expiry_dt', 'N/A')
            
            print("-" * 25 + f" Open Legs (Expiry: {expiry_info}) " + "-" * 25)
            for p in strategy_state['positions']:
                side_col = Fore.GREEN if p['side'] == 'BUY' else Fore.RED
                qty = p['qty']
                entry = p['entry_price']
                ltp_raw = p.get('ltp')
                ltp = f"{ltp_raw:,.2f}" if isinstance(ltp_raw, (int, float)) else "N/A"
                
                # Calculate "Points" (Normalized to standard lot for easy strategy review)
                # If QTY=150 and Lot=75, multiplier is 2. Multiplier * Price = Points.
                multiplier = qty / config.ORDER_QUANTITY
                net_points_entry = entry * multiplier
                net_points_ltp = ltp_raw * multiplier if isinstance(ltp_raw, (int, float)) else 0.0
                
                # Leg PnL
                leg_pnl = 0.0
                if isinstance(ltp_raw, (int, float)):
                    if p['side'] == 'BUY':
                        leg_pnl = (ltp_raw - entry) * qty
                    else:
                        leg_pnl = (entry - ltp_raw) * qty
                pnl_col_val = Fore.GREEN if leg_pnl >= 0 else Fore.RED

                print(f"{side_col}{p['side']} {qty} {p.get('type','')} {p.get('strike','')} @ {entry}{Style.RESET_ALL} "
                      f"(LTP: {ltp} | PnL: {pnl_col_val}{leg_pnl:,.2f}{Style.RESET_ALL})")
        
        print("="*50 + "\n")
