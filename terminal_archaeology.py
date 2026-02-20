#!/usr/bin/env python3
"""
terminal_archaeology.py — Excavate background terminal IDs from a session JSONL.

Reads the chatSessions JSONL for a given session and reports:
  - All run_in_terminal calls
  - Whether each was background (by pastTenseMessage pattern)
  - Terminal ID from resultDetails (if shell integration captured it)
  - Whether the terminal is likely still open (no completion signal in JSONL)

Usage:
    python3 terminal_archaeology.py <SESSION_ID> <WORKSPACE_HASH> [--bg-only] [--ids-only]

Output is a table; --ids-only emits just the terminal ID list for piping to
a follow-up `get_terminal_output` check.

Author: POLARIS3 0.0.32
Date:   2026-02-20
"""

import sys
import json
import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TerminalCall:
    req_idx: int
    tool_call_id: str
    command: str
    is_background: bool
    terminal_id: Optional[str] = None
    past_tense_msg: str = ""
    result_raw: object = None
    is_complete: bool = False


def load_requests(session_id: str, workspace_hash: str) -> list:
    appdata = os.environ.get("APPDATA", "")
    p = Path(appdata) / "Code - Insiders" / "User" / "workspaceStorage" / workspace_hash / "chatSessions" / f"{session_id}.jsonl"
    if not p.exists():
        print(f"ERROR: {p} not found", file=sys.stderr)
        sys.exit(1)

    lines = p.read_bytes().decode("utf-8", "replace").splitlines()
    requests: dict[int, dict] = {}

    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        k = d.get("k", d.get("kind"))
        v = d.get("v", d.get("value"))

        # kind=0: snapshot
        if k == 0 and isinstance(v, dict):
            for idx, req in enumerate(v.get("requests", [])):
                if req:
                    requests[idx] = req

        # kind=2 list: new requests appended after snapshot
        elif isinstance(k, list) and k == ["requests"] and isinstance(v, list):
            base = max(requests.keys(), default=-1) + 1
            for i, req in enumerate(v):
                if req:
                    requests[base + i] = req

        # kind=1/2 mutations on existing requests
        elif isinstance(k, list) and len(k) >= 2 and k[0] == "requests" and isinstance(k[1], int):
            idx = k[1]
            if idx not in requests:
                requests[idx] = {}
            if len(k) == 2 and isinstance(v, dict):
                requests[idx].update(v)
            elif len(k) == 3:
                requests[idx][k[2]] = v

    return [requests[i] for i in sorted(requests.keys())]


def extract_file_url(msg: str) -> str:
    """Extract a file:/// URL and decode it to a Windows path."""
    m = re.search(r'file:///([^\s\)]+)', msg)
    if not m:
        return msg
    path = m.group(1)
    from urllib.parse import unquote
    path = unquote(path).replace("/", "\\")
    # Handle drive letter: c%3A -> c:
    if len(path) >= 2 and path[1] == ":":
        pass
    elif len(path) >= 3 and path[0].isalpha() and path[2] == ":":
        path = path[1:]  # strip leading backslash before drive letter
    return path


def is_background_call(result_details: object) -> bool:
    """
    Heuristic: background terminal calls have NULL resultDetails.
    Foreground calls with shell integration active have rd.input + rd.output.

    ARCHITECTURAL REALITY (discovered 2026-02-20):
    - invocationMessage.value is ALWAYS empty in JSONL (not stored)
    - pastTenseMessage is empty for shell-integration-active calls
    - resultDetails is only present for FOREGROUND calls with shell integration
    - Background terminal IDs are NOT persisted in JSONL at all
    - This means background terminals cannot be reliably detected or ID'd from JSONL
    """
    # If rd has input/output, it's a foreground call with shell integration
    if isinstance(result_details, dict) and "input" in result_details:
        return False
    # If rd is None, could be background OR foreground without shell integration
    # We cannot distinguish these from JSONL alone
    return result_details is None


def extract_terminal_id(result_details: object) -> Optional[str]:
    """
    Background terminal IDs are NOT stored in JSONL.
    This function is a placeholder — it will always return None.
    To get live terminal IDs, use hermes/pywinauto or VS Code extension API.
    """
    if isinstance(result_details, dict):
        if "id" in result_details:
            return str(result_details["id"])
        if "terminalId" in result_details:
            return str(result_details["terminalId"])
    return None


def mine_terminal_calls(requests: list) -> list[TerminalCall]:
    calls = []
    for req_idx, req in enumerate(requests):
        if not req:
            continue
        for part in req.get("response", []):
            if not isinstance(part, dict):
                continue
            if part.get("kind") != "toolInvocationSerialized":
                continue
            if part.get("toolId") not in ("run_in_terminal", "copilot_runInTerminal"):
                continue

            tool_call_id = part.get("toolCallId", "")
            result_details = part.get("resultDetails")

            # Command text only available via rd.input when shell integration active
            command = ""
            if isinstance(result_details, dict):
                command = result_details.get("input", "")

            is_bg = is_background_call(result_details)
            term_id = extract_terminal_id(result_details)
            is_complete = part.get("isComplete", False)

            calls.append(TerminalCall(
                req_idx=req_idx,
                tool_call_id=tool_call_id,
                command=command[:120],
                is_background=is_bg,
                terminal_id=term_id,
                past_tense_msg="",
                result_raw=result_details,
                is_complete=is_complete,
            ))
    return calls


def print_table(calls: list[TerminalCall], bg_only: bool = False):
    si_calls = [c for c in calls if not c.is_background]  # shell-integration-active
    no_si_calls = [c for c in calls if c.is_background]   # null resultDetails (either bg or no-SI)

    if not bg_only:
        print(f"\n=== SHELL-INTEGRATION CALLS (command text recoverable): {len(si_calls)} ===")
        print(f"{'REQ':>4}  {'ERR':>3}  {'COMPLETE':>8}  COMMAND")
        print("-" * 100)
        for c in si_calls[-20:]:  # last 20 most recent
            rd = c.result_raw
            is_err = rd.get("isError", False) if isinstance(rd, dict) else False
            err_flag = "ERR" if is_err else "   "
            done_flag = "done" if c.is_complete else "open?"
            print(f"{c.req_idx:>4}  {err_flag}  {done_flag:>8}  {c.command[:80]}")

    print(f"\n=== ARCHAEOLOGICAL SUMMARY ===")
    print(f"Total terminal calls:              {len(calls)}")
    print(f"With shell integration (SI active): {len(si_calls)}")
    print(f"Without SI (bg or no-SI):          {len(no_si_calls)}")
    print(f"")
    print(f"LIMITATION: Background terminal IDs are NOT stored in JSONL.")
    print(f"  IDs are only in the agent's working memory during the request.")
    print(f"  To enumerate LIVE open terminals, use hermes + pywinauto or VS Code API.")
    print(f"  To recover IDs from this session, VS Code shell integration must have been active.")


def print_ids_only(calls: list[TerminalCall]):
    """Emit just the terminal IDs for piping — only available for SI-active foreground calls."""
    for c in calls:
        if c.terminal_id:
            print(c.terminal_id)


def print_resultdetails_sample(calls: list[TerminalCall], n: int = 5):
    """Print raw resultDetails for the first N background calls — diagnostic."""
    print("\n=== resultDetails samples (background calls) ===")
    count = 0
    for c in calls:
        if c.is_background:
            print(f"  req[{c.req_idx}] isComplete={c.is_complete}")
            print(f"    result_raw = {repr(c.result_raw)[:200]}")
            print(f"    past = {c.past_tense_msg!r}")
            count += 1
            if count >= n:
                break
    if count == 0:
        print("  (no background calls found)")


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <SESSION_ID> <WORKSPACE_HASH> [--bg-only] [--ids-only] [--samples]")
        sys.exit(1)

    session_id = sys.argv[1]
    workspace_hash = sys.argv[2]
    bg_only = "--bg-only" in sys.argv
    ids_only = "--ids-only" in sys.argv
    samples = "--samples" in sys.argv

    requests = load_requests(session_id, workspace_hash)
    print(f"Loaded {len(requests)} requests from session {session_id[:8]}...")

    calls = mine_terminal_calls(requests)

    if ids_only:
        print_ids_only(calls)
    elif samples:
        print_resultdetails_sample(calls, n=10)
    else:
        print_table(calls, bg_only=bg_only)


if __name__ == "__main__":
    main()
