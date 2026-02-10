from instrument_manager import InstrumentMaster
import pandas as pd
from datetime import date

def check_expiries():
    print("Initializing InstrumentMaster...")
    master = InstrumentMaster()
    master.load_master()
    
    symbol = 'NIFTY'
    print(f"Fetching expiries for {symbol}...")
    expiries = master.get_expiry_dates(symbol)
    
    print("\n--- FOUND EXPIRIES ---")
    for d in expiries:
        print(d)
        
    print("\n--- ANALYSIS ---")
    today = date.today() # Should be 2026-01-23
    print(f"Today: {today}")
    
    # Check for Jan Expiries
    jan_expiries = [d for d in expiries if d.year == 2026 and d.month == 1]
    print(f"Jan 2026 Expiries: {jan_expiries}")
    
    if jan_expiries:
        last_jan = jan_expiries[-1]
        print(f"Last expiry in Jan: {last_jan}")
        
        if last_jan == date(2026, 1, 27):
            print("CONFIRMED: Jan 27 is the last expiry of January.")
        elif last_jan == date(2026, 1, 29):
            print("CONFIRMED: Jan 29 is the last expiry of January.")
        else:
            print(f"Unknown last expiry: {last_jan}")

if __name__ == "__main__":
    check_expiries()
