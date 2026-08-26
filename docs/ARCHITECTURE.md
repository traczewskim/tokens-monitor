# Architecture

`tokens-monitor` is two files: `ccusage.py` (parsing, aggregation, HTTP
server) and `panel.html` (a single self-contained page — CSS, JS, and
hand-rolled inline SVG charts, no external assets). This document traces the
data flow from raw JSONL transcripts on disk to what renders in the browser.

## 1. Transcript discovery

`iter_transcripts(root)` walks `root` (default `~/.claude/projects`,
overridable with `--root`) recursively and yields every file ending in
`.jsonl`. Claude Code lays these out as
`<root>/<encoded-cwd>/<session-id>.jsonl`, but the walk does not depend on
that structure — it just finds every `.jsonl` file under the root.

## 2. Record schema

Only lines containing the literal substring `"usage"` are parsed as JSON (a
cheap pre-filter before the `json.loads` call). For each parsed line, the
fields consumed are:

| Field | Source | Notes |
|---|---|---|
| dedup key | `requestId`, else `message.id`, else `"<path>:<lineno>"` | identifies one API response |
| timestamp | `timestamp` | ISO 8601; parsed and converted to local time |
| model | `message.model` | normalized by `normalize_model()` (strips a trailing 8-digit date suffix, e.g. `-20251001`) |
| project | `cwd` | the working directory the session ran in, used as-is |
| session | `sessionId` or `session_id` | |
| sidechain | `isSidechain` | 1/0 flag for subagent traffic |
| speed | `message.usage.speed` | defaults to `"standard"`; drives fast-mode pricing |
| input_tokens | `message.usage.input_tokens` | |
| cache_creation | `message.usage.cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` | falls back to the older flat `cache_creation_input_tokens` field, treated as 5m-only, if the split object is absent |
| cache_read_input_tokens | `message.usage.cache_read_input_tokens` | |
| output_tokens | `message.usage.output_tokens` | see dedup below — this is the field that grows across lines |
| thinking_tokens | `message.usage.output_tokens_details.thinking_tokens` | recorded from whichever line currently holds the max `output_tokens` |

Lines without a dict-shaped `message.usage`, or that fail JSON parsing, are
skipped silently.

## 3. Deduplication algorithm

Claude Code streams one API response across **multiple JSONL lines** — one
per content block (a thinking block, each tool call, the final text). Every
line for that response repeats the same `usage` object, except
`output_tokens`, which is the running total as the response streams, so the
*last* line for a given response carries the true final output count.

`scan()` keeps an in-memory dict keyed by the dedup key described above:

1. First line seen for a key: record `dt`, `model`, `project`, `session`,
   `branch`, `side`, `speed`, `input_tokens`, both cache-creation buckets,
   `cache_read_input_tokens`, and the current `output_tokens`/thinking tokens.
2. Subsequent lines for the same key: **only** compare `output_tokens.` If
   the new line's `output_tokens` is larger, replace the stored
   `output_tokens` and thinking tokens with the new line's values. Every
   other field (input tokens, cache tokens, model, timestamp, etc.) is left
   untouched, since those are constant across the group.

**Worked example.** Suppose a single response is written as three JSONL
lines with `requestId = "req_1"`:

| line | input_tokens | cache_read | output_tokens |
|---|---|---|---|
| 1 (thinking block) | 1200 | 300 | 40 |
| 2 (tool call) | 1200 | 300 | 40 |
| 3 (final text) | 1200 | 300 | 260 |

A naive per-line sum reports `input_tokens = 3600`, `cache_read = 900`,
`output_tokens = 340` — every field inflated by roughly 3x for this response.
`scan()` instead produces one record: `input_tokens = 1200`,
`cache_read = 300`, `output_tokens = 260` (the max seen), which matches the
one real API response. Across a full transcript history, where most
responses span two or three content blocks, this naive-sum error compounds
to roughly double the true usage.

Records are also deduplicated **across files** by the same key, which
matters when Claude Code resumes or forks a session and copies earlier lines
into a new transcript file — without this, resumed sessions would double-count
their history.

## 4. Aggregation and the wire payload

`build()` turns the deduplicated `requests` dict into the JSON payload served
to the browser. Rather than repeating strings (model name, project path,
session id) on every row, it interns them:

```python
def idx(table, value):
    if value not in table:
        table[value] = len(table)
    return table[value]
```

`days`, `models`, `projects`, and `sessions` are each a dict mapping a
first-seen string to a small integer id, later flattened to a list ordered by
id (`keys_in_order`). Every request becomes a fixed-width numeric row that
references those tables by index instead of embedding strings:

| index | field | meaning |
|---|---|---|
| 0 | `day` | index into `days` (YYYY-MM-DD) |
| 1 | `hour` | local hour, 0-23 |
| 2 | `dow` | weekday, 0 = Monday |
| 3 | `model` | index into `models` |
| 4 | `project` | index into `projects` |
| 5 | `session` | index into `sessions` |
| 6 | `side` | 1 = subagent, 0 = main thread |
| 7 | `in` | input tokens |
| 8 | `cw` | cache-write tokens (5m + 1h buckets combined) |
| 9 | `cr` | cache-read tokens |
| 10 | `out` | output tokens |
| 11 | `think` | thinking tokens |
| 12 | `cost` | USD at list API rates, rounded to 6 decimals |

Cost is computed once per row at build time (not in the browser) using
`rates_for(model, day, speed)`, which looks up `PRICING`, applies fast-mode
rates when `speed == "fast"`, and applies a time-limited `"until"` rate when
the row's day falls on or before the cutoff. Unpriced models are tallied by
name in an `unpriced` dict and cost `0.0`.

The final payload:

```json
{
  "generated": "...",
  "files": 0,
  "requests": 0,
  "days": ["2026-01-01", "..."],
  "models": ["claude-sonnet-5", "..."],
  "projects": ["/path/one", "..."],
  "sessions": ["...", "..."],
  "rows": [[0, 14, 3, 0, 0, 0, 0, 1200, 300, 0, 260, 40, 0.0126], "..."],
  "session": { "...current 5-hour window, see below..." },
  "unpriced": {"<synthetic>": 3},
  "pricing": { "...copy of PRICING..." },
  "scan_ms": 0
}
```

`pricing` is a copy of the `PRICING` table included so the front end could
display rate provenance if needed. `scan_ms` is added by the server (see
below), not by `build()`.

### Recent activity and the plan window

Claude's plan limit is metered in a five-hour window that belongs to the
**account**, not to this machine: it opens with the first request from any
surface — Claude Code here, claude.ai, the phone app, another checkout, a
container — and only the first of those writes transcripts under `--root`.
Reconstructing the window from local timestamps produces a boundary that
merely coincides with the real one; when Claude is used anywhere else it can
be hours out. So `build()` does not try. It emits a `recent` object instead:

```json
"recent": {
  "hours": 5,
  "bucket_s": 60,
  "t0": "2026-08-25T11:00:00-04:00",
  "buckets": [[672, 131, 38370, 2091981, 50504, 2.5255, 33], "..."],
  "peak": { "tokens": 577434973, "cost": 421.9, "start": "2026-08-15T20:01:19-04:00" },
  "limit_reset": "2026-08-19T02:30:00-04:00"
}
```

- **`buckets`** are per-minute sums — `[index, in, cw, cr, out, cost, requests]`,
  sparse, indexed from `t0` — covering the last `RECENT_SPAN_H` hours. The
  browser sums the ones newer than `now - hours` itself. A total computed on
  the server would freeze at the moment of the scan, because the freshness
  cache keeps serving the same bytes until a transcript changes; buckets let
  the panel roll the window forward on every tick with no rescan.
- **`peak`** is the heaviest `hours` of wall-clock anywhere in the record,
  found by `peak_window()` with two pointers over the timestamp-sorted
  requests — the true maximum over every window position, not the best of a
  fixed grid. It is what the panel's gauge is scaled against, since no plan
  allowance is available to scale against instead.
- **`limit_reset`** is authoritative when present. Claude Code writes a
  `quotaLimits` object onto a record whose request the API actually refused,
  carrying `rateLimitType` and a `resetsAt` epoch; `five_hour_reset()` picks
  the newest five-hour one out of the transcripts during the scan. That fires
  a handful of times in a long history, and the panel uses it only while it is
  still in the future — otherwise the reset time comes from the user, who can
  paste what `/usage` showed them.

## 5. HTTP endpoints

Every request is refused with `403` unless its `Host` header names a
loopback address (or the interface given to `--host`). Binding to
`127.0.0.1` alone is not sufficient: a page on the open web can resolve
its own hostname to `127.0.0.1` (DNS rebinding) and read `/api/data`,
which lists every project path and session id on the machine.

`serve()` starts a `http.server.ThreadingHTTPServer` (HTTP/1.1) bound to
`--host`/`--port`. There are exactly two routes:

- **`GET /`** and **`GET /index.html`** — return `panel.html` verbatim as
  `text/html`. If the file is missing next to `ccusage.py`, responds `500`.
- **`GET /api/data`** (optionally `?refresh=1`) — returns the JSON payload
  described above as `application/json`. Every response sets
  `Cache-Control: no-store` so the browser always re-fetches on load.

Anything else returns `404`.

### The freshness cache

Re-parsing every transcript on every request would be wasteful, but polling
for changes needs to be cheap too. The `Cache` class fingerprints the
transcript set on every `/api/data` request by walking `iter_transcripts()`
and computing `(newest_mtime, file_count, total_size)` — three cheap `stat()`
aggregates, no file content is read for this step. If the fingerprint matches
the one recorded at the last build, the cached JSON bytes are returned as-is.
If it differs, or `?refresh=1` was passed, or nothing has been built yet,
`build()` runs again and `scan_ms` (wall-clock milliseconds for that rebuild)
is attached to the payload before caching it.

This means: transcripts that grow, get added, or get removed are picked up on
the next page load, the panel's **Refresh** button, or a heartbeat poll — no
file watcher, no dependency beyond `os.stat`. It is also what makes the
heartbeat cheap: an idle poll costs one `stat()` per transcript and re-sends
the bytes that are already in memory.

## 6. Front end: filtering and rendering

`panel.html` fetches `/api/data` on page load, on **Refresh** (via
`?refresh=1`), and on each heartbeat tick if one is armed, keeping the full
row set in memory as `S.data`. All filtering — time range (7/30/90 days or all), project, model,
main-thread vs. subagent — happens **client-side** in `filtered()`, which
scans `S.data.rows` and returns the subset matching the current `S` filter
state. No filter ever triggers a new network request.

Every UI control (range buttons, project/model selects, scope select, daily
tokens/cost toggle, theme) re-runs `renderAll()`, which re-derives everything
from `filtered()`:

- **KPI tiles** — totals over the filtered rows, plus a same-day subset for
  the "Today" tile.
- **Daily usage** — rows grouped by day, stacked by billing class (input,
  cache write, cache read, output), with calendar gaps filled in (so silent
  days show as empty bars rather than being skipped) when the date span is
  190 days or fewer.
- **By model / by project** — rows grouped by the relevant index, ranked by
  cost, with the tail beyond a fixed limit (8 for models, 9 for projects)
  folded into a synthetic "Other" bucket.
- **Heatmap** — rows bucketed into a 7×24 weekday-by-local-hour grid, tokens
  as the color-intensity metric.
- **Heaviest sessions** — rows grouped by session, top 12 by cost.

The **last 5 hours** card sits above the KPI tiles and renders the `recent`
object described in section 4: `recentTotals()` sums the buckets newer than
`now - hours` on every render, so the trailing window moves with the clock
rather than with the data. It is deliberately exempt from the filters — usage
against the plan is a property of the account, not of the project you happen
to be looking at.

Its second row reports the plan window's reset, and takes that instant only
from a source that knows it: `resetAnchor()` prefers `limit_reset` while that
is still in the future, falls back to a time the user pasted from `/usage`
(kept in `localStorage` under `ccusage-reset`, never sent anywhere), and
otherwise says it does not know. When the anchor passes, the row says the
window ended and offers a re-sync instead of extrapolating the next boundary —
after an idle stretch the next window opens whenever the account is next used,
which this machine cannot observe. The card re-renders every 10 seconds
whether or not the heartbeat is armed, and skips that tick while the reset
field is open so it cannot yank the input mid-edit.

### The heartbeat

The **Auto-refresh** control (`Off` / `30s` / `60s`, persisted in
`localStorage` under `ccusage-beat`) arms an interval that re-fetches
`/api/data` *without* `refresh=1`, so the server answers from its freshness
cache unless a transcript actually changed. Three things keep an armed
heartbeat from being disruptive:

- **Unchanged payloads never re-render.** The response is compared as text
  against the last one; identical bytes short-circuit before `JSON.parse`, so
  an idle poll leaves the DOM — and any open dropdown or tooltip — untouched.
- **Hidden tabs do not poll.** A tick while `document.hidden` sets a missed
  flag instead of fetching; the `visibilitychange` handler catches up when
  the tab is looked at again.
- **A failed poll keeps the dashboard.** If the server is gone, the numbers
  stay on screen, the status dot goes grey and reads "no answer from the
  server — retrying"; the next successful tick clears it.

All charts are built as inline SVG DOM nodes (`document.createElementNS`) —
no charting library, no CDN, nothing that requires network access. Theme
(auto/light/dark) is stored in `localStorage` and applied via a
`data-theme` attribute that CSS custom properties key off of; "auto" follows
`prefers-color-scheme`.
