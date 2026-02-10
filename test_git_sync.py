import git_utils
import os

# Create dummy state file
state_file = "test_live_state.json"
with open(state_file, "w") as f:
    f.write('{"test": "data"}')

# Create dummy non-state file
other_file = "test_other.txt"
with open(other_file, "w") as f:
    f.write("Should not be pushed")

print("--- Testing State File Push ---")
# This should attempt to push (might fail if remote not reachable/auth, but logic trace is what matters)
# We can't easily mock subprocess here without complex setups, so we rely on the function's own print output or return
# Since we updated git_utils to print/return, we'll see.
git_utils.sync_push(state_file)

print("\n--- Testing Non-State File Push ---")
git_utils.sync_push(other_file)

# Cleanup
import time
time.sleep(1)
try:
    os.remove(state_file)
    os.remove(other_file)
except:
    pass
