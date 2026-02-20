# reboot-count

Canonical reboot counter for VS Code Copilot Chat agents.

**What is a reboot?** A context compaction event that produced a unique `<conversation-summary>` text, which was subsequently injected into the model's context at the next wake.

**Why this matters:** Agents with the Q-semver identity system use the reboot count as the patch component of their version (`0.0.PATCH`). An incorrect count corrupts identity.

---

## The Ground Truth Rubric

A reboot is counted when **all three** hold:

1. A request has a `progressTaskSerialized` response part with `content.value` in the completed-compaction set
2. That request has `result.metadata.summary.text` — the actual summary text that was generated
3. The MD5 of that summary text is **different** from the previous compaction's summary hash

Two consecutive compaction markers with the **same** summary hash = still one reboot (VS Code can write the same marker twice — see `HISTORY.md` for the kind=2 artifact).

A compaction marker with **no stored summary** (`result.metadata.summary` absent) is a **phantom reboot** — the agent never woke into a `<conversation-summary>` block. Phantoms are **not counted** and do not update the previous-hash state. This handles the edge case where VS Code reloaded mid-compaction: the marker was written but the summary was never stored.

---

## Completed Compaction Markers

| String | Era | Notes |
|--------|-----|-------|
| `"Summarized conversation history"` | Pre-Feb 2026 | Old marker, still valid |
| `"Compacted conversation"` | Feb 2026+ | Current marker |
| `"Compacting conversation..."` | All time | In-progress/aborted — **NOT a completed reboot** |

A compaction that was user-cancelled does NOT write the completion marker. Only `"Summarized"` or `"Compacted"` (without trailing `...`) indicate a completed reboot.

---

## Scripts

### `count_reboots_ground_truth.py` — Reference implementation

Reads the session JSONL, resolves all kind=1/kind=2 patches, extracts summary text for every completed compaction, and counts unique hash transitions.

```
Usage: python3 count_reboots_ground_truth.py <SESSION_ID> <WORKSPACE_HASH>
```

Output:
```
Snapshot: 257 requests | JSONL: 200 lines

Completed compaction events: 29
req_idx  marker                          summary_hash  REBOOT?   summary_preview
...
═══════════════════════════════════════════════════════════
TRUE REBOOT COUNT (unique summary transitions): 29
═══════════════════════════════════════════════════════════
```

### `find_session.py` — Locate your session JSONL

Given a session ID, finds the workspace hash and JSONL path by scanning AppData.

---

## Production Implementation

The production reboot counter lives in the [VSQode/qhoami](https://github.com/VSQode/qhoami) extension.

qhoami v0.1.6+ uses both marker strings and deduplicates per-request-index (prevents the kind=2 array-replace artifact from counting one compaction multiple times).

qhoami does NOT yet implement the ground-truth rubric (summary hash comparison). It counts deduplicated completion markers. Difference:
- For well-behaved sessions: same result
- For sessions with back-to-back compactions producing identical summaries (rare): qhoami overcounts by 1

See [VSQode/qhoami issue tracker](https://github.com/VSQode/qhoami/issues) for the roadmap to full ground-truth implementation.

---

## JSONL Format Primer

Session storage uses an ObjectMutationLog format:

| kind | Description |
|------|-------------|
| 0 | Full snapshot (initial state) |
| 1 | Set-patch: `k` = key path, `v` = new value |
| 2 | Array-replace patch: `k` = key path, `v` = array to merge |

Abbreviated keys: `k` = `keys`, `v` = `value`.

Reboot markers live in response parts:
```json
{"kind": "progressTaskSerialized", "content": {"value": "Compacted conversation"}}
```

Summary text lives in the result:
```json
{"k": ["requests", N, "result"], "v": {"metadata": {"summary": {"text": "..."}}}}
```

Files are at:
```
%APPDATA%\Code - Insiders\User\workspaceStorage\{WORKSPACE_HASH}\chatSessions\{SESSION_ID}.jsonl
```

---

## History

See [HISTORY.md](HISTORY.md) for the full timeline of compaction format changes and bugs encountered.

---

*Authored by POLARIS2 (0.0.29) — DarienSirius / VSQode Governor*  
*Session: 634638ae-2e0b-4ef0-b221-f1cf344185b1*

## Known Limitations

**Old `.json` format sessions:** Sessions from before the JSONL migration are stored as `.json` not `.jsonl`. `count_reboots_ground_truth.py` raises `FileNotFoundError` for these — expected behavior, out of scope.

**Validation coverage (2026-02-20 POLARIS3):**
- `634638ae` (VGM9 nucleus, POLARIS3 main): Patch=35 ✅
- `62b28c3c` (VGM9 nucleus, POLARIS1 sidecar): Patch=2 ✅
- `6d3dd062` (VGM9 husk hash, old .json format): FileNotFoundError as expected ✅
