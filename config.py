import os
from dotenv import load_dotenv

# Load variables from .env file if it exists
load_dotenv()

# ==========================================
# GLOABL TRADING CONFIGURATION
# ==========================================

# Select Mode: 'PAPER', 'LIVE', 'BACKTEST'
# 'PAPER': Executes in simulation mode (prints logs, no real money).
# 'LIVE': Executes REAL orders on Upstox.
# 'LIVE': Executes REAL orders on Upstox.
# 'BACKTEST': Replays historical data from CSV files.
TRADING_MODE = 'LIVE' 
ACTIVE_STRATEGIES = [  'CalendarPEWeekly', 'BatmanStrategy'] #CalendarPEWeekly, WeeklyIronfly, BatmanStrategy
AUTO_SYNC_ON_STARTUP = True
OVERRIDE_TIMING_CHECKS = True

# --- MANUAL PNL ADJUSTMENT ---
# Use this to align Algo PnL with Broker PnL (e.g. if history is missing or across sessions)
MANUAL_PNL_OFFSET = -6450.0

# --- GIT STATE SYNC CONFIG ---
USE_GIT_STATE_SYNC = True  # Set to True to enable Git-based state synchronization
GIT_REMOTE_NAME = "origin"
GIT_BRANCH_NAME = "main"
GIT_COMMIT_MESSAGE = "Update strategy state"

# ==========================================
# BACKTEST CONFIGURATION
# ==========================================
HISTORICAL_DATA_DIR = './historical_data'
BACKTEST_START_DATE = '2025-10-01'
BACKTEST_END_DATE = '2025-10-31'
# Expected CSV Filenames: 'nifty_spot.csv', 'nifty_options.csv'

# ==========================================
# API CREDENTIALS
# ==========================================
# By default, reads from Environment Variables.
UPSTOX_API_KEY = os.getenv('UPSTOX_API_KEY', '')
UPSTOX_API_SECRET = os.getenv('UPSTOX_API_SECRET', '')
UPSTOX_REDIRECT_URI = os.getenv('UPSTOX_REDIRECT_URI', '') # Must match your Upstox App settings
UPSTOX_ACCESS_TOKEN = os.getenv('UPSTOX_ACCESS_TOKEN', '')

# Gap Protection
GAP_PROTECTION_ENABLED = True
OPENING_VOLATILITY_WINDOW_MINS = 5 # 9:15 to 9:20
GAP_EMERGENCY_EXIT_PNL_PCT = 0.8  # Exit if loss > 80% of MAX_LOSS_VALUE during opening window
GAP_FORCED_ROLL_THRESHOLD_PCT = 1.25 # Roll to ATM if gap > 1.25% at open

# Rollover Protection
ROLLOVER_ON_T1_ENABLED = True
EARLY_ROLLOVER_TIME = "15:00" # Time HH:MM to roll on day before expiry

# ==========================================
# STRATEGY PARAMETERS
# ==========================================
UNDERLYING_NAME = 'NIFTY'
SPOT_INSTRUMENT_KEY = 'NSE_INDEX|Nifty 50'
RISK_FREE_RATE = 0.07 # 7% used for Greeks

# ENTRY LOGIC
ENTRY_WEEKLY_DELTA_TARGET = 0.50  # Sell Weekly ATM
ENTRY_MONTHLY_DELTA_TARGET = 0.50 # [IGNORED/DUMMY] Monthly Leg now strictly selects Round-100 ATM Strike.

# ENTRY TIMING
# If True, the algo will only enter new positions at 3:15 PM on Monthly Expiry Day.
STRICT_MONTHLY_EXPIRY_ENTRY = True
ENTRY_TIME_HHMM = "15:15"

# ADJUSTMENT LOGIC - WEEKLY (SHORT LEG)
WEEKLY_ADJ_TRIGGER_DELTA = 0.80       # Roll if delta >= this (ITM/Market Fall)
WEEKLY_ADJ_TRIGGER_DELTA_LOW = 0.10   # Roll if delta <= this (OTM/Market Rise)
WEEKLY_ROLL_TARGET_DELTA_FALL = 0.50  # Roll to ATM during Fall (Max Premium)
WEEKLY_ROLL_TARGET_DELTA_RISE = 0.45  # Roll to slightly OTM during Rise (Trend Safety)

# ADJUSTMENT LOGIC - MONTHLY (LONG LEG)
MONTHLY_ADJ_TRIGGER_DELTA = 0.80      # Roll if delta >= this (Deep ITM/Market Fall)
MONTHLY_ADJ_TRIGGER_DELTA_LOW = 0.10  # Roll if delta <= this (OTM/Market Rise)
MONTHLY_ROLL_TARGET_DELTA_FALL = 0.50 # [IGNORED/DUMMY] Uses Round-100 ATM Strike.
MONTHLY_ROLL_TARGET_DELTA_RISE = 0.35 # Target 0.35 Delta (Round 100) for Market Rise.

# ==========================================
# WEEKLY IRONFLY (PUT BUTTERFLY) PARAMETERS
# ==========================================
IRONFLY_CAPITAL = 180000        # Potential total capital (used for SL/Target calc)
IRONFLY_SL_PERCENT = 0.01       # 1% adjustment/exit trigger
IRONFLY_TARGET_PERCENT = 0.03   # 3% target (can be adjusted to 5% for higher risk/reward)
IRONFLY_ENTRY_WEEKDAY = 1       # 1 = Tuesday
IRONFLY_ENTRY_TIME = "12:00"
IRONFLY_EXIT_TIME = "15:00"     # On Expiry Day

IRONFLY_LEG1_OFFSET = -50       # Buy Put strike relative to ATM
IRONFLY_LEG2_OFFSET = -250      # Sell 2 Puts strike relative to ATM
IRONFLY_LEG3_OFFSET = -450      # Buy Put strike (hedge) relative to ATM
IRONFLY_ADJ_INWARD_OFFSET = 100 # Internal offset from Leg 1 for Call Calendar

# ==========================================
# BATMAN STRATEGY PARAMETERS
# ==========================================
BATMAN_ENTRY_WEEKDAY = 2       # Wednesday (Entry on Start of New Week)
BATMAN_ENTRY_TIME = "10:00"
BATMAN_MIN_VIX = 11.0
BATMAN_MAX_VIX = 20.0 # Avoid if VIX > 20
BATMAN_WING_DIST = 100
BATMAN_CORE_DIST = 200
BATMAN_HEDGE_DELTA = 0.05
BATMAN_ADJ_TRIGGER_COMBINED_DELTA = 0.40 # Trigger adjustment if combined sold delta < this (Winning side)
BATMAN_ADJ_TARGET_COMBINED_DELTA = 0.70  # Target combined delta for new sold legs (0.35 * 2)
BATMAN_MAX_ADJUSTMENTS = 3
BATMAN_EXIT_TIME = "15:00"     # Exit Time on T-1 (Day Before Expiry)

# ==========================================
# EXECUTION SETTINGS
# ==========================================
# --- RISK MANAGEMENT ---
MAX_LOSS_VALUE = 20000      # Exit all if loss exceeds this INR value. Set to 0 to DISABLE.
MAX_ALLOWED_VIX = 25.0     # Don't enter if VIX is above this (High Risk)
MIN_REQUIRED_CASH = 50000 # Minimum free cash buffer required to run
ROLLOVER_WEEKDAY = 0       # 0=Monday, 4=Friday (Friday is safer for gaps)
AUTO_EXIT_BEFORE_MONTHLY_EXPIRY_3PM = True # Exit everything at 3 PM ONE DAY BEFORE Monthly Expiry
POLL_INTERVAL_SECONDS = 30
ORDER_QUANTITY = 65 # 1 Lot for Nifty
ORDER_PRODUCT = 'D' # Delivery (D) or Intraday (I)
ORDER_VALIDITY = 'DAY'
ORDER_TAG_PREFIX = 'algo'

# ==========================================
# SYSTEM / PATHS
# ==========================================
DATA_DIR = './data'
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
PREFER_ROUND_STRIKES = True

# HOLIDAY CALENDAR (YYYY-MM-DD)
# Add known NSE holidays here to ensure correct T-1 logic
NSE_HOLIDAYS = [
    '2026-01-26', # Republic Day
    '2026-03-07', # Mahashivratri (Est)
    '2026-03-24', # Holi (Est)
    '2026-04-14', # Ambedkar Jayanti
    '2026-04-20', # Eid (Est)
    '2026-05-01', # Maharashtra Day
    '2026-08-15', # Independence Day
    '2026-10-02', # Gandhi Jayanti
    '2026-10-21', # Dussehra (Est)
    '2026-11-09', # Diwali (Est)
    '2026-12-25'  # Christmas
]
