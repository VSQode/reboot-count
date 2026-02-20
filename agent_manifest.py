#!/usr/bin/env python3
"""
agent_manifest.py — Comprehensive per-patch agent activity manifest.

For each reboot window (patch), reconstructs:
  - Model used at start and end of the patch
  - Files CREATED, EDITED (from chatEditingSessions), READ (from tool calls)
  - Shell commands run (from run_in_terminal resultDetails.input)
  - Live filesystem stats (exists, ctime, mtime, size)
  - Instruction/context files attached to prompts (contentReferences)
  - Tool call breakdown counts

Data sources:
  1. chatSessions JSONL:        requests, modelId, timestamps, tool calls
  2. chatEditingSessions/state.json: file operations indexed by requestId
  3. Live filesystem:           os.stat() for all referenced file paths

Usage:
    python3 agent_manifest.py <SESSION_ID> <WORKSPACE_HASH> [options]

Options:
    --summary          Brief per-patch table (default)
    --patch N          Full detail for a single patch
    --model-timeline   Just the model transition at each patch boundary
    --all              Full detail for every patch (verbose)
    --files            File inventory with live filesystem stats
    --cmds             All shell commands, by patch

AppData path constructed from arguments:
    %APPDATA%\\Code - Insiders\\User\\workspaceStorage\\{WORKSPACE_HASH}\\...
"""

import sys
import json
import os
import copy
import re
import datetime
from pathlib import Path
from collections import defaultdict, Counter
from urllib.parse import unquote

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APPDATA = os.environ.get("APPDATA", "")
STORAGE_BASE = Path(APPDATA) / "Code - Insiders" / "User" / "workspaceStorage"

COMPLETED_MARKERS = {
    "Summarized conversation history",
    "Compacted conversation",
}

FILE_TOOL_IDS = {
    "copilot_readFile",
    "copilot_createFile",
    "copilot_createDirectory",
    "copilot_replaceString",
    "copilot_multiReplaceString",
    "copilot_editFiles",
    "copilot_findFiles",
    "copilot_findTextInFiles",
    "copilot_listDirectory",
    "copilot_getErrors",
    "copilot_getChangedFiles",
    "copilot_searchCodebase",
}

WRITE_TOOL_IDS = {
    "copilot_createFile",
    "copilot_createDirectory",
    "copilot_replaceString",
    "copilot_multiReplaceString",
    "copilot_editFiles",
}

READ_TOOL_IDS = {
    "copilot_readFile",
    "copilot_findFiles",
    "copilot_findTextInFiles",
    "copilot_listDirectory",
    "copilot_getErrors",
    "copilot_getChangedFiles",
    "copilot_searchCodebase",
}

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def ms_to_dt(ms):
    """Convert milliseconds-since-epoch int to datetime (UTC)."""
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000, tz=datetime.timezone.utc)
    except Exception:
        return None


def fmt_ts(ms):
    dt = ms_to_dt(ms)
    if dt is None:
        return "?"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_dt(dt):
    if dt is None:
        return "?"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def duration_str(start_ms, end_ms):
    try:
        delta = (int(end_ms) - int(start_ms)) / 1000
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        return f"{h}h{m:02d}m"
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# File path extraction from tool call invocationMessage
# ---------------------------------------------------------------------------

def extract_file_path(part):
    """
    Extract a Windows file path from a toolInvocationSerialized response part.
    Returns the path string or None.
    """
    msg = part.get("invocationMessage", "")
    if isinstance(msg, dict):
        msg_text = msg.get("value", "")
    else:
        msg_text = str(msg) if msg else ""

    # Markdown link pattern: [text](file:///c%3A/...)
    m = re.search(r"\]\(file:///([^)]+)\)", msg_text)
    if m:
        encoded = m.group(1)
        path = unquote(encoded).replace("/", os.sep)
        return path

    # Bare URL fallback: file:///c%3A/...
    m = re.search(r"file:///([^\s'\"]+)", msg_text)
    if m:
        encoded = m.group(1)
        path = unquote(encoded).replace("/", os.sep)
        return path

    return None


def extract_model_short(model_id):
    """'copilot/claude-sonnet-4.6' -> 'claude-sonnet-4.6'"""
    if not model_id:
        return "?"
    return model_id.split("/")[-1] if "/" in model_id else model_id


# ---------------------------------------------------------------------------
# JSONL loader (chatSessions)
# ---------------------------------------------------------------------------

class Request:
    __slots__ = (
        "idx", "request_id", "timestamp", "model_id",
        "tool_calls", "content_refs", "message_text",
        "is_reboot", "summary_hash",
    )

    def __init__(self, idx, raw):
        self.idx = idx
        self.request_id = raw.get("requestId", "")
        self.timestamp = raw.get("timestamp")
        self.model_id = raw.get("modelId", "")
        self.message_text = ""
        self.tool_calls = []   # list of ToolCall objects
        self.content_refs = [] # list of file path strings
        self.is_reboot = False
        self.summary_hash = None

        # Parse message text
        msg = raw.get("message", {})
        if isinstance(msg, dict):
            self.message_text = msg.get("text", "")
        elif isinstance(msg, str):
            self.message_text = msg

        # Parse content references (instruction files attached to prompt)
        for ref in (raw.get("contentReferences") or []):
            if isinstance(ref, dict):
                inner = ref.get("reference", {})
                if isinstance(inner, dict):
                    fp = inner.get("fsPath")
                    if fp:
                        self.content_refs.append(fp)

        # Parse tool calls from response array
        for part in (raw.get("response") or []):
            if not isinstance(part, dict):
                continue
            if part.get("kind") != "toolInvocationSerialized":
                continue
            tc = ToolCall(part)
            self.tool_calls.append(tc)

        # Check for completed reboot marker
        for part in (raw.get("response") or []):
            if not isinstance(part, dict):
                continue
            kind_str = str(part.get("kind", ""))
            if "progressTask" not in kind_str:
                continue
            content = part.get("content", {})
            if isinstance(content, dict):
                content = content.get("value", "")
            if isinstance(content, str) and content in COMPLETED_MARKERS:
                self.is_reboot = True
                break

        # Extract summary hash if this is a reboot request
        if self.is_reboot:
            result = raw.get("result", {})
            if isinstance(result, dict):
                meta = result.get("metadata", {})
                if isinstance(meta, dict):
                    summ = meta.get("summary", {})
                    if isinstance(summ, dict):
                        text = summ.get("text", "")
                        if text:
                            import hashlib
                            self.summary_hash = hashlib.md5(text.encode()).hexdigest()[:8]


class ToolCall:
    __slots__ = ("tool_id", "tool_call_id", "file_path", "command", "is_complete")

    def __init__(self, part):
        self.tool_id = part.get("toolId", "")
        self.tool_call_id = part.get("toolCallId", "")
        self.is_complete = bool(part.get("isComplete", False))
        self.file_path = extract_file_path(part)
        self.command = None

        # For run_in_terminal: extract command from resultDetails.input
        rd = part.get("resultDetails")
        if self.tool_id == "run_in_terminal" and isinstance(rd, dict):
            self.command = rd.get("input", "")


def load_requests(session_id, workspace_hash):
    """Load and resolve all requests from chatSessions JSONL. Returns list[Request]."""
    path = STORAGE_BASE / workspace_hash / "chatSessions" / f"{session_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"chatSessions JSONL not found: {path}")

    lines = path.read_bytes().decode("utf-8", "replace").splitlines()

    # kind=0 snapshot
    snap = json.loads(lines[0])
    raw_reqs = snap.get("v", {}).get("requests", [])
    req_data = {i: copy.deepcopy(r) for i, r in enumerate(raw_reqs) if r is not None}
    next_idx = len(raw_reqs)

    # Apply patches
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = obj.get("kind")
        keys = obj.get("k", [])
        val = obj.get("v")

        # Top-level requests array extension
        if kind == 2 and keys == ["requests"] and isinstance(val, list):
            for r in val:
                if isinstance(r, dict):
                    req_data[next_idx] = r
                    next_idx += 1
            continue

        if kind not in (1, 2):
            continue

        if not (len(keys) >= 3 and keys[0] == "requests" and isinstance(keys[1], int)):
            continue

        req_idx = keys[1]
        field = keys[2]

        if req_idx not in req_data:
            req_data[req_idx] = {}

        if kind == 1:
            if len(keys) == 3:
                req_data[req_idx][field] = val
            elif len(keys) >= 4:
                # Deep patch into a sub-field
                target = req_data[req_idx].setdefault(field, {})
                sub_keys = keys[3:]
                for sk in sub_keys[:-1]:
                    if isinstance(target, dict):
                        target = target.setdefault(sk, {})
                if isinstance(target, dict) and sub_keys:
                    target[sub_keys[-1]] = val
        elif kind == 2 and field == "response":
            existing = req_data[req_idx].get("response", [])
            if isinstance(val, list):
                existing.extend(val)
                req_data[req_idx]["response"] = existing

    # Build Request objects sorted by index
    requests = []
    for idx in sorted(req_data.keys()):
        raw = req_data[idx]
        if raw:
            requests.append(Request(idx, raw))

    print(f"chatSessions: {len(requests)} requests loaded", file=sys.stderr)
    return requests


# ---------------------------------------------------------------------------
# Reboot window computation
# ---------------------------------------------------------------------------

def compute_patch_windows(requests):
    """
    Returns list of (patch_num, start_req_idx, end_req_idx, start_ts, end_ts)
    where start/end are indices into the requests list.

    Patch 0 = before first reboot.
    Patch N = after Nth unique summary transition.
    """
    # Deduplicate reboots: only count transitions to a new summary hash
    reboot_indices = []  # indices in requests list
    prev_hash = None
    for i, req in enumerate(requests):
        if req.is_reboot and req.summary_hash and req.summary_hash != prev_hash:
            reboot_indices.append(i)
            prev_hash = req.summary_hash

    # Build windows
    # Patch 0: requests[0 .. reboot_indices[0]]
    # Patch k: requests[reboot_indices[k-1]+1 .. reboot_indices[k]]
    # Patch N: requests[reboot_indices[-1]+1 .. end]
    windows = []
    starts = [0] + [ri + 1 for ri in reboot_indices]
    ends = reboot_indices + [len(requests) - 1]
    for patch_num, (start, end) in enumerate(zip(starts, ends)):
        start_ts = requests[start].timestamp if start < len(requests) else None
        end_ts = requests[end].timestamp if end < len(requests) else None
        windows.append({
            "patch": patch_num,
            "req_start": start,
            "req_end": end,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "requests": requests[start: end + 1],
        })

    print(f"Reboot windows: {len(windows)} patches (0-{len(windows)-1})", file=sys.stderr)
    return windows


# ---------------------------------------------------------------------------
# chatEditingSessions loader
# ---------------------------------------------------------------------------

def load_editing_ops(session_id, workspace_hash):
    """
    Returns dict: requestId -> list of {type, fsPath, epoch}
    """
    path = (STORAGE_BASE / workspace_hash / "chatEditingSessions"
            / session_id / "state.json")
    if not path.exists():
        print(f"WARNING: chatEditingSessions state.json not found: {path}", file=sys.stderr)
        return {}

    data = json.loads(path.read_bytes().decode("utf-8", "replace"))
    ops = data.get("timeline", {}).get("operations", [])
    print(f"chatEditingSessions: {len(ops)} operations loaded", file=sys.stderr)

    by_request = defaultdict(list)
    for op in ops:
        rid = op.get("requestId", "")
        uri = op.get("uri", {})
        if isinstance(uri, dict):
            fspath = uri.get("fsPath", "")
        else:
            fspath = str(uri)
        op_type = op.get("type", "")
        epoch = op.get("epoch", 0)
        if rid and fspath:
            by_request[rid].append({
                "type": op_type,
                "fsPath": fspath,
                "epoch": epoch,
            })

    return dict(by_request)


# ---------------------------------------------------------------------------
# Live filesystem stats
# ---------------------------------------------------------------------------

FS_CACHE = {}


def fs_stat(path_str):
    """Returns (exists, ctime_str, mtime_str, size_str) from live filesystem."""
    if path_str in FS_CACHE:
        return FS_CACHE[path_str]

    p = Path(path_str)
    if not p.exists():
        result = (False, None, None, None)
    else:
        try:
            st = p.stat()
            ctime = datetime.datetime.fromtimestamp(st.st_ctime, tz=datetime.timezone.utc)
            mtime = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc)
            size = st.st_size
            result = (True, ctime, mtime, size)
        except OSError:
            result = (False, None, None, None)

    FS_CACHE[path_str] = result
    return result


def fmt_size(size):
    if size is None:
        return ""
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size/1024:.1f}KB"
    else:
        return f"{size/1024/1024:.1f}MB"


def fmt_stat(path_str, show_ctime=True):
    """One-line filesystem stat summary."""
    exists, ctime, mtime, size = fs_stat(path_str)
    if not exists:
        return "[MISSING]"
    parts = ["[EXISTS]"]
    if show_ctime and ctime:
        parts.append(f"ctime={fmt_dt(ctime)}")
    if mtime:
        parts.append(f"mtime={fmt_dt(mtime)}")
    if size is not None:
        parts.append(fmt_size(size))
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Path display helper
# ---------------------------------------------------------------------------

def short_path(path_str, max_len=80):
    """Shorten a path for display by collapsing VGM9 prefix."""
    p = str(path_str).replace("\\", "/")
    p = re.sub(r"^[Cc]:/www/VGM9/", "VGM9/", p)
    if len(p) > max_len:
        p = "..." + p[-(max_len - 3):]
    return p


# ---------------------------------------------------------------------------
# Per-patch analysis
# ---------------------------------------------------------------------------

def analyze_patch(window, editing_ops):
    """
    Build a rich per-patch record from a window dict and editing_ops.
    """
    reqs = window["requests"]

    # Model at first and last request
    model_start = extract_model_short(reqs[0].model_id) if reqs else "?"
    model_end = extract_model_short(reqs[-1].model_id) if reqs else "?"

    # All tool calls
    all_tool_calls = []
    for req in reqs:
        for tc in req.tool_calls:
            all_tool_calls.append((req, tc))

    tool_id_counts = Counter(tc.tool_id for _, tc in all_tool_calls)

    # File-related tool calls (reads/writes from chatSessions perspective)
    reads = []    # (path, tool_id)
    writes = []   # (path, tool_id)
    cmds = []     # shell command strings
    for req, tc in all_tool_calls:
        if tc.tool_id == "run_in_terminal":
            if tc.command:
                cmds.append(tc.command)
        elif tc.tool_id in READ_TOOL_IDS:
            if tc.file_path:
                reads.append((tc.file_path, tc.tool_id))
        elif tc.tool_id in WRITE_TOOL_IDS:
            if tc.file_path:
                writes.append((tc.file_path, tc.tool_id))

    # File ops from chatEditingSessions (creates and edits)
    edit_creates = []   # unique file paths
    edit_edits = []     # unique file paths
    seen_creates = set()
    seen_edits = set()

    for req in reqs:
        ops_for_req = editing_ops.get(req.request_id, [])
        for op in ops_for_req:
            fp = op["fsPath"]
            if op["type"] == "create" and fp not in seen_creates:
                seen_creates.add(fp)
                edit_creates.append(fp)
            elif op["type"] == "textEdit" and fp not in seen_edits:
                seen_edits.add(fp)
                edit_edits.append(fp)

    # Content references (unique instruction files seen across all requests in patch)
    content_refs = set()
    for req in reqs:
        content_refs.update(req.content_refs)

    # Duration
    dur = None
    if window["start_ts"] and window["end_ts"]:
        try:
            dur = (int(window["end_ts"]) - int(window["start_ts"])) / 1000
        except Exception:
            pass

    return {
        "patch": window["patch"],
        "start_ts": window["start_ts"],
        "end_ts": window["end_ts"],
        "duration_s": dur,
        "req_count": len(reqs),
        "req_start_idx": window["req_start"],
        "req_end_idx": window["req_end"],
        "model_start": model_start,
        "model_end": model_end,
        "tool_counts": tool_id_counts,
        "reads": reads,
        "writes": writes,
        "cmds": cmds,
        "edit_creates": edit_creates,
        "edit_edits": edit_edits,
        "content_refs": sorted(content_refs),
    }


# ---------------------------------------------------------------------------
# Output modes
# ---------------------------------------------------------------------------

def print_summary(analyses):
    """Brief table of all patches."""
    hdr = (f"{'P':>3}  {'start':23}  {'dur':7}  "
           f"{'model start':<22}  {'model end':<22}  "
           f"{'reqs':>4}  {'creat':>5}  {'edit':>5}  "
           f"{'reads':>5}  {'cmds':>5}")
    print(hdr)
    print("-" * len(hdr))
    for a in analyses:
        dur_str = ""
        if a["duration_s"] is not None:
            h = int(a["duration_s"] // 3600)
            m = int((a["duration_s"] % 3600) // 60)
            dur_str = f"{h}h{m:02d}m"
        model_change = "  " if a["model_start"] == a["model_end"] else "->"
        model_col = f"{a['model_start']:<22}" if a["model_start"] == a["model_end"] \
                    else f"{a['model_start']:<20}{model_change}"
        reads_count = len(set(p for p, _ in a["reads"]))
        print(f"P{a['patch']:02d}  {fmt_ts(a['start_ts']):23}  {dur_str:7}  "
              f"{a['model_start']:<22}  {a['model_end']:<22}  "
              f"{a['req_count']:4d}  {len(a['edit_creates']):5d}  "
              f"{len(a['edit_edits']):5d}  "
              f"{reads_count:5d}  {len(a['cmds']):5d}")


def print_model_timeline(analyses):
    """Model at each patch boundary."""
    print(f"{'PATCH':>5}  {'start':23}  {'dur':7}  model")
    print("-" * 80)
    prev_model = None
    for a in analyses:
        if a["duration_s"] is not None:
            h = int(a["duration_s"] // 3600)
            m = int((a["duration_s"] % 3600) // 60)
            dur_str = f"{h}h{m:02d}m"
        else:
            dur_str = ""
        model_str = a["model_start"]
        if a["model_start"] != a["model_end"]:
            model_str = f"{a['model_start']} -> {a['model_end']}"
        marker = " <-- CHANGE" if model_str != prev_model and prev_model is not None else ""
        print(f"P{a['patch']:02d}    {fmt_ts(a['start_ts']):23}  {dur_str:7}  {model_str}{marker}")
        prev_model = model_str


def print_patch_detail(a):
    """Full detail for a single patch."""
    patch = a["patch"]
    dur_s = a["duration_s"]
    dur_str = ""
    if dur_s is not None:
        h = int(dur_s // 3600)
        m = int((dur_s % 3600) // 60)
        dur_str = f" ({h}h{m:02d}m)"

    print(f"\n{'='*72}")
    print(f"PATCH {patch:2d}  [{fmt_ts(a['start_ts'])} -> {fmt_ts(a['end_ts'])}]{dur_str}")
    print(f"{'='*72}")
    model_str = a["model_start"]
    if a["model_start"] != a["model_end"]:
        model_str = f"{a['model_start']}  ->  {a['model_end']}  (CHANGED)"
    print(f"  Model:    {model_str}")
    print(f"  Requests: {a['req_count']}  (req#{a['req_start_idx']}..#{a['req_end_idx']})")

    # Tool breakdown
    print(f"\n  TOOL CALLS:")
    for tool_id, cnt in sorted(a["tool_counts"].items(), key=lambda x: -x[1]):
        bar = "#" * min(cnt, 40)
        print(f"    {cnt:5d}  {tool_id:<35}  {bar}")

    # Files created (chatEditingSessions)
    print(f"\n  CREATED ({len(a['edit_creates'])} unique files, from chatEditingSessions):")
    for fp in sorted(a["edit_creates"]):
        stat = fmt_stat(fp, show_ctime=True)
        print(f"    {stat:<50}  {short_path(fp)}")

    # Files edited (chatEditingSessions)
    print(f"\n  EDITED ({len(a['edit_edits'])} unique files, from chatEditingSessions):")
    for fp in sorted(a["edit_edits"]):
        stat = fmt_stat(fp, show_ctime=False)
        print(f"    {stat:<35}  {short_path(fp)}")

    # Files read (tool calls)
    read_file_counts = Counter(p for p, t in a["reads"] if t == "copilot_readFile")
    print(f"\n  READ via copilot_readFile ({len(read_file_counts)} unique files,"
          f" {sum(read_file_counts.values())} total calls):")
    for fp, cnt in sorted(read_file_counts.items(), key=lambda x: -x[1]):
        stat = fmt_stat(fp, show_ctime=False)
        print(f"    {cnt:4d}x  {stat:<35}  {short_path(fp)}")

    # Other read ops (search, list, etc.)
    other_reads = [(p, t) for p, t in a["reads"] if t != "copilot_readFile" and p]
    if other_reads:
        print(f"\n  OTHER READ OPS ({len(other_reads)}):")
        for fp, tid in sorted(other_reads):
            print(f"    {tid:<30}  {short_path(fp)}")

    # Shell commands
    print(f"\n  SHELL COMMANDS ({len(a['cmds'])}):")
    for cmd in a["cmds"]:
        # Truncate long commands
        display_cmd = cmd.replace("\n", " ").strip()
        if len(display_cmd) > 120:
            display_cmd = display_cmd[:117] + "..."
        print(f"    $ {display_cmd}")

    # Content references
    print(f"\n  CONTEXT FILES ATTACHED TO PROMPTS ({len(a['content_refs'])}):")
    for fp in a["content_refs"]:
        stat = fmt_stat(fp, show_ctime=False)
        print(f"    {stat:<35}  {short_path(fp)}")


def print_files_inventory(analyses):
    """All files referenced across all patches with live fs stats."""
    all_files = defaultdict(lambda: {
        "patches_created": [],
        "patches_edited": [],
        "patches_read": set(),
    })

    for a in analyses:
        patch = a["patch"]
        for fp in a["edit_creates"]:
            all_files[fp]["patches_created"].append(patch)
        for fp in a["edit_edits"]:
            all_files[fp]["patches_edited"].append(patch)
        for fp, _ in a["reads"]:
            all_files[fp]["patches_read"].add(patch)

    print(f"\n{'FILE INVENTORY':^80}")
    print(f"{'status':<10}  {'ctime':19}  {'mtime':19}  {'size':8}  file")
    print("-" * 120)
    for fp in sorted(all_files.keys()):
        info = all_files[fp]
        exists, ctime, mtime, size = fs_stat(fp)
        status = "[EXISTS]" if exists else "[MISSING]"
        ct_str = fmt_dt(ctime) if ctime else "-" * 19
        mt_str = fmt_dt(mtime) if mtime else "-" * 19
        sz_str = fmt_size(size) if size is not None else ""
        created_p = f"C:{','.join(str(p) for p in info['patches_created'])}" if info["patches_created"] else ""
        edited_p = f"E:{','.join(str(p) for p in info['patches_edited'][:5])}" if info["patches_edited"] else ""
        read_p = f"R:{','.join(str(p) for p in sorted(info['patches_read'])[:5])}" if info["patches_read"] else ""
        patch_info = "  ".join(x for x in [created_p, edited_p, read_p] if x)
        print(f"{status:<10}  {ct_str}  {mt_str}  {sz_str:>8}  {short_path(fp, 60)}  [{patch_info}]")


def print_cmds_by_patch(analyses):
    """All shell commands grouped by patch."""
    for a in analyses:
        if not a["cmds"]:
            continue
        print(f"\n--- PATCH {a['patch']:2d} [{fmt_ts(a['start_ts'])}] "
              f"model={a['model_start']} ---")
        for cmd in a["cmds"]:
            display_cmd = cmd.replace("\n", " ").strip()
            if len(display_cmd) > 130:
                display_cmd = display_cmd[:127] + "..."
            print(f"  $ {display_cmd}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    session_id = sys.argv[1]
    workspace_hash = sys.argv[2]
    flags = set(sys.argv[3:])

    # Parse --patch N
    patch_target = None
    for i, arg in enumerate(sys.argv[3:]):
        if arg == "--patch" and i + 4 < len(sys.argv):
            try:
                patch_target = int(sys.argv[i + 4])
            except ValueError:
                pass

    print(f"\nLoading session {session_id[:8]}... / workspace {workspace_hash[:8]}...",
          file=sys.stderr)

    # Load data
    requests = load_requests(session_id, workspace_hash)
    windows = compute_patch_windows(requests)
    editing_ops = load_editing_ops(session_id, workspace_hash)

    # Analyze each patch
    print("Analyzing patches...", file=sys.stderr)
    analyses = [analyze_patch(w, editing_ops) for w in windows]

    print(f"\n", file=sys.stderr)

    # Output mode
    if "--model-timeline" in flags:
        print_model_timeline(analyses)
    elif "--files" in flags:
        print_files_inventory(analyses)
    elif "--cmds" in flags:
        print_cmds_by_patch(analyses)
    elif "--all" in flags:
        for a in analyses:
            print_patch_detail(a)
    elif "--patch" in flags and patch_target is not None:
        matching = [a for a in analyses if a["patch"] == patch_target]
        if not matching:
            print(f"Patch {patch_target} not found. Available: 0-{len(analyses)-1}")
            sys.exit(1)
        print_patch_detail(matching[0])
    else:
        # Default: summary
        print(f"AGENT MANIFEST SUMMARY")
        print(f"Session: {session_id}")
        print(f"Patches: {len(analyses)}  (P0 = pre-first-reboot, P{len(analyses)-1} = current)")
        print()
        print_summary(analyses)


if __name__ == "__main__":
    main()
