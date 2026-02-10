import git_utils
import config
import os
import csv
import time

def test_csv_sync():
    print("=== TESTING CSV LOG SYNC ===")
    
    # Enable Sync just in case
    config.USE_GIT_STATE_SYNC = True
    
    # Create a dummy trade log file that matches the pattern
    test_log_file = "trade_log_test_sync.csv"
    
    print(f"1. Creating {test_log_file}...")
    with open(test_log_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'instrument_key', 'side', 'qty', 'price', 'expiry', 'tag', 'pnl'])
        writer.writerow([time.time(), 'TEST_KEY', 'BUY', 50, 100.0, '2026-02-10', 'TEST_TAG', 0.0])
        
    print("2. Attempting Sync Push...")
    if git_utils.sync_push(test_log_file):
        print("PASS: Push Successful.")
    else:
        print("FAIL: Push Failed.")

    # Clean up (Optional, maybe keep to verify on remote manually if needed)
    if os.path.exists(test_log_file):
        os.remove(test_log_file)
        
if __name__ == "__main__":
    if not config.USE_GIT_STATE_SYNC:
        print("WARNING: Git Sync is DISABLED in config.")
    else:
        test_csv_sync()
