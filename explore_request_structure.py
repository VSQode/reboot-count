#!/usr/bin/env python3
"""
Explore the internal structure of chatSessions JSONL request objects.
Shows: top-level keys, model fields, tool call structure, response shape.

Usage:
    python3 explore_request_structure.py <SESSION_ID> <WORKSPACE_HASH> [--req N]
"""
import sys
import json
import os
import re
from pathlib import Path

APPDATA = os.environ.get("APPDATA", "")
CHAT_SESSIONS_BASE = Path(APPDATA) / "Code - Insiders" / "User" / "workspaceStorage"


def load_jsonl(session_id, workspace_hash):
    jsonl_path = CHAT_SESSIONS_BASE / workspace_hash / "chatSessions" / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        print(f"ERROR: {jsonl_path} not found", file=sys.stderr)
        sys.exit(1)

    requests = {}
    with open(jsonl_path, "rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("kind", 0)
            value = rec.get("value", {})

            if kind == 0:
                # Snapshot — extract requests array
                reqs = value.get("requests", [])
                for r in reqs:
                    if r and isinstance(r, dict):
                        rid = r.get("requestId") or r.get("id")
                        if rid:
                            requests[rid] = r
            elif kind == 1:
                # Set-patch
                keys = rec.get("keys", [])
                if len(keys) >= 2 and keys[0] == "requests" and isinstance(keys[1], int):
                    idx = keys[1]
                    if len(keys) == 2:
                        # Replace entire request at index
                        if isinstance(value, dict):
                            rid = value.get("requestId") or value.get("id")
                            if rid:
                                requests[rid] = value
            elif kind == 2:
                # Array-replace
                keys = rec.get("keys", [])
                if isinstance(keys, list) and len(keys) == 1 and keys[0] == "requests":
                    # Top-level requests extension
                    if isinstance(value, list):
                        for r in value:
                            if r and isinstance(r, dict):
                                rid = r.get("requestId") or r.get("id")
                                if rid:
                                    requests[rid] = r

    # Sort by timestamp
    sorted_reqs = sorted(requests.values(), key=lambda r: r.get("timestamp", 0))
    return sorted_reqs


def print_keys_recursive(obj, prefix="", max_depth=3, depth=0):
    if depth >= max_depth:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            key_path = f"{prefix}.{k}" if prefix else k
            vtype = type(v).__name__
            if isinstance(v, (dict, list)):
                size = len(v)
                print(f"  {key_path}: {vtype}[{size}]")
                print_keys_recursive(v, key_path, max_depth, depth + 1)
            else:
                val_repr = repr(v)
                if len(val_repr) > 80:
                    val_repr = val_repr[:77] + "..."
                print(f"  {key_path}: {vtype} = {val_repr}")


def summarize_request(req, idx):
    print(f"\n{'='*70}")
    print(f"REQUEST #{idx} — {req.get('requestId', '?')}")
    print(f"{'='*70}")
    print_keys_recursive(req, max_depth=4)


def find_model_fields(req):
    """Recursively search for any key containing 'model'."""
    results = []
    def search(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}" if path else k
                if "model" in k.lower():
                    results.append((p, v))
                search(v, p)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                search(v, f"{path}[{i}]")
    search(req)
    return results


def find_tool_calls(req):
    """Find any tool call / tool result structures."""
    results = []
    def search(obj, path=""):
        if isinstance(obj, dict):
            name = obj.get("name") or obj.get("toolName") or obj.get("function", {}).get("name") if isinstance(obj.get("function"), dict) else None
            if name and any(k in obj for k in ("arguments", "input", "output", "result")):
                results.append((path, {"name": name, "keys": list(obj.keys())}))
            for k, v in obj.items():
                search(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                search(v, f"{path}[{i}]")
    search(req)
    return results


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <SESSION_ID> <WORKSPACE_HASH> [--req N]")
        sys.exit(1)

    session_id = sys.argv[1]
    workspace_hash = sys.argv[2]
    target_req = None

    for i, arg in enumerate(sys.argv[3:]):
        if arg == "--req" and i + 4 < len(sys.argv):
            target_req = int(sys.argv[i + 4])

    reqs = load_jsonl(session_id, workspace_hash)
    print(f"Loaded {len(reqs)} requests")

    # Show structure of requests 0, 1, and the last one
    indices = [0, 1, len(reqs) // 2, len(reqs) - 1] if target_req is None else [target_req]
    indices = sorted(set(indices))

    for idx in indices:
        if idx >= len(reqs):
            continue
        req = reqs[idx]
        summarize_request(req, idx)

        print("\n  --- MODEL FIELDS ---")
        model_fields = find_model_fields(req)
        if model_fields:
            for path, val in model_fields:
                print(f"    {path} = {repr(val)[:100]}")
        else:
            print("    (none found)")

        print("\n  --- TOOL CALLS ---")
        tool_calls = find_tool_calls(req)
        if tool_calls:
            for path, info in tool_calls[:5]:
                print(f"    {path}: name={info['name']!r}, keys={info['keys']}")
        else:
            print("    (none found)")

    print("\n\n" + "="*70)
    print("SCANNING ALL REQUESTS FOR MODEL FIELDS...")
    print("="*70)
    model_values = set()
    for req in reqs:
        for path, val in find_model_fields(req):
            if isinstance(val, str) and val:
                model_values.add(val)
    print("Unique model strings found:", sorted(model_values))

    print("\n" + "="*70)
    print("SCANNING ALL REQUESTS — TOP-LEVEL KEYS FREQUENCY...")
    print("="*70)
    from collections import Counter
    key_counts = Counter()
    for req in reqs:
        key_counts.update(req.keys())
    for k, count in key_counts.most_common():
        print(f"  {k}: {count}")

    print("\n" + "="*70)
    print("SEARCHING FOR TOOL CALL PATTERNS IN FIRST 10 REQUESTS...")
    print("="*70)
    for idx, req in enumerate(reqs[:10]):
        tc = find_tool_calls(req)
        if tc:
            print(f"\nRequest #{idx} has {len(tc)} tool call patterns:")
            for path, info in tc[:3]:
                print(f"  {path}: {info}")


if __name__ == "__main__":
    main()
