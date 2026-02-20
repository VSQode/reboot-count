#!/usr/bin/env python3
"""
correlate_edits_to_patches.py — Map every file edit/create to the patch number it happened in.

Data sources:
  1. chatSessions/{SESSION_ID}.jsonl   — request timestamps + reboot markers
  2. chatEditingSessions/{SESSION_ID}/state.json — operations (create/textEdit) with requestId

Algorithm:
  1. Read chatSessions JSONL → build {requestId: timestamp} map
  2. Read ground-truth reboot times from chatSessions (same as count_reboots_ground_truth.py)
  3. For each operation in chatEditingSessions: look up timestamp, find which patch window
  4. Output grouped by patch: list of file paths + operation types

Usage:
  python3 correlate_edits_to_patches.py <SESSION_ID> <WORKSPACE_HASH> [--brief]
  python3 correlate_edits_to_patches.py <SESSION_ID> <WORKSPACE_HASH> --patch N

  --brief       Show only file counts per patch, not full file list
  --patch N     Show only entries for patch N
"""
import sys
import json
import copy
import hashlib
import datetime
import os
import argparse
from pathlib import Path
from collections import defaultdict

COMPLETED_MARKERS = {
    "Summarized conversation history",
    "Compacted conversation",
}


def find_paths(session_id: str, workspace_hash: str):
    appdata = os.environ.get("APPDATA", "")
    base = Path(appdata) / "Code - Insiders" / "User" / "workspaceStorage" / workspace_hash
    jsonl = base / "chatSessions" / f"{session_id}.jsonl"
    editing = base / "chatEditingSessions" / session_id / "state.json"
    if not jsonl.exists():
        raise FileNotFoundError(f"chatSessions JSONL not found: {jsonl}")
    if not editing.exists():
        raise FileNotFoundError(f"chatEditingSessions state.json not found: {editing}")
    return jsonl, editing


def load_requests(jsonl_path: Path):
    """Load and resolve all requests from JSONL. Returns {req_idx: req_dict}.
    
    Handles three cases:
    - kind=0 snapshot: initial full state (requests array)
    - kind=1 patches: field-level updates to existing requests
    - kind=2 patches with keys=['requests']: top-level array extensions (new requests)
    - kind=2 patches with keys=['requests', N, 'response']: response part appends
    """
    lines = jsonl_path.read_bytes().decode("utf-8", "replace").splitlines()
    snap = json.loads(lines[0])
    reqs_raw = snap.get("v", {}).get("requests", [])

    request_data = {i: copy.deepcopy(r) for i, r in enumerate(reqs_raw) if r is not None}
    next_idx = len(reqs_raw)

    for raw in lines[1:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = obj.get("kind")
        keys = obj.get("k", [])
        val = obj.get("v")

        # Top-level requests array extension (new requests appended after snapshot)
        if kind == 2 and keys == ["requests"] and isinstance(val, list):
            for r in val:
                if isinstance(r, dict):
                    request_data[next_idx] = r
                    next_idx += 1
            continue

        if kind not in (1, 2):
            continue
        if len(keys) >= 3 and keys[0] == "requests" and isinstance(keys[1], int):
            req_idx = keys[1]
            field = keys[2]
            if req_idx not in request_data:
                request_data[req_idx] = {}
            if kind == 1:
                request_data[req_idx][field] = val
            elif kind == 2 and field == "response":
                existing = request_data[req_idx].get("response", [])
                if isinstance(val, list):
                    existing.extend(val)
                    request_data[req_idx]["response"] = existing

    return request_data


def get_compaction_marker(req):
    for part in (req or {}).get("response", []):
        if not isinstance(part, dict):
            continue
        if "progressTask" not in str(part.get("kind", "")):
            continue
        content = part.get("content", {})
        if isinstance(content, dict):
            content = content.get("value", "")
        if isinstance(content, str) and content in COMPLETED_MARKERS:
            return content
    return None


def get_summary_text(req):
    result = (req or {}).get("result", {})
    if not isinstance(result, dict):
        return None
    meta = result.get("metadata", {})
    if not isinstance(meta, dict):
        return None
    summ = meta.get("summary", {})
    if not isinstance(summ, dict):
        return None
    return summ.get("text", None)


def get_request_id(req):
    return (req or {}).get("requestId") or (req or {}).get("id")


def get_timestamp_ms(req):
    for key in ("timestamp", "requestStartTime"):
        ts = (req or {}).get(key)
        if ts is not None:
            return int(ts)
    return None


def fmt_ts(ms):
    if ms is None:
        return "?"
    try:
        dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ms)


def build_reboot_windows(request_data):
    """
    Returns list of (patch_number, start_ms, end_ms, summary_hash).
    patch_number: 1-indexed, matches count_reboots_ground_truth.py output.
    start_ms: timestamp of the compaction request (exclusive — edits AT this req are in this patch).
    end_ms: timestamp of the next compaction request (or None for the current patch).
    """
    compactions = []
    prev_hash = None

    for idx in sorted(request_data.keys()):
        req = request_data[idx]
        marker = get_compaction_marker(req)
        if not marker:
            continue
        summary = get_summary_text(req)
        h = hashlib.md5(summary.encode()).hexdigest()[:10] if summary else "NO_SUMMARY"
        if h != prev_hash:
            ts = get_timestamp_ms(req)
            compactions.append({"patch": len(compactions) + 1, "ts_ms": ts, "req_idx": idx, "hash": h})
            prev_hash = h

    # Build windows: patch N spans from compactions[N-1].ts_ms to compactions[N].ts_ms
    windows = []
    for i, c in enumerate(compactions):
        end_ms = compactions[i + 1]["ts_ms"] if i + 1 < len(compactions) else None
        windows.append({
            "patch": c["patch"],
            "start_ms": c["ts_ms"],
            "end_ms": end_ms,
            "hash": c["hash"],
        })

    # Patch 0 = before first reboot
    first_start = compactions[0]["ts_ms"] if compactions else None
    windows.insert(0, {
        "patch": 0,
        "start_ms": None,
        "end_ms": first_start,
        "hash": None,
    })

    return windows


def ts_to_patch(ts_ms, windows):
    """Return the patch number for a given timestamp."""
    if ts_ms is None:
        return -1  # unknown
    for w in reversed(windows):
        start = w["start_ms"]
        end = w["end_ms"]
        if start is None:
            # patch 0: before first reboot
            if end is None or ts_ms < end:
                return 0
        else:
            if ts_ms >= start and (end is None or ts_ms < end):
                return w["patch"]
    return -1


def load_editing_ops(editing_path: Path):
    """Load chatEditingSessions state.json operations. Returns list of op dicts."""
    raw = editing_path.read_bytes().decode("utf-8", "replace")
    d = json.loads(raw)
    return d.get("timeline", {}).get("operations", [])


def main():
    parser = argparse.ArgumentParser(description="Correlate file edits to patch numbers")
    parser.add_argument("session_id")
    parser.add_argument("workspace_hash")
    parser.add_argument("--brief", action="store_true", help="Show counts per patch only")
    parser.add_argument("--patch", type=int, default=None, help="Show only this patch number")
    args = parser.parse_args()

    jsonl_path, editing_path = find_paths(args.session_id, args.workspace_hash)

    print(f"Loading chatSessions JSONL...", flush=True)
    request_data = load_requests(jsonl_path)
    print(f"  {len(request_data)} requests resolved")

    # Build requestId → timestamp map
    req_id_to_ts = {}
    for idx, req in request_data.items():
        rid = get_request_id(req)
        ts = get_timestamp_ms(req)
        if rid and ts:
            req_id_to_ts[rid] = ts

    print(f"  {len(req_id_to_ts)} requestId->timestamp mappings")

    windows = build_reboot_windows(request_data)
    print(f"  {len(windows) - 1} reboot windows (patches 1–{len(windows)-1})")

    print(f"\nLoading chatEditingSessions operations...", flush=True)
    ops = load_editing_ops(editing_path)
    print(f"  {len(ops)} operations")

    # Group operations by patch
    by_patch = defaultdict(list)
    unknown_ts = 0

    for op in ops:
        op_type = op.get("type", "?")
        uri = op.get("uri", {})
        path = uri.get("fsPath") or uri.get("path") or "?"
        rid = op.get("requestId", "")
        ts_ms = req_id_to_ts.get(rid)
        if ts_ms is None:
            unknown_ts += 1
        patch = ts_to_patch(ts_ms, windows)
        by_patch[patch].append({
            "type": op_type,
            "path": path,
            "ts": fmt_ts(ts_ms),
            "requestId": rid,
        })

    if unknown_ts:
        print(f"  {unknown_ts} ops with unresolvable timestamp (requestId not in chatSessions)")

    # Print results
    patches_to_show = sorted(by_patch.keys())
    if args.patch is not None:
        patches_to_show = [p for p in patches_to_show if p == args.patch]

    print()
    for patch in patches_to_show:
        entries = by_patch[patch]
        label = f"patch {patch}" if patch >= 0 else "unknown patch"
        ws = next((w for w in windows if w["patch"] == patch), None)
        ts_range = ""
        if ws:
            ts_range = f"  [{fmt_ts(ws['start_ms'])} -> {fmt_ts(ws['end_ms'])}]"
        print(f"{'='*70}")
        print(f"PATCH {patch:>3}  ({len(entries)} ops){ts_range}")
        print(f"{'='*70}")

        if not args.brief:
            # Deduplicate: show unique (type, path) pairs in order of first appearance
            seen = set()
            for e in entries:
                key = (e["type"], e["path"])
                if key not in seen:
                    seen.add(key)
                    short_path = e["path"].replace("c:\\www\\VGM9\\", "").replace("/c/www/VGM9/", "")
                    print(f"  {e['type']:10}  {e['ts']:23}  {short_path}")
        else:
            # Brief: count by type
            from collections import Counter
            counts = Counter(e["type"] for e in entries)
            unique_files = len(set(e["path"] for e in entries))
            print(f"  unique files: {unique_files}  ops: {dict(counts)}")
        print()


if __name__ == "__main__":
    main()
