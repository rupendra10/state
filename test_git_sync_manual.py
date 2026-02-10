import git_utils
import config
import os
import json
import time

def test_sync():
    print("=== TESTING GIT SYNC ===")
    
    # 1. Test Pull
    print("1. Testing Sync Pull...")
    if git_utils.sync_pull():
        print("PASS: Pull Successful.")
    else:
        print("FAIL: Pull Failed.")
        
    # 2. Test Push (Dry Run effectively, we create a dummy state file)
    print("\n2. Testing Sync Push...")
    dummy_file = "test_sync_state.json"
    
    # Create valid JSON content
    with open(dummy_file, 'w') as f:
        json.dump({"test": "sync", "timestamp": time.time()}, f)
        
    # Attempt Push
    # Note: git_utils.sync_push restricts to files ending with _state.json
    # Our dummy file does NOT end with _live_state.json or _paper_state.json, 
    # so we expect it to SKIP or we need to name it correctly to test.
    # Let's use a name that passes the filter but isn't critical.
    test_state_file = "TestStrategy_paper_state.json"
    with open(test_state_file, 'w') as f:
        json.dump({"test": "sync", "timestamp": time.time()}, f)
    
    if git_utils.sync_push(test_state_file):
        print("PASS: Push Successful (or Skipped if no changes).")
    else:
        print("FAIL: Push Failed.")

    # Clean up
    if os.path.exists(test_state_file):
        os.remove(test_state_file)
        
if __name__ == "__main__":
    if not config.USE_GIT_STATE_SYNC:
        print("WARNING: Git Sync is DISABLED in config.")
    else:
        test_sync()
