"""
Antigravity MCP Bridge: Main MCP Server and Tool Implementations.
Exposes tools to orchestrate, monitor, wait, and terminate Antigravity (agy) tasks.
"""
import os
import sys
import json
import time
import subprocess
from typing import Dict, Any, Optional
from mcp.server.mcpserver import MCPServer

from agy_mcp_bridge.config import (
    find_agy_binary,
    DEFAULT_WORKSPACE,
    BRAIN_DIR,
    REGISTRY_FILE,
    TASK_LOGS_DIR,
    DEFAULT_TURNS_THRESHOLD,
    DEFAULT_SYNC_TIMEOUT,
    DEFAULT_PROXY_URL,
)
from agy_mcp_bridge.registry import (
    load_registry,
    get_project_key,
    get_session_for_project,
    register_project_session,
    clear_project_session,
    get_active_job,
    set_active_job,
    clear_active_job,
    is_pid_alive,
    terminate_process_group,
    send_desktop_notification,
)
from agy_mcp_bridge.telemetry import (
    gather_live_telemetry,
    format_job_running_report,
)
from agy_mcp_bridge.handoff import (
    resolve_session_and_handoff,
    retire_and_rotate_session,
)

server = MCPServer("Antigravity-Worker")


def wait_for_job(
    project_key: str,
    target_dir: str,
    job_info: Dict[str, Any],
    wait_seconds: int,
    proc: Optional[subprocess.Popen] = None,
    rotation_notice: Optional[str] = None
) -> str:
    """
    Synchronously wait for a background job up to wait_seconds.
    If finished: cleans active_job and returns full final result.
    If timeout reached: does NOT kill process; returns structured live telemetry snapshot.
    """
    start_wait = time.time()
    pid = job_info.get("pid", -1)
    stdout_file = job_info.get("stdout_file", "")
    stderr_file = job_info.get("stderr_file", "")
    instruction = job_info.get("instruction", "")
    conv_id = job_info.get("conversation_id")
    started_at = job_info.get("started_at", start_wait)

    while True:
        now = time.time()
        elapsed_wait = now - start_wait
        alive = is_pid_alive(pid, proc)

        if not alive:
            # Process terminated
            break

        if elapsed_wait >= wait_seconds:
            # Sync window limit reached - return snapshot without killing process!
            total_elapsed = int(now - started_at)
            send_desktop_notification(
                f"Antigravity: {project_key} Running in Background",
                f"Active for {total_elapsed}s (PID {pid}), live telemetry returned"
            )
            return format_job_running_report(
                project_key=project_key,
                workspace_path=target_dir,
                job_info=job_info,
                elapsed_seconds=total_elapsed
            )

        time.sleep(1.0)

    # Process has completed: harvest logs and update registry
    clear_active_job(project_key)

    stdout_text = ""
    stderr_text = ""
    if stdout_file and os.path.exists(stdout_file):
        try:
            with open(stdout_file, "r", encoding="utf-8", errors="replace") as f:
                stdout_text = f.read().strip()
        except Exception:
            pass

    if stderr_file and os.path.exists(stderr_file):
        try:
            with open(stderr_file, "r", encoding="utf-8", errors="replace") as f:
                stderr_text = f.read().strip()
        except Exception:
            pass

    new_conv_id = conv_id
    response_text = ""
    turns = 1

    if stdout_text:
        try:
            data = json.loads(stdout_text)
            new_conv_id = data.get("conversation_id", conv_id)
            response_text = data.get("response", "").strip()
            turns = data.get("num_turns", 1)
        except json.JSONDecodeError:
            response_text = stdout_text

    if not response_text and stderr_text:
        response_text = f"[CLI Notice/Stderr]\n{stderr_text}"
    elif not response_text:
        response_text = "Task finished with no text output."

    if new_conv_id:
        register_project_session(
            project_key=project_key,
            workspace_path=target_dir,
            conversation_id=new_conv_id,
            summary=instruction[:100],
            turns=turns
        )

    health_status = "HEALTHY"
    if turns >= DEFAULT_TURNS_THRESHOLD:
        health_status = f"WARNING_BLOATED ({turns}/{DEFAULT_TURNS_THRESHOLD} turns, will auto-rotate next call)"
    elif rotation_notice:
        health_status = "AUTO_ROTATED (Fresh session initialized from handoff)"

    footer = (
        f"\n\n---\n"
        f"[Task Registry & Session Health Telemetry]\n"
        f"• Project / Task: `{project_key}`\n"
        f"• Bound Session ID: `{new_conv_id}` (Turn: {turns})\n"
        f"• Session Health: {health_status}\n"
        f"• Workspace: {target_dir}\n"
    )
    if rotation_notice:
        footer += f"• Notice: {rotation_notice}\n"
    footer += f"(Note: You can continue by keeping project '{project_key}', or override explicitly with `conversation_id='{new_conv_id}'`.)"

    send_desktop_notification(
        f"Antigravity: {project_key} Completed",
        f"Session {new_conv_id} (Turn {turns}) finished successfully."
    )

    return response_text + footer


# --- MCP Tools ---

@server.tool(
    name="run_agy_task",
    description=(
        "Execute a task using Google Antigravity (agy) CLI in headless daemon mode.\n"
        "Features:\n"
        "1. Session Persistence: Automatically binds project sessions via Task Registry.\n"
        "2. Daemon Execution: Spawns in an independent process group; never SIGKILLs on RPC timeout.\n"
        "3. Live Snapshot: If task exceeds sync window (e.g. 1100s), returns live telemetry without interrupting background progress.\n"
        "4. Concurrency Protection: Automatically attaches to active running tasks to prevent SQLite lock collisions."
    )
)
def run_agy_task(
    instruction: str,
    project_name: str = "",
    workspace_path: str = "",
    conversation_id: str = "",
    reset_conversation: bool = False,
    mode: str = "accept-edits",
    safe_mode: bool = False,
    timeout_seconds: int = 1100
) -> str:
    """
    Run an agy task anchored to a specific project registry or explicit conversation ID.
    """
    target_dir = os.path.expanduser(workspace_path.strip()) if workspace_path.strip() else DEFAULT_WORKSPACE
    if not os.path.exists(target_dir):
        return f"Error: Workspace path does not exist: {target_dir}"

    actual_timeout = min(max(timeout_seconds, 60), DEFAULT_SYNC_TIMEOUT)
    agy_path = find_agy_binary()

    if not agy_path or not os.path.exists(agy_path):
        return f"Error: agy binary not found. Please install agy or set AGY_PATH environment variable."

    if mode not in ["accept-edits", "plan"]:
        mode = "accept-edits"

    project_key = get_project_key(project_name, target_dir)

    # Concurrency Protection: If an active job is already running for this project
    existing_job = get_active_job(project_key)
    if existing_job:
        existing_pid = existing_job.get("pid", -1)
        if is_pid_alive(existing_pid):
            if reset_conversation:
                sys.stderr.write(f"[run_agy_task] Aborting existing job (PID {existing_pid}) due to reset_conversation=True\n")
                sys.stderr.flush()
                terminate_process_group(existing_pid)
                clear_active_job(project_key)
            else:
                sys.stderr.write(
                    f"[run_agy_task] Job already running for '{project_key}' (PID {existing_pid}). Auto-attaching...\n"
                )
                sys.stderr.flush()
                return wait_for_job(
                    project_key=project_key,
                    target_dir=target_dir,
                    job_info=existing_job,
                    wait_seconds=actual_timeout
                )
        else:
            clear_active_job(project_key)

    effective_conv_id = None
    use_continue_flag = False
    rotation_notice = None
    handoff_seed = ""

    cid_clean = conversation_id.strip()

    if reset_conversation:
        curr_id = get_session_for_project(project_key)
        reg = load_registry()
        last_t = reg.get("projects", {}).get(project_key, {}).get("last_turns", 1)
        if curr_id:
            handoff_path, handoff_seed = retire_and_rotate_session(
                project_key=project_key,
                workspace_path=target_dir,
                old_conv_id=curr_id,
                old_turns=last_t,
                reason="Explicit reset requested with state handoff",
                last_summary=instruction[:100]
            )
            rotation_notice = f"Project session `{curr_id}` reset on request. Clean session initialized with handoff."
        else:
            clear_project_session(project_key)
    elif cid_clean.lower() in ["latest", "recent", "last"]:
        use_continue_flag = True
    else:
        effective_conv_id, rotation_notice, handoff_seed = resolve_session_and_handoff(
            project_key=project_key,
            workspace_path=target_dir,
            requested_cid=cid_clean
        )

    def _build_command(conv_override: Optional[str] = None, seed_override: str = "") -> list[str]:
        c = [
            agy_path,
            "--output-format", "json",
            "--mode", mode,
            "--add-dir", target_dir,
            "--dangerously-skip-permissions"
        ]
        if use_continue_flag:
            c.append("--continue")
        elif conv_override:
            c.extend(["--conversation", conv_override])

        eff_inst = (seed_override + instruction) if seed_override else instruction
        c.extend(["-p", eff_inst])
        return c

    cmd = _build_command(conv_override=effective_conv_id, seed_override=handoff_seed)

    # Subprocess environment configuration
    sub_env = os.environ.copy()
    if DEFAULT_PROXY_URL:
        sub_env["http_proxy"] = sub_env.get("http_proxy") or DEFAULT_PROXY_URL
        sub_env["https_proxy"] = sub_env.get("https_proxy") or DEFAULT_PROXY_URL
        sub_env["all_proxy"] = sub_env.get("all_proxy") or DEFAULT_PROXY_URL
        sub_env["HTTP_PROXY"] = sub_env["http_proxy"]
        sub_env["HTTPS_PROXY"] = sub_env["https_proxy"]
        sub_env["ALL_PROXY"] = sub_env["all_proxy"]
    sub_env["no_proxy"] = sub_env.get("no_proxy") or "localhost,127.0.0.1"
    sub_env["NO_PROXY"] = sub_env["no_proxy"]

    venv_bin = os.path.join(target_dir, ".venv", "bin")
    if os.path.exists(venv_bin):
        sub_env["PATH"] = f"{venv_bin}:{sub_env.get('PATH', '')}"
        sub_env["VIRTUAL_ENV"] = os.path.join(target_dir, ".venv")

    os.makedirs(TASK_LOGS_DIR, exist_ok=True)
    max_retries = 2

    for attempt in range(max_retries + 1):
        job_id = f"job_{project_key}_{int(time.time())}"
        stdout_file = os.path.join(TASK_LOGS_DIR, f"{job_id}.out")
        stderr_file = os.path.join(TASK_LOGS_DIR, f"{job_id}.err")

        stdout_f = open(stdout_file, "w", encoding="utf-8")
        stderr_f = open(stderr_file, "w", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            cwd=target_dir,
            stdout=stdout_f,
            stderr=stderr_f,
            env=sub_env,
            start_new_session=True
        )
        stdout_f.close()
        stderr_f.close()

        job_info = {
            "job_id": job_id,
            "pid": proc.pid,
            "stdout_file": stdout_file,
            "stderr_file": stderr_file,
            "started_at": time.time(),
            "started_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "instruction": instruction[:120],
            "conversation_id": effective_conv_id,
            "target_dir": target_dir,
            "mode": mode
        }
        set_active_job(project_key, job_info)

        # Initial probe: check if it crashes immediately on network handshake or quota
        probe_start = time.time()
        while time.time() - probe_start < 8.0:
            if not is_pid_alive(proc.pid, proc):
                break
            time.sleep(0.5)

        if not is_pid_alive(proc.pid, proc):
            stdout_txt = ""
            stderr_txt = ""
            if os.path.exists(stdout_file):
                try:
                    with open(stdout_file, "r", encoding="utf-8", errors="replace") as f:
                        stdout_txt = f.read().strip()
                except Exception:
                    pass
            if os.path.exists(stderr_file):
                try:
                    with open(stderr_file, "r", encoding="utf-8", errors="replace") as f:
                        stderr_txt = f.read().strip()
                except Exception:
                    pass

            combined = (stderr_txt + "\n" + stdout_txt).lower()

            is_quota_exhausted = (
                proc.returncode != 0
                and effective_conv_id is not None
                and (
                    "individual quota reached" in combined
                    or "resource_exhausted" in combined
                    or "code 429" in combined
                )
            )
            if is_quota_exhausted:
                sys.stderr.write(
                    f"[run_agy_task] 429 RESOURCE_EXHAUSTED on session {effective_conv_id}. Triggering Session Handoff & Rotation...\n"
                )
                sys.stderr.flush()
                reg = load_registry()
                p_last_t = reg.get("projects", {}).get(project_key, {}).get("last_turns", 100)
                handoff_path, handoff_seed = retire_and_rotate_session(
                    project_key=project_key,
                    workspace_path=target_dir,
                    old_conv_id=effective_conv_id,
                    old_turns=p_last_t,
                    reason="Context bloat triggered 429 RESOURCE_EXHAUSTED",
                    last_summary=instruction[:100]
                )
                rotation_notice = f"Session `{effective_conv_id}` exceeded context limit (429). Automatically rotated to clean session with handoff."
                effective_conv_id = None
                clear_active_job(project_key)

                cmd = _build_command(conv_override=None, seed_override=handoff_seed)
                continue

            is_transient_network_error = (
                proc.returncode != 0
                and (
                    "eligibility check failed" in combined
                    or "failed to get profile picture" in combined
                    or ("oauth" in combined and "eof" in combined)
                    or "tls: bad record mac" in combined
                    or ("streamgeneratecontent" in combined and any(k in combined for k in ["eof", "tls:", "reset by peer", "broken pipe", "bad record mac", "transport"]))
                    or "http2: server sent goaway" in combined
                    or "unexpected eof" in combined
                    or "connection reset by peer" in combined
                )
            )
            if is_transient_network_error and attempt < max_retries:
                backoff_delay = (attempt + 1) * 1.5
                sys.stderr.write(
                    f"[run_agy_task] Transient network/TLS error on attempt {attempt + 1}. Retrying in {backoff_delay:.1f}s...\n"
                )
                sys.stderr.flush()
                clear_active_job(project_key)
                time.sleep(backoff_delay)
                continue

            # Process finished quickly (< 8s) or hit unrecoverable error
            return wait_for_job(
                project_key=project_key,
                target_dir=target_dir,
                job_info=job_info,
                wait_seconds=2,
                proc=proc,
                rotation_notice=rotation_notice
            )

        # Process is alive after probe: enter main wait loop
        remaining_wait = actual_timeout - int(time.time() - probe_start)
        return wait_for_job(
            project_key=project_key,
            target_dir=target_dir,
            job_info=job_info,
            wait_seconds=max(remaining_wait, 10),
            proc=proc,
            rotation_notice=rotation_notice
        )

    return f"Error executing agy task: Exceeded maximum retry attempts."


@server.tool(
    name="wait_agy_task",
    description=(
        "Block and wait for an active background agy task to complete and return its final output.\n"
        "Use this tool when `run_agy_task` returned an intermediate progress snapshot because "
        "the task was still running, or when you want to block until the background job finishes.\n"
        "If the task finishes within `timeout_seconds`, returns the complete final result.\n"
        "If the task is still running when `timeout_seconds` is reached, safely returns another live telemetry snapshot without killing the process."
    )
)
def wait_agy_task(
    project_name: str = "",
    workspace_path: str = "",
    timeout_seconds: int = 1100
) -> str:
    """
    Wait for an active background job to complete.
    """
    target_dir = os.path.expanduser(workspace_path.strip()) if workspace_path.strip() else DEFAULT_WORKSPACE
    project_key = get_project_key(project_name, target_dir)

    job_info = get_active_job(project_key)
    if not job_info:
        return (
            f"No running background job found for project `{project_key}`.\n"
            f"The task may have already finished or was not started. Please check `get_task_registry()` or call `run_agy_task`."
        )

    actual_timeout = min(max(timeout_seconds, 60), DEFAULT_SYNC_TIMEOUT)
    return wait_for_job(
        project_key=project_key,
        target_dir=target_dir,
        job_info=job_info,
        wait_seconds=actual_timeout
    )


@server.tool(
    name="abort_agy_task",
    description=(
        "Explicitly abort and terminate a running background agy task for a project.\n"
        "Sends SIGTERM followed by SIGKILL to the process group, frees locks, and updates task registry."
    )
)
def abort_agy_task(project_name: str = "", workspace_path: str = "") -> str:
    """
    Explicitly cancel and terminate a background task for a project.
    """
    target_dir = os.path.expanduser(workspace_path.strip()) if workspace_path.strip() else DEFAULT_WORKSPACE
    project_key = get_project_key(project_name, target_dir)

    job_info = get_active_job(project_key)
    if not job_info:
        return f"No active background task found for project `{project_key}` to abort."

    pid = job_info.get("pid", -1)
    if is_pid_alive(pid):
        terminate_process_group(pid)
        clear_active_job(project_key)
        send_desktop_notification(
            f"Antigravity: {project_key} Aborted",
            f"Process PID {pid} was manually terminated."
        )
        return (
            f"🛑 [Task for project `{project_key}` successfully aborted]\n"
            f"• Terminated Process: PID {pid}\n"
            f"• All filesystem changes preserved in `{target_dir}`\n"
            f"• You can resume anytime by calling `run_agy_task`."
        )
    else:
        clear_active_job(project_key)
        return f"Task for project `{project_key}` (PID {pid}) already exited. Registry cleared."


@server.tool(
    name="get_task_registry",
    description="Inspect all tracked projects in the Task Registry with their bound agy session IDs, workspaces, and status."
)
def get_task_registry() -> str:
    """List all registered projects and their bound agy sessions."""
    reg = load_registry()
    projects = reg.get("projects", {})
    if not projects:
        return "Task Registry is currently empty. No projects registered yet."

    lines = ["### 📋 Supervisor Task Registry:\n"]
    for p_name, data in projects.items():
        cid = data.get("conversation_id", "N/A")
        ws = data.get("workspace_path", "N/A")
        turns = data.get("last_turns", 1)
        up_time = data.get("updated_at", "N/A")
        summary = data.get("last_summary", "")

        active_job = data.get("active_job")
        job_label = ""
        if active_job:
            apid = active_job.get("pid", -1)
            if is_pid_alive(apid):
                aelapsed = int(time.time() - active_job.get("started_at", time.time()))
                job_label = f" | 🟢 RUNNING (PID {apid}, {aelapsed}s)"

        retired = data.get("retired_sessions", {})
        retired_str = f" | 📦 Retired Sessions: {len(retired)}" if retired else ""
        health_label = "⚠️ BLOATED/NEEDS_ROTATION" if turns >= DEFAULT_TURNS_THRESHOLD else "✅ HEALTHY"

        lines.append(
            f"* **Project**: `{p_name}`\n"
            f"  * **Session ID**: `{cid}` (Turn: {turns} | Status: {health_label}{job_label}{retired_str})\n"
            f"  * **Workspace**: `{ws}`\n"
            f"  * **Last Updated**: {up_time}\n"
            f"  * **Last Summary**: {summary}\n"
        )
    return "\n".join(lines)


@server.tool(
    name="get_agy_status",
    description=(
        "Check Antigravity (agy) health, background job execution status, or real-time project progress.\n"
        "If project_name is provided, returns the live status (PID liveness, running duration, current step, git changes) "
        "of that project's background task.\n"
        "If project_name is empty, returns system health, agy path, and a summary of all active background jobs."
    )
)
def get_agy_status(project_name: str = "", workspace_path: str = "") -> str:
    """Check agy status, active registry, or real-time task progress."""
    target_dir = os.path.expanduser(workspace_path.strip()) if workspace_path.strip() else DEFAULT_WORKSPACE
    if project_name.strip():
        project_key = get_project_key(project_name, target_dir)
        job_info = get_active_job(project_key)
        if job_info:
            pid = job_info.get("pid", -1)
            alive = is_pid_alive(pid)
            elapsed = int(time.time() - job_info.get("started_at", time.time()))
            telem = gather_live_telemetry(
                job_info.get("conversation_id"),
                job_info.get("target_dir", target_dir),
                job_info.get("started_at", time.time())
            )
            state_str = "🟢 RUNNING" if alive else "⚪ EXITED"
            tool_summary = ", ".join(f"{k}: {v}" for k, v in telem["tool_counts"].items()) or "(None)"
            return (
                f"### 📊 Project `{project_key}` Background Task Status:\n"
                f"• Process State: {state_str} (PID: {pid})\n"
                f"• Elapsed Time: {elapsed}s (Started: {job_info.get('started_at_str', 'N/A')})\n"
                f"• Bound Session: `{telem['effective_id'] or 'N/A'}` (Current Step: {telem['total_steps']})\n"
                f"• Tool Invocations: {tool_summary}\n"
                f"• Workspace Git State:\n{telem['git_status_output']}\n"
                f"• Latest Agent Thought:\n> {telem['last_thought'][:200] or '(None)'}\n"
            )
        else:
            reg = load_registry()
            p_data = reg.get("projects", {}).get(project_key, {})
            if p_data:
                return (
                    f"### 📋 Project `{project_key}` Status:\n"
                    f"• No active background task running.\n"
                    f"• Bound Session ID: `{p_data.get('conversation_id', 'None')}` (Turns: {p_data.get('last_turns', 0)})\n"
                    f"• Workspace: `{p_data.get('workspace_path', 'N/A')}`\n"
                    f"• Last Updated: {p_data.get('updated_at', 'N/A')}\n"
                )
            else:
                return f"Project `{project_key}` is not tracked in registry."

    # General agy path & health check
    agy_path = find_agy_binary()
    if not agy_path or not os.path.exists(agy_path):
        return f"Status: Error - agy not found in PATH. Please set AGY_PATH."

    reg = load_registry()
    projects = reg.get("projects", {})
    running_jobs = []
    for pk, pinfo in projects.items():
        aj = pinfo.get("active_job")
        if aj and is_pid_alive(aj.get("pid", -1)):
            elapsed = int(time.time() - aj.get("started_at", time.time()))
            running_jobs.append(f"  * `{pk}`: PID {aj.get('pid')} (Running {elapsed}s)")

    running_str = "\n".join(running_jobs) if running_jobs else "  (No background tasks currently active)"

    try:
        res = subprocess.run(
            [agy_path, "--help"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if res.returncode == 0:
            return (
                f"Status: Ready\n"
                f"Path: {agy_path}\n"
                f"Default Workspace: {DEFAULT_WORKSPACE}\n"
                f"Registry File: {REGISTRY_FILE} ({len(projects)} projects tracked)\n"
                f"Active Background Jobs:\n{running_str}\n"
                f"Python Interpreter: {sys.executable}"
            )
        else:
            return f"Status: Degraded\nReturn code: {res.returncode}\nStderr: {res.stderr}"
    except Exception as e:
        return f"Status: Failed to reach agy - {str(e)}"


@server.tool(
    name="list_recent_agy_conversations",
    description="List raw physical agy conversation sessions from local brain directory (for audit or recovery)."
)
def list_recent_agy_conversations(limit: int = 5) -> str:
    """List recent physical agy sessions."""
    if not os.path.exists(BRAIN_DIR):
        return f"Notice: Brain directory not found at {BRAIN_DIR}"

    entries = []
    try:
        for name in os.listdir(BRAIN_DIR):
            p = os.path.join(BRAIN_DIR, name)
            if os.path.isdir(p) and not name.startswith("."):
                entries.append((os.path.getmtime(p), name, p))
    except Exception as e:
        return f"Error reading sessions: {str(e)}"

    if not entries:
        return "No local agy conversation history found."

    entries.sort(reverse=True)
    results = ["### Recent Physical Antigravity (agy) Sessions:\n"]

    for mtime, cid, path in entries[:limit]:
        dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        preview = "(No preview available)"
        tpath = os.path.join(path, ".system_generated", "logs", "transcript.jsonl")
        if os.path.exists(tpath):
            try:
                with open(tpath, "r", encoding="utf-8") as f:
                    first = json.loads(f.readline())
                    content = first.get("content", "")
                    if "<USER_REQUEST>" in content:
                        content = content.split("<USER_REQUEST>")[1].split("</USER_REQUEST>")[0].strip()
                    preview = content[:90].replace("\n", " ")
            except Exception:
                pass

        results.append(f"* **ID**: `{cid}`\n  * **Time**: {dt}\n  * **Summary**: {preview}\n")

    return "\n".join(results)
