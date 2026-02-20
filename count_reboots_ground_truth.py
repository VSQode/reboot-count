#!/usr/bin/env python3
"""
count_reboots_ground_truth.py — Reference implementation for reboot counting.

A TRUE REBOOT = a completed compaction event that produced a distinct
conversation-summary text (result.metadata.summary.text).

Algorithm:
  1. Load the session JSONL (ObjectMutationLog format)
  2. Rebuild each request object by applying kind=1/kind=2 patches to snapshot
  3. For each request with a completed-compaction marker in its response:
     - Extract result.metadata.summary.text
     - Compute MD5 hash of the summary text
  4. Walk the list; count TRANSITIONS (new hash != previous hash)
  5. Report the full list and the true count

Usage:
  python3 count_reboots_ground_truth.py <SESSION_ID> <WORKSPACE_HASH>

  SESSION_ID     : The chat session GUID
  WORKSPACE_HASH : The VS Code workspaceStorage folder hash

AppData path constructed automatically from the two arguments:
  %APPDATA%\\Code - Insiders\\User\\workspaceStorage\\{WORKSPACE_HASH}\\chatSessions\\{SESSION_ID}.jsonl
"""
import sys
import json
import copy
import hashlib
import os
from pathlib import Path

COMPLETED_MARKERS = {
    "Summarized conversation history",  # pre-Feb 2026
    "Compacted conversation",           # Feb 2026+
}

def find_jsonl(session_id: str, workspace_hash: str) -> Path:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        raise RuntimeError("APPDATA environment variable not set")
    base = Path(appdata) / "Code - Insiders" / "User" / "workspaceStorage"
    path = base / workspace_hash / "chatSessions" / f"{session_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"JSONL not found: {path}")
    return path

def load_and_resolve(jsonl_path: Path) -> dict:
    """Load snapshot and apply all kind=1/kind=2 patches. Returns {req_idx: request_dict}."""
    lines = jsonl_path.read_bytes().decode("utf-8", "replace").splitlines()
    snap = json.loads(lines[0])
    reqs_raw = snap.get("v", {}).get("requests", [])
    print(f"Snapshot: {len(reqs_raw)} requests | JSONL: {len(lines)} lines\n")

    request_data = {i: copy.deepcopy(r) for i, r in enumerate(reqs_raw) if r is not None}

    for raw in lines[1:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = obj.get("kind")
        if kind not in (1, 2):
            continue
        keys = obj.get("k", [])
        val = obj.get("v")
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

def get_compaction_marker(req: dict) -> str | None:
    """Return the completed-compaction marker string, or None."""
    for part in (req or {}).get("response", []):
        if not isinstance(part, dict):
            continue
        kind_str = str(part.get("kind", ""))
        if "progressTask" not in kind_str:
            continue
        content = part.get("content", {})
        if isinstance(content, dict):
            content = content.get("value", "")
        if isinstance(content, str) and content in COMPLETED_MARKERS:
            return content
    return None

def get_summary_text(req: dict) -> str | None:
    """Return result.metadata.summary.text, or None."""
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

def count_reboots(session_id: str, workspace_hash: str) -> int:
    jsonl_path = find_jsonl(session_id, workspace_hash)
    request_data = load_and_resolve(jsonl_path)

    compaction_events = []
    for idx in sorted(request_data.keys()):
        req = request_data[idx]
        marker = get_compaction_marker(req)
        if marker:
            summary = get_summary_text(req)
            compaction_events.append((idx, marker, summary))

    print(f"Completed compaction events: {len(compaction_events)}\n")

    col_w = 30
    print(f"{'req_idx':>7}  {'marker':{col_w}}  {'summary_hash':12}  {'REBOOT?':8}  summary_preview")
    print("-" * 110)

    true_reboots = []
    prev_hash = None
    no_summary = []

    for idx, marker, summary in compaction_events:
        if summary is not None:
            h = hashlib.md5(summary.encode()).hexdigest()[:10]
            preview = summary[:60].replace("\n", "↵")
        else:
            h = "NO_SUMMARY"
            preview = "(no result.metadata.summary)"
            no_summary.append((idx, marker))

        is_new = (h != prev_hash)
        label = "✅ NEW " if is_new else "  dup "

        if is_new:
            true_reboots.append({
                "req_idx": idx,
                "marker": marker,
                "summary_hash": h,
                "summary_len": len(summary) if summary else 0,
                "summary_head": (summary or "")[:200],
            })

        print(f"{idx:>7}  {marker:{col_w}}  {h:12}  {label}  {preview}")
        prev_hash = h

    print()
    print("=" * 59)
    print(f"TRUE REBOOT COUNT (unique summary transitions): {len(true_reboots)}")
    print("=" * 59)
    print()

    for i, r in enumerate(true_reboots):
        head = r["summary_head"][:120].replace("\n", "↵") if r["summary_head"] else "(no summary stored)"
        print(f"  Reboot {i+1:2d}:  req[{r['req_idx']:3d}]  hash={r['summary_hash']}  len={r['summary_len']}  {head}")

    if no_summary:
        print("\nRequests WITHOUT result.metadata.summary:")
        for idx, m in no_summary:
            print(f"  req[{idx}] {m}")

    return len(true_reboots)

def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <SESSION_ID> <WORKSPACE_HASH>")
        sys.exit(1)
    session_id = sys.argv[1]
    workspace_hash = sys.argv[2]
    count = count_reboots(session_id, workspace_hash)
    print(f"\nPatch = {count}")

if __name__ == "__main__":
    main()
