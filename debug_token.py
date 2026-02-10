from instrument_manager import InstrumentMaster
import pandas as pd

m = InstrumentMaster()
m.load_master()

target_token = 'NSE_FO|40476'

print(f"Searching for {target_token}...")
if m.df is not None:
    row = m.df[m.df['instrument_key'] == target_token]
    if not row.empty:
        print("--- MATCH FOUND ---")
        print(row.iloc[0])
        print("-------------------")
    else:
        print("Token not found in Loaded DF.")
else:
    print("DF failed to load.")
