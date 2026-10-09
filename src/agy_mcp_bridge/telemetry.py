"""
Telemetry harvest engine: extracts real-time progress from transcript.jsonl,
parses tool call statistics, inspects Git repository state, and formats reports.
"""
import os
import json
import time
import subprocess
from typing import Dict, Any, Optional

from agy_mcp_bridge.config import BRAIN_DIR
from agy_mcp_bridge.registry import register_project_session


def find_recent_session_id(start_timestamp: float) -> Optional[str]:
    """Find the newest conversation ID created or modified around start_timestamp."""
    if not os.path.exists(BRAIN_DIR):
        return None
    try:
        entries = []
        for name in os.listdir(BRAIN_DIR):
            p = os.path.join(BRAIN_DIR, name)
            if os.path.isdir(p) and len(name) >= 32:
                mtime = os.path.getmtime(p)
                entries.append((mtime, name))
        if entries:
            entries.sort(reverse=True)
            newest_mtime, newest_id = entries[0]
            if newest_mtime >= (start_timestamp - 30):
                return newest_id
    except Exception:
        pass
    return None


def gather_live_telemetry(
    conv_id: Optional[str],
    workspace_path: str,
    start_timestamp: float
) -> Dict[str, Any]:
    """Extract real-time execution telemetry from transcript.jsonl and git tree."""
    effective_id = conv_id or find_recent_session_id(start_timestamp)
    brain_dir = os.path.join(BRAIN_DIR, effective_id) if (effective_id and os.path.exists(BRAIN_DIR)) else None
    transcript_path = os.path.join(brain_dir, ".system_generated", "logs", "transcript.jsonl") if brain_dir else None

    total_steps = 0
    tool_counts: Dict[str, int] = {}
    recent_actions: list[str] = []
    last_thought = ""

    if transcript_path and os.path.exists(transcript_path):
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                lines = [json.loads(line) for line in f if line.strip()]

            total_steps = len(lines)
            for l in lines:
                if l.get("source") == "MODEL":
                    if "thinking" in l and l["thinking"]:
                        last_thought = l["thinking"]
                    if "content" in l and l["content"]:
                        last_thought = l["content"]
                    if "tool_calls" in l:
                        for tc in l["tool_calls"]:
                            name = tc.get("name", "unknown")
                            tool_counts[name] = tool_counts.get(name, 0) + 1
                            args = tc.get("args", {})
                            detail = ""
                            if name == "run_command":
                                cmd = str(args.get("CommandLine", "")).strip("'\"\n ")
                                detail = f"`{cmd[:60]}`"
                            elif name in ["write_to_file", "replace_file_content", "view_file"]:
                                tgt = args.get("TargetFile") or args.get("AbsolutePath", "")
                                tgt = str(tgt).strip("'\"\n ")
                                detail = f"`{os.path.basename(tgt)}`"
                            recent_actions.append(f"{name}({detail})")
        except Exception:
            pass

    git_status_output = "  (Git status not available)"
    head_commit = ""
    if workspace_path and os.path.exists(workspace_path):
        try:
            res_git = subprocess.run(
                ["git", "status", "-s"],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                timeout=5
            )
            git_lines = res_git.stdout.strip().splitlines()
            if git_lines:
                preview = git_lines[:8]
                rem = len(git_lines) - len(preview)
                git_status_output = "\n".join(f"  {line}" for line in preview)
                if rem > 0:
                    git_status_output += f"\n  ...and {rem} additional changed files"
            else:
                git_status_output = "  (Working directory is clean)"

            res_head = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                timeout=3
            )
            head_commit = res_head.stdout.strip()
        except Exception:
            pass

    return {
        "effective_id": effective_id,
        "total_steps": total_steps,
        "tool_counts": tool_counts,
        "recent_actions": recent_actions,
        "last_thought": last_thought,
        "git_status_output": git_status_output,
        "head_commit": head_commit
    }


def format_job_running_report(
    project_key: str,
    workspace_path: str,
    job_info: Dict[str, Any],
    elapsed_seconds: int
) -> str:
    """Format structured intermediate telemetry snapshot when sync window expires without killing."""
    pid = job_info.get("pid", -1)
    started_at_str = job_info.get("started_at_str", "recent")
    conv_id = job_info.get("conversation_id")
    started_at = job_info.get("started_at", time.time())

    telem = gather_live_telemetry(conv_id, workspace_path, started_at)
    effective_id = telem["effective_id"] or conv_id
    total_steps = telem["total_steps"]
    tool_counts = telem["tool_counts"]
    recent_actions = telem["recent_actions"]
    last_thought = telem["last_thought"]
    git_status_output = telem["git_status_output"]
    head_commit = telem["head_commit"]

    if effective_id:
        try:
            summary = f"[Daemon Job Running] Step {total_steps}, {elapsed_seconds}s elapsed"
            register_project_session(
                project_key=project_key,
                workspace_path=workspace_path,
                conversation_id=effective_id,
                summary=summary,
                turns=1
            )
        except Exception:
            pass

    tool_summary_str = ", ".join(f"{k}: {v}" for k, v in tool_counts.items()) if tool_counts else "(No tool calls recorded yet)"
    recent_5_str = "\n".join(f"  - {act}" for act in recent_actions[-5:]) if recent_actions else "  - (No recent actions)"
    clean_thought = last_thought[:350].strip() if last_thought else "(No model thoughts recorded yet)"
    head_info = f" [Commit: `{head_commit}`]" if head_commit else ""

    report = f"""⏳ [AGY Task Running in Background (Elapsed: {elapsed_seconds}s | Process PID: {pid})]
The agent is actively executing a composite workflow (Completed Step {total_steps}, Total Tool Calls: {sum(tool_counts.values())}).
To prevent the client RPC timeout (e.g. 1200s limit), the bridge returned this intermediate progress snapshot.
The background daemon process is alive and healthy. **DO NOT ABORT OR RE-DISPATCH FROM SCRATCH.**

### 📊 Live Execution Telemetry:
• Process: PID `{pid}` (RUNNING, started at {started_at_str})
• Bound Session: `{effective_id or "Unknown"}` (Step {total_steps}, Total Tool Invocations: {sum(tool_counts.values())})
• Tool Usage Breakdown: {tool_summary_str}
• Recent Operations:
{recent_5_str}
• Workspace Git Status{head_info}:
{git_status_output}
• Latest Agent Thoughts:
> {clean_thought}

---
### 💡 Supervisor Next Actions:
1. **Continue Waiting for Completion (Recommended)**:
   Call `wait_agy_task(project_name="{project_key}")` to enter another wait window until final delivery.
2. **Instant Status Inspection**:
   Call `get_agy_status(project_name="{project_key}")` for a non-blocking snapshot.
3. **Explicit Cancellation (If Needed)**:
   Call `abort_agy_task(project_name="{project_key}")` to cleanly terminate the process group.
"""

    footer = f"""
---
[Task Registry & Session Health Telemetry]
• Project / Task: `{project_key}`
• Bound Session ID: `{effective_id or "N/A"}` (Turn: 1)
• Session Health: DAEMON_RUNNING (Step {total_steps}, PID {pid} active)
• Workspace: {workspace_path}
(Note: Call `wait_agy_task(project_name="{project_key}")` to resume waiting for completion.)
"""
    return report + footer


def salvage_timeout_progress(
    conv_id: Optional[str],
    project_key: str,
    workspace_path: str,
    timeout_seconds: int,
    start_timestamp: float
) -> str:
    """Salvage state, transcript history, and git status when a task hits execution limit."""
    telem = gather_live_telemetry(conv_id, workspace_path, start_timestamp)
    effective_id = telem["effective_id"]
    total_steps = telem["total_steps"]
    tool_counts = telem["tool_counts"]
    recent_actions = telem["recent_actions"]
    last_thought = telem["last_thought"]
    git_status_output = telem["git_status_output"]
    head_commit = telem["head_commit"]

    if effective_id:
        try:
            summary = f"[Timeout Salvaged] Completed {total_steps} steps before {timeout_seconds}s limit"
            register_project_session(
                project_key=project_key,
                workspace_path=workspace_path,
                conversation_id=effective_id,
                summary=summary,
                turns=1
            )
        except Exception:
            pass

    tool_summary_str = ", ".join(f"{k}: {v}" for k, v in tool_counts.items()) if tool_counts else "(No tool calls found)"
    recent_5_str = "\n".join(f"  - {act}" for act in recent_actions[-5:]) if recent_actions else "  - (No recent tool actions)"
    clean_thought = last_thought[:350].strip() if last_thought else "(No model thoughts recorded)"
    head_info = f" [Commit: `{head_commit}`]" if head_commit else ""

    report = f"""⚠️ [AGY Task Execution Window Elapsed ({timeout_seconds}s)]
The workflow exceeded the single-turn synchronous execution window.
All filesystem modifications, git commits, and transcript records have been salvaged and persisted.

### 📊 Salvaged Telemetry:
• Bound Session ID: `{effective_id or "Unknown"}` (Step {total_steps}, Total Tool Calls: {sum(tool_counts.values())})
• Tool Breakdown: {tool_summary_str}
• Recent 5 Actions:
{recent_5_str}
• Git Working Tree{head_info}:
{git_status_output}
• Model Thoughts:
> {clean_thought}

---
### 💡 Supervisor Resumption Advice:
1. **Do not re-issue the entire task from scratch!** Most file writes, edits, and tests are already committed to disk.
2. **Resume directly on the bound session:**
   Call `run_agy_task(project_name="{project_key}", conversation_id="{effective_id or ""}", instruction="Continue remaining wrap-up tasks based on current git status and modified files.")`
"""

    footer = f"""
---
[Task Registry & Session Health Telemetry]
• Project / Task: `{project_key}`
• Bound Session ID: `{effective_id or "N/A"}` (Turn: 1)
• Session Health: WARNING_TIMEOUT (Step {total_steps}, salvaged)
• Workspace: {workspace_path}
"""
    return report + footer
