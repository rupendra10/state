import subprocess
import os
import config
import shutil

_git_available = None

def _is_git_installed():
    global _git_available
    if _git_available is None:
        _git_available = shutil.which("git") is not None
        if not _git_available:
            print("[GIT SYNC] Warning: 'git' command not found in PATH. State synchronization disabled.")
    return _git_available

def sync_pull():
    """
    Performs a git pull --rebase to fetch latest state changes from the remote.
    """
    if not config.USE_GIT_STATE_SYNC or not _is_git_installed():
        return True
        
    try:
        # 1. Fetch latest
        subprocess.run(["git", "fetch", config.GIT_REMOTE_NAME], check=True, capture_output=True, timeout=30, shell=True)
        # 2. Pull rebase to avoid merge commits for simple JSON sync
        result = subprocess.run(
            ["git", "pull", "--rebase", config.GIT_REMOTE_NAME, config.GIT_BRANCH_NAME],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            shell=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[GIT SYNC] Pull failed: {e.stderr}")
        return False
    except Exception as e:
        print(f"[GIT SYNC] Error during pull: {e}")
        return False

def sync_push(file_path):
    """
    Commits and pushes a specific file to the remote repository.
    """
    if not config.USE_GIT_STATE_SYNC or not _is_git_installed():
        return True
        
    if not os.path.exists(file_path):
        return False
        
    # Safety Check: strict restriction to state files only
    if not file_path.endswith("_live_state.json") and not file_path.endswith("_state.json"):
        # print(f"[GIT SYNC] Skipping push for non-state file: {file_path}")
        return True

        
    try:
        # 1. Add file (Check existence again to be safe against race conditions)
        if os.path.exists(file_path):
            subprocess.run(["git", "add", file_path], check=True, capture_output=True, timeout=10, shell=True)
        else:
            return False
        
        # 2. Check if there are changes to commit
        status = subprocess.run(["git", "status", "--porcelain", file_path], check=True, capture_output=True, text=True, shell=True)
        if not status.stdout.strip():
            # No changes to commit
            return True
            
        # 3. Commit
        commit_msg = f"{config.GIT_COMMIT_MESSAGE}: {os.path.basename(file_path)}"
        subprocess.run(["git", "commit", "-m", commit_msg], check=True, capture_output=True, timeout=10, shell=True)
        
        # 4. Push
        subprocess.run(["git", "push", config.GIT_REMOTE_NAME, config.GIT_BRANCH_NAME], check=True, capture_output=True, timeout=30, shell=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[GIT SYNC] Push failed for {file_path}: {e.stderr}")
        return False
    except Exception as e:
        print(f"[GIT SYNC] Error during push for {file_path}: {e}")
        return False
