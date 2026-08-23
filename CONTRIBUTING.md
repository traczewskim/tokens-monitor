# Contributing

Thanks for considering a contribution to `tokens-monitor`. This is a small,
deliberately dependency-free project — please keep changes in that spirit.

## Running locally

No install step:

```bash
git clone git@github.com:traczewskim/tokens-monitor.git
cd tokens-monitor
python3 ccusage.py --open
```

To develop against a smaller or synthetic dataset instead of your real
transcripts, point `--root` at any directory containing `*.jsonl` files with
the same shape Claude Code writes (see `docs/ARCHITECTURE.md` for the record
schema):

```bash
python3 ccusage.py --root /path/to/test-transcripts --port 8788
```

`--json` and `--summary` are useful for quick checks without starting the
server:

```bash
python3 ccusage.py --root /path/to/test-transcripts --summary
python3 ccusage.py --root /path/to/test-transcripts --json | python3 -m json.tool | less
```

## Adding or updating a model in `PRICING`

`PRICING` lives at the top of `ccusage.py`. Keys are **normalized** model
ids — `normalize_model()` strips a trailing 8-digit date suffix (e.g.
`claude-sonnet-5-20251015` → `claude-sonnet-5`), so use the base name without
a date suffix as the key.

```python
"claude-example-6": {
    "in": 3.0,          # USD per million input tokens
    "out": 15.0,         # USD per million output tokens
    "fast": (6.0, 30.0), # optional: rates when usage.speed == "fast"
    "until": ("2026-12-31", 2.0, 10.0),  # optional: (cutoff_date, in, out)
    # rows dated on/before the cutoff use the "until" rate;
    # rows after it use "in"/"out" above.
},
```

- `in`/`out` are required and are USD per **million** tokens.
- `fast` is optional, a `(in, out)` tuple, applied when a record's
  `usage.speed == "fast"` — it takes priority over `until`.
- `until` is optional, a `(cutoff_date_str, in, out)` tuple for models that
  launched at an introductory rate; `cutoff_date_str` is compared as an ISO
  `YYYY-MM-DD` string against the record's date.
- A model not present in `PRICING` at all is priced at `$0` and reported in
  the `unpriced` summary rather than causing an error — prefer adding a
  `PRICING` entry over leaving a real model unpriced.
- Cache-token multipliers (`CACHE_WRITE_5M`, `CACHE_WRITE_1H`, `CACHE_READ`)
  are applied uniformly to every model's input rate and normally do not need
  per-model changes.

## Code style

- **Standard library only.** No `pip install`, no `requirements.txt`, no
  build step. If a change needs a third-party package, it's probably out of
  scope for this project.
- **`panel.html` stays self-contained.** No CDN links, no external fonts,
  icons, or JS libraries, no build/bundle step. Charts are hand-rolled SVG
  so the page works fully offline — keep new visualizations in that style.
- Keep `ccusage.py` a single file. Favor small, readable functions over
  abstractions; the whole parser is meant to be readable top to bottom.

## Verifying parser changes

Any change to `scan()`, `build()`, or `PRICING` can silently change reported
totals, which is the one thing this tool absolutely must get right. Before
and after a change, compare against a known-good baseline on the *same*
transcript set:

```bash
python3 ccusage.py --summary > before.txt
# make your change
python3 ccusage.py --summary > after.txt
diff before.txt after.txt
```

If the diff is not explained by your change (e.g. you fixed a genuine
double-count, or added a new priced model), treat it as a bug. For
deduplication logic specifically, construct a small synthetic transcript
that reproduces the multi-line-per-response pattern described in
`docs/ARCHITECTURE.md` — several JSONL lines sharing one `requestId` and an
`output_tokens` value that grows line over line — and confirm `--json`
reports exactly one row for that response, with `input_tokens`/cache tokens
taken once and `output_tokens` equal to the max across the group.

## Regenerating the screenshots

The images in `docs/` are rendered from synthetic data, never from real
transcripts. To refresh them:

```bash
python3 tools/make_demo_data.py --out /tmp/demo-projects
python3 ccusage.py --root /tmp/demo-projects --port 8790 &

# light
google-chrome --headless=new --disable-gpu --hide-scrollbars \
  --blink-settings=preferredColorScheme=1 --virtual-time-budget=10000 \
  --window-size=1400,2300 --screenshot=docs/screenshot.png \
  http://127.0.0.1:8790/

# dark (headless defaults to dark, so pass no colour-scheme flag)
google-chrome --headless=new --disable-gpu --hide-scrollbars \
  --user-data-dir=/tmp/cp-dark --virtual-time-budget=10000 \
  --window-size=1400,2300 --screenshot=docs/screenshot-dark.png \
  http://127.0.0.1:8790/
```

Use a separate `--user-data-dir` per run; Chrome will otherwise reuse the
first run's rendering and silently give you two identical images. Trim the
trailing background before committing.

Never commit a screenshot taken against `~/.claude/projects`.

## Git hooks

This repo is public and the tool reads private transcripts, so a careless
commit can publish real project paths or usage data. Enable the hooks once
after cloning:

```bash
git config core.hooksPath .githooks
```

Both hooks share the leak checks in `.githooks/lib/guard.sh`:

1. **File policy** — refuses `*.jsonl` transcripts, `.claude/` session state,
   and root-level `*.json` (a `--json` dataset dump).
2. **Identity scan** — refuses added lines containing a real home path. Your
   username is read at runtime with `id -un`; it is never written into the
   hooks, which are themselves public.
3. **Screenshot verification** — OCRs any `docs/screenshot*.png` and refuses
   it unless the synthetic project names are visible. `.gitignore` does not
   apply to already-tracked files, and the regeneration commands above write
   straight back to those paths, so this checks the pixels rather than
   trusting the filename. Requires `tesseract`.

### `pre-commit` — fast

Runs checks 1-3 against the staged changes. Takes about 0.03s.

### `pre-push` — thorough

Runs checks 1-3 again over the **whole range being pushed**, then sends that
range to `claude -p` for a `/security-review` pass and blocks on any HIGH or
MEDIUM finding.

Re-running the leak checks here is deliberate: `git commit --no-verify`
bypasses `pre-commit` entirely, and push is the point where anything actually
becomes public. The review runs once per push rather than once per commit,
which keeps committing instant.

The review stage is skipped automatically when the `claude` CLI is absent, so
external contributors are never blocked by it.

Escape hatches: `SKIP_AI_REVIEW=1 git push ...` keeps the leak checks but
skips the review; `--no-verify` skips a hook entirely.

Note the review examines the range being pushed. The `/security-review` skill
normally diffs `origin/HEAD..HEAD`, which would miss work that has not been
committed or would review the wrong range at hook time.
