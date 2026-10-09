"""
Configuration and environment resolution for Antigravity MCP Bridge.
Provides robust auto-detection with environment variable overrides.
"""
import os
import shutil
from pathlib import Path
from typing import Optional


def find_agy_binary() -> Optional[str]:
    """Auto-detect agy executable across common installation paths."""
    env_path = os.getenv("AGY_PATH")
    if env_path and os.path.exists(env_path):
        return os.path.abspath(env_path)

    which_path = shutil.which("agy")
    if which_path:
        return os.path.abspath(which_path)

    common_paths = [
        os.path.expanduser("~/.local/bin/agy"),
        "/usr/local/bin/agy",
        "/opt/homebrew/bin/agy",
        os.path.expanduser("~/bin/agy"),
    ]
    for p in common_paths:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return os.path.abspath(p)

    return None


# Base Data & Storage Paths
DATA_DIR = os.getenv("AGY_BRIDGE_DATA_DIR", os.path.expanduser("~/.agy-bridge"))
os.makedirs(DATA_DIR, exist_ok=True)

REGISTRY_FILE = os.getenv("AGY_REGISTRY_FILE", os.path.join(DATA_DIR, "task_registry.json"))
HANDOFF_DIR = os.getenv("AGY_HANDOFF_DIR", os.path.join(DATA_DIR, "handoffs"))
TASK_LOGS_DIR = os.getenv("AGY_TASK_LOGS_DIR", os.path.join(DATA_DIR, "task_logs"))

os.makedirs(HANDOFF_DIR, exist_ok=True)
os.makedirs(TASK_LOGS_DIR, exist_ok=True)

# AGY Physical Artifact Directories
BRAIN_DIR = os.getenv(
    "AGY_BRAIN_DIR",
    os.path.expanduser("~/.gemini/antigravity-cli/brain")
)

# Runtime Parameters
DEFAULT_WORKSPACE = os.getenv("AGY_DEFAULT_WORKSPACE", os.getcwd())
DEFAULT_TURNS_THRESHOLD = int(os.getenv("AGY_MAX_TURNS", "100"))
DEFAULT_SYNC_TIMEOUT = int(os.getenv("AGY_SYNC_TIMEOUT", "1100"))

# Proxy Resolution (Auto-inherits environment or optional fallback)
DEFAULT_PROXY_URL = os.getenv("AGY_PROXY_URL", "http://127.0.0.1:7897")
