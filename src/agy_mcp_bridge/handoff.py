"""
Automated zero-LLM session handoff and context rotation engine.
Generates authoritative Markdown handoff documents when context limits are reached (429 or turn bloat).
"""
import os
import time
import subprocess
from typing import Tuple, Optional, Dict, Any

from agy_mcp_bridge.config import HANDOFF_DIR, DEFAULT_TURNS_THRESHOLD
from agy_mcp_bridge.registry import load_registry, save_registry


def create_session_handoff(
    project_key: str,
    workspace_path: str,
    old_conv_id: str,
    old_turns: int,
    reason: str,
    last_summary: str = ""
) -> Tuple[str, str]:
    """
    Generate an authoritative, zero-LLM markdown handoff document from the workspace.
    Returns: (handoff_filepath, handoff_content_string)
    """
    os.makedirs(HANDOFF_DIR, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    file_ts = time.strftime("%Y%m%d_%H%M%S")
    handoff_filename = f"handoff_{project_key}_{old_conv_id[:8]}_{file_ts}.md"
    handoff_path = os.path.join(HANDOFF_DIR, handoff_filename)

    git_branch = "unknown"
    git_status_lines = []
    git_recent_commits = []
    try:
        b_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=5
        )
        if b_res.returncode == 0:
            git_branch = b_res.stdout.strip()
        s_res = subprocess.run(
            ["git", "status", "-s"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=5
        )
        if s_res.returncode == 0:
            git_status_lines = [l for l in s_res.stdout.strip().splitlines() if l][:20]
        l_res = subprocess.run(
            ["git", "log", "-n", "3", "--oneline"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=5
        )
        if l_res.returncode == 0:
            git_recent_commits = [l for l in l_res.stdout.strip().splitlines() if l]
    except Exception:
        pass

    # Generic workspace artifact discovery
    common_artifacts = [
        "README.md",
        "TODO.md",
        "ACCEPTANCE.md",
        "CHANGELOG.md",
        "ARCHITECTURE.md",
        "docs/ARCHITECTURE.md",
        "docs/PROGRESS.md"
    ]
    artifacts_found = []
    for rel_path in common_artifacts:
        full_p = os.path.join(workspace_path, rel_path)
        if os.path.exists(full_p):
            sz_kb = os.path.getsize(full_p) / 1024
            artifacts_found.append(f"- `{rel_path}` ({sz_kb:.1f} KB)")

    content = f"""# Project Session Handoff: {project_key}
- **Timestamp**: {timestamp}
- **Predecessor Session ID**: `{old_conv_id}`
- **Turns Completed**: {old_turns}
- **Handoff Trigger**: {reason}
- **Workspace**: `{workspace_path}`

---

## 1. Executive Summary & Objective
- **Last Verified Work**: {last_summary or "Active project development in progress."}
- **Continuity Contract**: The predecessor session reached its health threshold ({old_turns} turns). All codebase edits, test suites, and documentation have been persisted directly to disk in the workspace. The successor session takes over directly from the physical repo state without repeating passed experiments.

## 2. Workspace Physical State
- **Git Branch**: `{git_branch}`
- **Recent Commits**:
{chr(10).join(f"  - {c}" for c in git_recent_commits) if git_recent_commits else "  - (No commit telemetry)"}
- **Working Tree Status ({len(git_status_lines)} modified items)**:
{chr(10).join(f"  - `{s}`" for s in git_status_lines) if git_status_lines else "  - (Clean working directory)"}

## 3. Discovered Workspace Artifacts
{chr(10).join(artifacts_found) if artifacts_found else "- (No standard summary documents found)"}

## 4. Operational Invariants for Successor Session
1. **Rely on Disk Evidence**: Inspect local files (`src/`, `tests/`, `docs/`) directly; do not re-run passed milestones.
2. **Preserve Accounting & Ledger**: Read from persistent files in workspace.
3. **Fail-Closed Governance**: Obey bounded execution and zero-leakage rules established by the project.
"""
    try:
        with open(handoff_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass

    return handoff_path, content


def retire_and_rotate_session(
    project_key: str,
    workspace_path: str,
    old_conv_id: str,
    old_turns: int,
    reason: str,
    last_summary: str = ""
) -> Tuple[str, str]:
    """Retire an old session, generate handoff, and record pointer alias in registry."""
    handoff_path, handoff_content = create_session_handoff(
        project_key=project_key,
        workspace_path=workspace_path,
        old_conv_id=old_conv_id,
        old_turns=old_turns,
        reason=reason,
        last_summary=last_summary
    )

    reg = load_registry()
    if "projects" not in reg:
        reg["projects"] = {}
    p_entry = reg["projects"].setdefault(project_key, {})
    retired = p_entry.setdefault("retired_sessions", {})
    retired[old_conv_id] = {
        "retired_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "turns": old_turns,
        "reason": reason,
        "handoff_path": handoff_path
    }
    # Clear conversation_id so next invocation auto-spawns a fresh session
    p_entry["conversation_id"] = None
    p_entry["last_turns"] = 0
    p_entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_registry(reg)

    handoff_seed = (
        f"[System Handoff Context from Predecessor Session {old_conv_id[:8]}... ({old_turns} turns)]\n"
        f"Predecessor session was rotated to prevent context bloat ({reason}).\n"
        f"Verified workspace state has been handed off to this clean session.\n"
        f"Handoff Summary:\n"
        f"{handoff_content[:2000]}\n"
        f"[End Handoff Context]\n\n"
    )
    return handoff_path, handoff_seed


def resolve_session_and_handoff(
    project_key: str,
    workspace_path: str,
    requested_cid: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolves session ID, checking if requested ID has been retired or exceeds turn threshold.
    Returns: (effective_conv_id, rotation_notice, handoff_seed_prompt)
    """
    reg = load_registry()
    p_entry = reg.get("projects", {}).get(project_key, {})
    current_conv = p_entry.get("conversation_id")
    last_turns = p_entry.get("last_turns", 1)
    retired_map = p_entry.get("retired_sessions", {})

    cid = requested_cid.strip()
    target_cid = cid if cid else current_conv

    if not target_cid or target_cid.lower() in ["latest", "recent", "last"]:
        return None, None, None

    # Check 1: Has this session already been retired?
    if target_cid in retired_map:
        successor_id = current_conv
        handoff_path = retired_map[target_cid].get("handoff_path", "")
        notice = f"Requested session `{target_cid}` was retired. Transparently forwarded to active successor `{successor_id}`."
        handoff_seed = ""
        if handoff_path and os.path.exists(handoff_path):
            try:
                with open(handoff_path, "r", encoding="utf-8") as f:
                    handoff_seed = f"[Handoff Context from {target_cid[:8]}...]\n{f.read()[:2000]}\n[End Handoff]\n\n"
            except Exception:
                pass
        return successor_id, notice, handoff_seed

    # Check 2: Has this session reached the health turn threshold?
    if target_cid == current_conv and last_turns >= DEFAULT_TURNS_THRESHOLD:
        handoff_path, handoff_seed = retire_and_rotate_session(
            project_key=project_key,
            workspace_path=workspace_path,
            old_conv_id=target_cid,
            old_turns=last_turns,
            reason=f"Session reached turn threshold ({last_turns}/{DEFAULT_TURNS_THRESHOLD} turns)"
        )
        notice = f"Session `{target_cid}` reached {last_turns} turns. Automatically rotated and handed off to clean session."
        return None, notice, handoff_seed

    return target_cid, None, None
