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


def is_background_call(invocation_msg: str, past_tense: str, result_details: object) -> bool:
    """Heuristic: is this a background terminal call?"""
    # Background terminals have pastTenseMessage like "Started background terminal"
    if past_tense and "background" in past_tense.lower():
        return True
    if invocation_msg and "background" in invocation_msg.lower():
        return True
    # If resultDetails has an 'id' field but no 'output' field, likely background
    if isinstance(result_details, dict):
        if "id" in result_details and "output" not in result_details:
            return True
    return False


def extract_terminal_id(result_details: object) -> Optional[str]:
    """Extract terminal ID from resultDetails."""
    if isinstance(result_details, dict):
        if "id" in result_details:
            return str(result_details["id"])
        # Sometimes returned as the value directly
        if "terminalId" in result_details:
            return str(result_details["terminalId"])
    # Background terminal result might be a plain string ID
    if isinstance(result_details, str) and len(result_details) > 8:
        return result_details
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
            inv_msg = part.get("invocationMessage", {})
            if isinstance(inv_msg, dict):
                inv_text = inv_msg.get("value", "")
            else:
                inv_text = str(inv_msg)

            past = part.get("pastTenseMessage", "")
            if isinstance(past, dict):
                past = past.get("value", "")

            result_details = part.get("resultDetails")
            command = ""
            if isinstance(result_details, dict):
                command = result_details.get("input", "")
            if not command:
                # Try to extract from invocation message
                command = inv_text

            is_bg = is_background_call(inv_text, past, result_details)
            term_id = extract_terminal_id(result_details)
            is_complete = part.get("isComplete", False)

            calls.append(TerminalCall(
                req_idx=req_idx,
                tool_call_id=tool_call_id,
                command=command[:120],
                is_background=is_bg,
                terminal_id=term_id,
                past_tense_msg=past[:80] if past else "",
                result_raw=result_details,
                is_complete=is_complete,
            ))
    return calls


def print_table(calls: list[TerminalCall], bg_only: bool = False):
    filtered = [c for c in calls if not bg_only or c.is_background]
    print(f"\n{'REQ':>4}  {'BG':>3}  {'COMPLETE':>8}  {'TERM_ID':>36}  COMMAND")
    print("-" * 120)
    for c in filtered:
        bg_flag = "BG" if c.is_background else "  "
        done_flag = "done" if c.is_complete else "open?"
        tid = c.terminal_id or "(no id)"
        print(f"{c.req_idx:>4}  {bg_flag}  {done_flag:>8}  {tid:>36}  {c.command[:60]}")

    print(f"\nTotal terminal calls: {len(calls)}")
    bg = [c for c in calls if c.is_background]
    print(f"  Background: {len(bg)}")
    print(f"  Foreground: {len(calls) - len(bg)}")
    with_id = [c for c in bg if c.terminal_id]
    print(f"  Background with recoverable ID: {len(with_id)}")
    open_bg = [c for c in bg if not c.is_complete]
    print(f"  Background possibly still open: {len(open_bg)}")


def print_ids_only(calls: list[TerminalCall]):
    """Emit just the terminal IDs for piping."""
    for c in calls:
        if c.is_background and c.terminal_id:
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
