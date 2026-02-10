import os
import pandas as pd
import time
from datetime import datetime, date, timedelta
from upstox_wrapper import UpstoxWrapper
from instrument_manager import InstrumentMaster
from strategies import CalendarPEWeekly, WeeklyIronfly, BatmanStrategy
import config
from greeks import calculate_delta
from utils import calculate_implied_volatility, get_ist_now
from event_monitor import print_event_summary
from colorama import Fore, Style

# Strategy Mapping for dynamic selection
STRATEGY_CLASSES = {
    'CalendarPEWeekly': CalendarPEWeekly,
    'WeeklyIronfly': WeeklyIronfly,
    'BatmanStrategy': BatmanStrategy
}

def main():
    print(f"{Fore.CYAN}Starting Multi-Strategy Algo...{Style.RESET_ALL}")
    print_event_summary()
    
    # 1. Setup API and Master Data
    api = UpstoxWrapper() # Reads token from Config/Env
    master = InstrumentMaster()
    master.load_master()
    
    # 2. Instantiate and Initialize Active Strategies
    active_strategies = []
    print(f"Loading Active Strategies: {config.ACTIVE_STRATEGIES}")
    for s_name in config.ACTIVE_STRATEGIES:
        if s_name in STRATEGY_CLASSES:
            strat_inst = STRATEGY_CLASSES[s_name]()
            strat_inst.load_previous_state()
            active_strategies.append(strat_inst)
        else:
            print(f"{Fore.RED}WARNING: Strategy '{s_name}' is not recognized.{Style.RESET_ALL}")

    if not active_strategies:
        print(f"{Fore.RED}CRITICAL: No valid strategies configured. Exiting.{Style.RESET_ALL}")
        return

    # 2.5 Auto-Sync with Broker at startup
    if config.AUTO_SYNC_ON_STARTUP and config.TRADING_MODE == 'LIVE':
        print(f"{Fore.YELLOW}AUTO_SYNC_ON_STARTUP is ENABLED. Reconciling active strategies with broker positions...{Style.RESET_ALL}")
        broker_positions = api.get_positions()
        if broker_positions:
            for strat in active_strategies:
                if strat.pull_from_broker(broker_positions, master_df=master.df):
                    strat.save_state()
                    print(f"{Fore.GREEN}Successfully synced {strat.name} state from broker.{Style.RESET_ALL}")
                else:
                    # Not an error if the user hasn't opened any trades for this strategy yet
                    print(f"{Fore.CYAN}No existing {strat.name} positions found in broker account.{Style.RESET_ALL}")
        else:
            print(f"{Fore.CYAN}No active positions found in broker portfolio.{Style.RESET_ALL}")
    
    # Display Trading Mode Banner
    mode_color = Fore.RED if config.TRADING_MODE == 'LIVE' else Fore.CYAN
    print("\n" + "="*60)
    print(f"{mode_color}TRADING MODE: {config.TRADING_MODE}{Style.RESET_ALL}")
    print(f"Active Strategies: {', '.join([s.name for s in active_strategies])}")
    if config.TRADING_MODE == 'LIVE':
        print(f"{Fore.YELLOW}[!] LIVE MODE - Real money at risk!{Style.RESET_ALL}")
        if config.STRICT_MONTHLY_EXPIRY_ENTRY:
            print(f"CalendarPEWeekly: Entries on Monthly Expiry Day at {config.ENTRY_TIME_HHMM}")
        print(f"WeeklyIronfly: Entries on Current Weekly Expiry at {config.IRONFLY_ENTRY_TIME}")
        print(f"BatmanStrategy: Entries on Wednesday at {config.BATMAN_ENTRY_TIME}")
    else:
        print(f"{Fore.GREEN}[P] PAPER MODE - Simulation only{Style.RESET_ALL}")
    print("="*60 + "\n")

    # 3. Identify Expiries Dynamically
    expiries = master.get_expiry_dates(config.UNDERLYING_NAME)
    if not expiries or len(expiries) < 2:
        print(f"{Fore.RED}CRITICAL ERROR: Could not find at least two future expiries.{Style.RESET_ALL}")
        return

    curr_weekly = expiries[0]
    next_weekly = expiries[1]
    
    # NEW: Skip today's expiry if executing freshly on an expiry day
    today = date.today()
    expiry_skipped = False
    if curr_weekly == today:
        print(f"{Fore.YELLOW}Today is expiry day ({today}). Shifting to future expiries as per requirement.{Style.RESET_ALL}")
        expiry_skipped = True
        curr_weekly = expiries[1]
        if len(expiries) > 2:
            next_weekly = expiries[2]
        else:
            print(f"{Fore.RED}WARNING: Not enough future expiries found after shifting.{Style.RESET_ALL}")
    
    # Identify Monthly only if needed
    needs_monthly = 'CalendarPEWeekly' in config.ACTIVE_STRATEGIES
    
    monthly_expiry = None
    m_expiries = [] 
    if needs_monthly:
        # Find the last expiry of the next month relative to our (possibly shifted) curr_weekly
        target_month = curr_weekly.month + 1
        target_year = curr_weekly.year
        if target_month > 12: target_month = 1; target_year += 1
        
        # We look through all expiries, skipping today if it was skipped for weekly
        search_expiries = expiries[1:] if expiries[0] == today else expiries
        
        m_expiries = [d for d in search_expiries if d.year == target_year and d.month == target_month]
        if not m_expiries:
            # Fallback: next month after target_month
            target_month += 1
            if target_month > 12: target_month = 1; target_year += 1
            m_expiries = [d for d in search_expiries if d.year == target_year and d.month == target_month]
            
        monthly_expiry = m_expiries[-1] if m_expiries else expiries[-1]

    print(f"{Fore.CYAN}Expiries Identified:{Style.RESET_ALL}")
    print(f" - [Main Weekly]:    {curr_weekly}")
    print(f" - [Next Weekly]:    {next_weekly} (Target for WeeklyIronfly Entry)")
    if monthly_expiry:
        print(f" - [Monthly Hedge]:  {monthly_expiry} (For CalendarPEWeekly)")
    elif 'WeeklyIronfly' in config.ACTIVE_STRATEGIES:
        print(f" - [Monthly]:        Not pre-fetched (WeeklyIronfly only needs for adjustments)")

    # Pre-fetch instrument lists for all relevant segments
    # Current Weekly
    cw_pe = master.get_option_symbols(config.UNDERLYING_NAME, curr_weekly, 'PE')
    cw_ce = master.get_option_symbols(config.UNDERLYING_NAME, curr_weekly, 'CE')
    # Next Weekly
    nw_pe = master.get_option_symbols(config.UNDERLYING_NAME, next_weekly, 'PE')
    nw_ce = master.get_option_symbols(config.UNDERLYING_NAME, next_weekly, 'CE')
    # Monthly
    m_pe = master.get_option_symbols(config.UNDERLYING_NAME, monthly_expiry, 'PE') if monthly_expiry else pd.DataFrame()
    m_ce = master.get_option_symbols(config.UNDERLYING_NAME, monthly_expiry, 'CE') if monthly_expiry else pd.DataFrame()

    is_expiry_today = master.is_monthly_expiry_today(config.UNDERLYING_NAME)
    is_expiry_today = master.is_monthly_expiry_today(config.UNDERLYING_NAME)
    
    # HOLIDAY AWARENESS: Determine "Effective Tomorrow" (Next Trading Day)
    from utils import get_next_trading_day
    effective_tomorrow = get_next_trading_day(date.today())
    
    # Check if this "Effective Tomorrow" is the monthly expiry
    # Note: If Jan 27 is the last expiry of Jan, m_expiries logic might need to ensure it covers it.
    # But relies on the fact that if we are HERE, needs_monthly is True.
    
    # Needs Monthly?
    if 'BatmanStrategy' in config.ACTIVE_STRATEGIES:
         # Batman might technically use monthly if we wanted to be super safe, but it uses Weekly options.
         # So we don't strictly need it unless we want to monitor.
         pass
    
    # GENERAL RULE: If the *next trading day* IS the Monthly Expiry, then TODAY is the Exit Day (T-1)
    is_day_before_monthly_expiry = False
    
    if needs_monthly and m_expiries:
        # Check if effective tomorrow matches ANY monitored expiry 
        # But specifically for 'is_day_before_monthly_expiry', we match the TARGET monthly.
        
        # Logic: If next trading day is the current weekly, AND that current weekly is effectively monthly end
        if effective_tomorrow == curr_weekly:
             # Check if there are any later expiries in the same month
             tomorrow_month = effective_tomorrow.month
             later_in_month = [d for d in expiries if d.year == effective_tomorrow.year and d.month == tomorrow_month and d > effective_tomorrow]
             
             if not later_in_month:
                 # It is the last expiry of the month -> effectively Monthly
                 is_day_before_monthly_expiry = True
             elif effective_tomorrow == m_expiries[-1]:
                 is_day_before_monthly_expiry = True
                 
    print(f"{Fore.CYAN}Holiday-Aware check: Today={date.today()}, NextTrading={effective_tomorrow}, IsPreExpiry={is_day_before_monthly_expiry}{Style.RESET_ALL}")
    
    # 4. Main Polling Loop
    last_adj_minute = -1
    try:
        while True:
            now = get_ist_now()

            # MARKET HOURS CHECK (LIVE MODE)
            # Prevent pre-market execution/adjustments
            if config.TRADING_MODE == 'LIVE':
                current_time_str = now.strftime("%H:%M:%S")
                # Strict 9:15 Start
                if current_time_str < "09:15:00":
                    print(f"[{current_time_str}] Pre-Market. Waiting for 09:15 AM Open...")
                    time.sleep(10)
                    continue
                # Optional: Stop after 15:30, though some might want to let it run to settle logs
                elif current_time_str > "15:35:00":
                     print(f"[{current_time_str}] Market Closed. Waiting...")
                     time.sleep(60)
                     continue
            
            # Candle-Based Adjustment Logic (5-min intervals)
            adj_interval = 5
            can_adjust = False
            if now.minute % adj_interval == 0 and now.minute != last_adj_minute:
                can_adjust = True
                # We update last_adj_minute below ONLY IF we actually processed the strategies
                # But for now, let's mark it so we don't trigger multiple times in the same minute
                last_adj_minute = now.minute

            # A. Get Spot Price
            spot_price = api.get_spot_price(config.SPOT_INSTRUMENT_KEY)
            if not spot_price:
                print("Waiting for quote...")
                time.sleep(5)
                continue
            
            adj_status = f"{Fore.GREEN}ADJ WINDOW OPEN{Style.RESET_ALL}" if can_adjust else f"Next Adj: {adj_interval - (now.minute % adj_interval)}m"
            print(f"[{now.strftime('%H:%M:%S')}] Spot: {spot_price} | {adj_status}")
            
            # B. Build Market Data Context
            # Filter options around ATM (±500 pts)
            atm = round(spot_price / 50) * 50
            strikes = range(atm - 500, atm + 550, 50)
            
            # Combine all keys for quotes
            def get_near_df(pe_df, ce_df):
                p_near = pe_df[pe_df['strike'].isin(strikes)]
                c_near = ce_df[ce_df['strike'].isin(strikes)]
                return p_near, c_near

            cw_pe_near, cw_ce_near = get_near_df(cw_pe, cw_ce)
            nw_pe_near, nw_ce_near = get_near_df(nw_pe, nw_ce)
            m_pe_near, m_ce_near = get_near_df(m_pe, m_ce) if needs_monthly else (pd.DataFrame(columns=['instrument_key']), pd.DataFrame(columns=['instrument_key']))
            
            all_keys = list(set(cw_pe_near['instrument_key'].tolist() + cw_ce_near['instrument_key'].tolist() +
                                nw_pe_near['instrument_key'].tolist() + nw_ce_near['instrument_key'].tolist() +
                                m_pe_near['instrument_key'].tolist() + m_ce_near['instrument_key'].tolist()))
            
            # Ensure currently held positions are ALWAYS included, even if they drift away from ATM
            for strat in active_strategies:
                # CalendarPEWeekly style
                if hasattr(strat, 'weekly_position') and strat.weekly_position:
                    all_keys.append(strat.weekly_position['instrument_key'])
                if hasattr(strat, 'monthly_position') and strat.monthly_position:
                    all_keys.append(strat.monthly_position['instrument_key'])

                # WeeklyIronfly style
                if hasattr(strat, 'positions') and strat.positions:
                   for pos in strat.positions:
                       all_keys.append(pos['instrument_key'])
            
            all_keys = list(set(all_keys))

            # NEW: Perform metadata recovery for held positions using Master DF
            for strat in active_strategies:
                # CalendarPEWeekly style
                for pos_attr in ['weekly_position', 'monthly_position']:
                    pos = getattr(strat, pos_attr, None)
                    # We check if expiry_dt is missing OR is a float (the old 'expiry' field format)
                    if pos and (not pos.get('expiry_dt') or pos.get('expiry_dt') == 'N/A' or isinstance(pos.get('expiry_dt'), float)):
                        key = pos['instrument_key']
                        match = master.df[master.df['instrument_key'] == key]
                        if not match.empty:
                            row = match.iloc[0]
                            pos['expiry_dt'] = str(row['expiry_dt'])
                            if 'type' not in pos: pos['type'] = row['instrument_type'].lower()
                            if 'strike' not in pos: pos['strike'] = float(row['strike'])
                            strat.save_state()

                # WeeklyIronfly style
                if hasattr(strat, 'positions') and strat.positions:
                   changed = False
                   for pos in strat.positions:
                       if not pos.get('expiry_dt') or pos.get('expiry_dt') == 'N/A':
                            key = pos['instrument_key']
                            match = master.df[master.df['instrument_key'] == key]
                            if not match.empty:
                                row = match.iloc[0]
                                pos['expiry_dt'] = str(row['expiry_dt'])
                                if 'type' not in pos: pos['type'] = row['instrument_type']
                                if 'strike' not in pos: pos['strike'] = float(row['strike'])
                                changed = True
                   if changed:
                       strat.save_state()

            if len(all_keys) > 250:
                print(f"{Fore.YELLOW}WARNING: Requesting high number of symbols ({len(all_keys)}). Possible rate limit risk.{Style.RESET_ALL}")
            
            quotes = api.get_option_chain_quotes(all_keys)
            greeks = api.get_option_greeks(all_keys)
            
            # REMAPPING FIX: Map NSE_FO|Symbol -> NSE_FO|Token
            # The API returns keys as Symbols (e.g. NSE_FO|NIFTY26FEB...), but Strategy uses Tokens (NSE_FO|40476)
            # We use master.df to bridge this gap.
            if not getattr(master, 'symbol_map', None):
                 # lazy init map
                 master.symbol_map = {}
                 if master.df is not None and not master.df.empty:
                     month_map = {10: 'O', 11: 'N', 12: 'D'}
                     for i in range(1, 10): month_map[i] = str(i)
                     
                     for _, row in master.df.iterrows():
                         try:
                             # Base Components
                             symbol = row['name'] # NIFTY
                             # CRITICAL FIX: master.df has 'strike_price', not 'strike'
                             strike = int(float(row['strike_price']))
                             opt_type = row['instrument_type'] # CE/PE
                             
                             # SAFE DATE CONVERSION
                             # row['expiry_dt'] might be string or date or nan
                             raw_exp = row['expiry_dt']
                             if pd.isna(raw_exp): continue
                             
                             exp = pd.to_datetime(raw_exp).date()
                             
                             yy = exp.strftime('%y')
                             mmm = exp.strftime('%b').upper()
                             dd = exp.strftime('%d')
                             m_char = month_map[exp.month]
                             
                             # Format 1: Weekly (NIFTY 26 1 06 26150 PE) -> SYMBOL YY M DD STRIKE TYPE
                             # Note: Upstox Weekly Format is SYMBOL + YY + M + DD + STRIKE + TYPE
                             fmt_weekly = f"NSE_FO|{symbol}{yy}{m_char}{dd}{strike}{opt_type}"
                             master.symbol_map[fmt_weekly] = row['instrument_key']
                             
                             # Format 2: Monthly (NIFTY 26 FEB 26200 PE) -> SYMBOL YY MMM STRIKE TYPE
                             # Note: Upstox Monthly Format is SYMBOL + YY + MMM + STRIKE + TYPE
                             fmt_monthly = f"NSE_FO|{symbol}{yy}{mmm}{strike}{opt_type}"
                             master.symbol_map[fmt_monthly] = row['instrument_key']
                             
                             # Fallback: Space-stripped Trading Symbol (just in case)
                             ts = str(row['trading_symbol']).replace(' ', '')
                             master.symbol_map[f"NSE_FO|{ts}"] = row['instrument_key']
                             
                         except Exception:
                             continue
            
            # Inject Token keys into greeks dict
            if greeks:
                keys_to_add = {}
                for key, val in greeks.items():
                    # If key is likely a symbol (contains letters/dates not just numbers after pipe)
                    # OR just try looking it up
                    if key in master.symbol_map:
                        token_key = master.symbol_map[key]
                        keys_to_add[token_key] = val
                
                greeks.update(keys_to_add)
            
            # Helper to package chain data
            def package_chain(pe_df, ce_df, q_dict, g_dict, spot, t_now):
                chain = []
                # Use raw dataframes but only process what we have quotes for
                for df, opt_type in [(pe_df, 'p'), (ce_df, 'c')]:
                    # Optimization: only look at keys we actually fetched
                    df_relevant = df[df['instrument_key'].isin(q_dict.keys())]
                    for _, row in df_relevant.iterrows():
                        key = row['instrument_key']
                        ltp = q_dict[key].last_price
                        tte = (datetime.combine(row['expiry_dt'], datetime.min.time()) - t_now).total_seconds() / (365*24*3600)
                        if tte <= 0: tte = 0.0001
                        
                        # Restore definition
                        broker_data = g_dict.get(key, {})
                        broker_delta = broker_data.get('delta')

                        iv_val = broker_data.get('iv')
                        if not iv_val:
                             iv_val = calculate_implied_volatility(ltp, spot, row['strike'], tte, config.RISK_FREE_RATE if hasattr(config, 'RISK_FREE_RATE') else 0.05, opt_type)
                        
                        calc_delta = calculate_delta(opt_type, spot, row['strike'], tte, config.RISK_FREE_RATE if hasattr(config, 'RISK_FREE_RATE') else 0.05, iv_val)
                        
                        final_delta = broker_delta if broker_delta is not None else calc_delta

                        chain.append({
                            'strike': row['strike'],
                            'iv': iv_val,
                            'time_to_expiry': tte,
                            'expiry_dt': row['expiry_dt'].strftime('%Y-%m-%d') if isinstance(row['expiry_dt'], (date, datetime)) else str(row['expiry_dt']),
                            'instrument_key': key,
                            'ltp': ltp,
                            'type': opt_type,
                            'delta': final_delta,
                            'calculated_delta': calc_delta
                        })
                return chain

            cw_chain_data = package_chain(cw_pe, cw_ce, quotes, greeks, spot_price, now)
            nw_chain_data = package_chain(nw_pe, nw_ce, quotes, greeks, spot_price, now)
            m_chain_data = package_chain(m_pe, m_ce, quotes, greeks, spot_price, now) if needs_monthly else []

            # Create Execution Callback
            def place_trade_callback(instrument_key, qty, side, tag, expiry='N/A'):
                side_colored = f"{Fore.GREEN}{side}{Style.RESET_ALL}" if side == 'BUY' else f"{Fore.RED}{side}{Style.RESET_ALL}"
                if config.TRADING_MODE == 'PAPER':
                    price = quotes[instrument_key].last_price if instrument_key in quotes else 0.0
                    print(f"[{datetime.now()}] [{Fore.CYAN}PAPER{Style.RESET_ALL}] {side_colored} {qty} | Key: {instrument_key} | Price: {price} | Expiry: {expiry}")
                    return {'status': 'success', 'avg_price': price}
                else:
                    print(f"[{datetime.now()}] [{Fore.RED}LIVE{Style.RESET_ALL}] {side_colored} {qty} | Key: {instrument_key} | Expiry: {expiry}")
                    return api.place_order(instrument_key, qty, side, tag=tag)

            # Check Global Entry Windows for LIVE
            can_enter_new_cycle = True
            current_time_str = now.strftime("%H:%M")
            if config.TRADING_MODE == 'LIVE' and config.STRICT_MONTHLY_EXPIRY_ENTRY:
                if getattr(config, 'OVERRIDE_TIMING_CHECKS', False):
                    can_enter_new_cycle = True
                elif not is_expiry_today:
                    can_enter_new_cycle = False
                elif current_time_str < config.ENTRY_TIME_HHMM:
                    can_enter_new_cycle = False

            # Compile Market Data
            # Fetch fresh positions for real-time reconciliation in strategies
            # Fetch fresh positions for real-time reconciliation in strategies
            broker_positions = None
            try:
                if config.TRADING_MODE == 'LIVE':
                    broker_positions = api.get_positions()
                else:
                    broker_positions = [] # Pure isolation for Paper Mode
            except:
                pass

            market_data = {
                'spot_price': spot_price,
                'now': now,
                'cw_chain': cw_chain_data,
                'nw_chain': nw_chain_data,
                'm_chain': m_chain_data,
                'quotes': quotes,
                'is_day_before_monthly_expiry': is_day_before_monthly_expiry,
                'is_expiry_today': is_expiry_today,
                'can_enter_new_cycle': can_enter_new_cycle,
                'can_adjust': can_adjust,
                'expiry_skipped': expiry_skipped,
                'greeks': greeks,
                'broker_positions': broker_positions,
                'monthly_expiry_trigger_date': effective_tomorrow if is_day_before_monthly_expiry else None
            }

            # C. Update All Strategies
            for strat in active_strategies:
                try:
                    strat.update(market_data, place_trade_callback)
                except Exception as e:
                    print(f"{Fore.RED}Error in Strategy {strat.name}: {e}{Style.RESET_ALL}")
            
            time.sleep(config.POLL_INTERVAL_SECONDS)
            
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Algo stopping manually...{Style.RESET_ALL}")
        # Option to exit all on manual stop could be added here

if __name__ == "__main__":
    main()
