#!/usr/bin/env python3
"""
probe_terminal_format.py — Inspect actual invocationMessage/pastTenseMessage format
for run_in_terminal calls in a session JSONL.

Usage: python3 probe_terminal_format.py <SESSION_ID> <WORKSPACE_HASH>
"""
import sys
import json
import os
from pathlib import Path


def load_all_requests(session_id, workspace_hash):
    appdata = os.environ.get("APPDATA", "")
    p = Path(appdata) / "Code - Insiders" / "User" / "workspaceStorage" / workspace_hash / "chatSessions" / f"{session_id}.jsonl"
    lines = p.read_bytes().decode("utf-8", "replace").splitlines()
    requests = {}

    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        k = d.get("k", d.get("kind"))
        v = d.get("v", d.get("value"))

        if k == 0 and isinstance(v, dict):
            for idx, req in enumerate(v.get("requests", [])):
                if req:
                    requests[idx] = req
        elif isinstance(k, list) and k == ["requests"] and isinstance(v, list):
            base = max(requests.keys(), default=-1) + 1
            for i, req in enumerate(v):
                if req:
                    requests[base + i] = req
        elif isinstance(k, list) and len(k) >= 2 and k[0] == "requests" and isinstance(k[1], int):
            idx = k[1]
            if idx not in requests:
                requests[idx] = {}
            if len(k) == 2 and isinstance(v, dict):
                requests[idx].update(v)
            elif len(k) == 3:
                requests[idx][k[2]] = v

    return [requests[i] for i in sorted(requests.keys())]


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <SESSION_ID> <WORKSPACE_HASH>")
        sys.exit(1)

    session_id, workspace_hash = sys.argv[1], sys.argv[2]
    requests = load_all_requests(session_id, workspace_hash)
    print(f"Loaded {len(requests)} requests")

    count = 0
    bg_count = 0
    for req in requests:
        if not req:
            continue
        for part in req.get("response", []):
            if not isinstance(part, dict):
                continue
            if part.get("kind") != "toolInvocationSerialized":
                continue
            if part.get("toolId") != "run_in_terminal":
                continue

            count += 1
            inv = part.get("invocationMessage", {})
            past = part.get("pastTenseMessage", "")
            rd = part.get("resultDetails")
            complete = part.get("isComplete", False)

            inv_val = inv.get("value", "") if isinstance(inv, dict) else str(inv)
            past_val = past.get("value", "") if isinstance(past, dict) else str(past)

            is_bg = False
            if "background" in inv_val.lower() or "background" in past_val.lower():
                is_bg = True

            if count <= 5 or (is_bg and bg_count < 3):
                print(f"\n--- terminal call #{count} ---")
                print(f"  inv.value  = {repr(inv_val[:120])}")
                print(f"  pastTense  = {repr(past_val[:120])}")
                print(f"  isComplete = {complete}")
                print(f"  resultDetails type = {type(rd).__name__}")
                if isinstance(rd, dict):
                    for k2, v2 in rd.items():
                        print(f"    rd.{k2} = {repr(str(v2)[:80])}")
                else:
                    print(f"  resultDetails = {repr(rd)[:100]}")
                if is_bg:
                    bg_count += 1
                    print(f"  *** BACKGROUND CALL #{bg_count} ***")

    print(f"\nTotal run_in_terminal: {count}")
    print(f"Background (by keyword): {bg_count}")


if __name__ == "__main__":
    main()
