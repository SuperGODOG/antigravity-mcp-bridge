"""
Task Registry persistence, project session resolution, and process group lifecycle management.
"""
import os
import sys
import json
import time
import signal
import hashlib
import subprocess
from typing import Dict, Any, Optional

from agy_mcp_bridge.config import REGISTRY_FILE


def load_registry() -> Dict[str, Any]:
    """Load persistent project -> session registry safely."""
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"projects": {}}


def save_registry(registry: Dict[str, Any]):
    """Save persistent project -> session registry atomically."""
    os.makedirs(os.path.dirname(REGISTRY_FILE), exist_ok=True)
    temp_file = REGISTRY_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, REGISTRY_FILE)


def get_project_key(project_name: str, workspace_path: str) -> str:
    """Determine standardized project identifier with path collision safety."""
    if project_name.strip():
        return project_name.strip()
    norm = os.path.abspath(os.path.expanduser(workspace_path.strip()))
    base = os.path.basename(norm) or "workspace"
    path_hash = hashlib.md5(norm.encode("utf-8")).hexdigest()[:6]
    return f"{base}_{path_hash}"


def get_session_for_project(project_key: str) -> Optional[str]:
    """Query registry for a project's active conversation ID."""
    reg = load_registry()
    entry = reg.get("projects", {}).get(project_key)
    if entry and isinstance(entry, dict):
        return entry.get("conversation_id")
    return None


def register_project_session(
    project_key: str,
    workspace_path: str,
    conversation_id: str,
    summary: str = "",
    turns: int = 1,
    retired_sessions: Optional[Dict[str, Any]] = None
):
    """Update or register a project's active session while preserving retired aliases."""
    reg = load_registry()
    if "projects" not in reg:
        reg["projects"] = {}
    existing = reg["projects"].get(project_key, {})
    existing_retired = existing.get("retired_sessions", {})
    if retired_sessions:
        existing_retired.update(retired_sessions)

    reg["projects"][project_key] = {
        "conversation_id": conversation_id,
        "workspace_path": os.path.abspath(workspace_path),
        "last_turns": turns,
        "last_summary": summary[:120] if summary else "Active session",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "retired_sessions": existing_retired
    }
    save_registry(reg)


def clear_project_session(project_key: str):
    """Remove project session from registry."""
    reg = load_registry()
    if project_key in reg.get("projects", {}):
        del reg["projects"][project_key]
        save_registry(reg)


def get_active_job(project_key: str) -> Optional[Dict[str, Any]]:
    """Retrieve active background job details for a project if any."""
    reg = load_registry()
    entry = reg.get("projects", {}).get(project_key)
    if entry and isinstance(entry, dict):
        return entry.get("active_job")
    return None


def set_active_job(project_key: str, job_info: Dict[str, Any]):
    """Record an active background job in registry."""
    reg = load_registry()
    if "projects" not in reg:
        reg["projects"] = {}
    entry = reg["projects"].setdefault(project_key, {})
    entry["active_job"] = job_info
    save_registry(reg)


def clear_active_job(project_key: str):
    """Remove active job entry from registry for a project."""
    reg = load_registry()
    entry = reg.get("projects", {}).get(project_key)
    if entry and isinstance(entry, dict) and "active_job" in entry:
        del entry["active_job"]
        save_registry(reg)


def is_pid_alive(pid: int, proc: Optional[subprocess.Popen] = None) -> bool:
    """Check if process PID is currently alive and not a zombie."""
    if pid <= 0:
        return False
    if proc is not None:
        return proc.poll() is None
    try:
        res = subprocess.run(["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            state = res.stdout.strip()
            return bool(state) and not state.startswith("Z")
        return False
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def terminate_process_group(pid: int) -> bool:
    """Terminate the process group of a detached child process."""
    if pid <= 0:
        return False
    try:
        pgid = os.getpgid(pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        time.sleep(1.0)
        if is_pid_alive(pid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return True
    except ProcessLookupError:
        return True
    except Exception as e:
        sys.stderr.write(f"[terminate_process_group] Error terminating PID {pid}: {e}\n")
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except Exception:
            return False


def send_desktop_notification(title: str, message: str):
    """Deliver a native desktop notification on macOS / Linux where available."""
    if sys.platform == "darwin":
        try:
            clean_title = title.replace('"', '\\"').replace("'", "")
            clean_msg = message.replace('"', '\\"').replace("'", "")[:180]
            script = f'display notification "{clean_msg}" with title "{clean_title}"'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        try:
            subprocess.run(["notify-send", title, message[:180]], capture_output=True, timeout=5)
        except Exception:
            pass
