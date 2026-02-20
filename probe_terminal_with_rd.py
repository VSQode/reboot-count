#!/usr/bin/env python3
"""
probe_terminal_with_rd.py — Show terminal calls WHERE resultDetails is NOT None.
These are the shell-integration-active calls that have command text + output.

Usage: python3 probe_terminal_with_rd.py <SESSION_ID> <WORKSPACE_HASH>
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

    requests = load_all_requests(sys.argv[1], sys.argv[2])
    print(f"Loaded {len(requests)} requests")

    total = 0
    with_rd = 0
    shown = 0

    for req_idx, req in enumerate(requests):
        if not req:
            continue
        for part in req.get("response", []):
            if not isinstance(part, dict):
                continue
            if part.get("kind") != "toolInvocationSerialized":
                continue
            if part.get("toolId") != "run_in_terminal":
                continue
            total += 1
            rd = part.get("resultDetails")
            if rd is None:
                continue
            with_rd += 1
            inv = part.get("invocationMessage", {})
            inv_val = inv.get("value", "") if isinstance(inv, dict) else str(inv)
            past = part.get("pastTenseMessage", "")
            past_val = past.get("value", "") if isinstance(past, dict) else str(past)

            if shown < 10:
                shown += 1
                print(f"\n--- WITH resultDetails #{with_rd} (req[{req_idx}]) ---")
                print(f"  inv.value  = {repr(inv_val[:80])}")
                print(f"  pastTense  = {repr(past_val[:100])}")
                if isinstance(rd, dict):
                    for k2, v2 in rd.items():
                        v_repr = repr(v2)[:120] if not isinstance(v2, list) else f"[{len(v2)} items] {repr(v2[0])[:80] if v2 else ''}"
                        print(f"    rd.{k2} = {v_repr}")
                else:
                    print(f"  rd = {repr(rd)[:200]}")

    print(f"\nTotal run_in_terminal: {total}")
    print(f"With resultDetails: {with_rd}")
    print(f"Without: {total - with_rd}")


if __name__ == "__main__":
    main()
