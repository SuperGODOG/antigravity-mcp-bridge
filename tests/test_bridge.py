"""
Unit and integration tests for Antigravity MCP Bridge.
"""
import os
import sys
import time
import subprocess

# Ensure src/ is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from agy_mcp_bridge.config import find_agy_binary, DATA_DIR, REGISTRY_FILE
from agy_mcp_bridge.registry import (
    load_registry,
    save_registry,
    get_project_key,
    register_project_session,
    get_session_for_project,
    get_active_job,
    set_active_job,
    clear_active_job,
    is_pid_alive,
    terminate_process_group,
)
from agy_mcp_bridge.server import server


def test_config_resolution():
    print("[TEST] Checking config resolution...")
    assert DATA_DIR is not None
    assert REGISTRY_FILE is not None
    agy_bin = find_agy_binary()
    print(f"  -> Discovered agy binary: {agy_bin}")
    print("[PASS] Config resolution OK")


def test_registry_operations():
    print("[TEST] Testing registry operations...")
    project_key = "__test_project_unit__"
    test_job = {
        "job_id": "test_job_1",
        "pid": os.getpid(),
        "stdout_file": "/tmp/test.out",
        "stderr_file": "/tmp/test.err",
        "started_at": time.time(),
        "started_at_str": "2026-10-09 12:00:00",
        "instruction": "Test unit instruction",
        "conversation_id": "test-cid-123",
        "target_dir": "/tmp",
        "mode": "accept-edits"
    }

    set_active_job(project_key, test_job)
    fetched = get_active_job(project_key)
    assert fetched is not None
    assert fetched["job_id"] == "test_job_1"
    assert fetched["pid"] == os.getpid()

    clear_active_job(project_key)
    assert get_active_job(project_key) is None

    # Clean up test entry
    reg = load_registry()
    if project_key in reg.get("projects", {}):
        del reg["projects"][project_key]
        save_registry(reg)
    print("[PASS] Registry operations OK")


def test_process_lifecycle():
    print("[TEST] Testing process lifecycle...")
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(9999999) is False

    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    assert is_pid_alive(proc.pid, proc) is True

    term_ok = terminate_process_group(proc.pid)
    assert term_ok is True
    time.sleep(0.5)
    assert is_pid_alive(proc.pid) is False
    print("[PASS] Process lifecycle OK")


def test_mcp_tool_registration():
    print("[TEST] Testing MCP tool registrations...")
    tools = [t.name for t in server._tool_manager.list_tools()]
    expected_tools = [
        "run_agy_task",
        "wait_agy_task",
        "abort_agy_task",
        "get_task_registry",
        "get_agy_status",
        "list_recent_agy_conversations"
    ]
    for exp in expected_tools:
        assert exp in tools, f"Missing tool: {exp}"
    print(f"  -> Successfully registered {len(tools)} tools: {tools}")
    print("[PASS] MCP tool registration OK")


if __name__ == "__main__":
    test_config_resolution()
    test_registry_operations()
    test_process_lifecycle()
    test_mcp_tool_registration()
    print("\n✨ ALL ANTIGRAVITY MCP BRIDGE TESTS PASSED! ✨\n")
