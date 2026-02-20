# Reboot Marker Format History

This document tracks every known format change in VS Code's compaction system, including bugs and artifacts that confuse automated reboot counters.

---

## Timeline

### Pre-Feb 2026 — `"Summarized conversation history"`

The original completed-compaction marker. Written as a `progressTask` response part (not yet `progressTaskSerialized`). Both the kind name AND the content string were different:

- `"kind": "progressTask"` (old)
- `"kind": "progressTaskSerialized"` (new, Feb 2026+)

Tools that only check `progressTaskSerialized` are **blind to all pre-Feb 2026 reboots**.

### Feb 2026+ — `"Compacted conversation"`

VS Code changed both the event kind (`progressTask` → `progressTaskSerialized`) and the completion string. The in-progress string also changed:

| Event | Old (pre-Feb 2026) | New (Feb 2026+) |
|-------|---------------------|-----------------|
| In progress | `"Summarizing conversation..."` | `"Compacting conversation..."` |
| Completed | `"Summarized conversation history"` | `"Compacted conversation"` |

The production qhoami tool was blind to the new string until v0.1.6 (commit `3154ba6`, Feb 2026).

---

## Known Bugs and Artifacts

### kind=2 Duplicate Artifact

VS Code sometimes writes a `kind=2` (array-replace) JSONL line that contains MULTIPLE copies of the same response part, including the compaction completion marker. A naive counter reading the resolved response array will count one compaction event multiple times.

**Example:** req[133] in session `634638ae` had 4 copies of `"Summarized conversation history"` due to a kind=2 patch write. Pre-v0.1.6 qhoami counted this as 4 reboots.

**Fix:** Deduplicate by request index — only count the first match per request, then `break`.

### Cancelled Compaction (No False Positive)

If the user clicks "Cancel" during compaction, VS Code writes ONLY the in-progress marker (`"Compacting conversation..."`) — NOT the completion marker. The JSONL therefore accurately distinguishes completed vs aborted.

No false positive from user-cancelled compactions. The in-progress string is explicitly excluded from the completed-set check.

### Reboot 7 (req[76]) — Missing Summary

req[76] in session `634638ae` has a `"Summarized conversation history"` marker but NO `result.metadata.summary.text`. The result was either not stored or the summary format was different at that era.

The missing-summary case is treated as a unique reboot (hash `"NO_SUMMARY"`) since it cannot be verified as a duplicate.

### Back-to-Back Compactions (req[150]–req[151])

Session `634638ae` has two completed compactions at consecutive request indices (150 and 151) with DIFFERENT summary hashes. This means VS Code ran two separate compaction cycles with no user message between them. Both are legitimate unique reboots. Ground truth count confirmed both as distinct.

---

## qhoami Accuracy by Version

| Version | Blind to | Overcounts | Result for session 634638ae |
|---------|----------|------------|------------------------------|
| v0.1.5 and earlier | All `"Compacted conversation"` markers | kind=2 duplicates | ~4–5 (catastrophically wrong) |
| v0.1.6 | Nothing | None known | 28 (correct at time of count; excludes current req[264]) |
| Planned v0.2.0 | Nothing | None | Full ground truth (summary hash dedup) |

---

*Last updated: 2026-02-∗ by POLARIS2 (0.0.29)*
