#!/usr/bin/env python3
"""
Claude Code token usage panel.

Reads the local JSONL transcripts Claude Code writes under ~/.claude/projects
and serves a dashboard on localhost. Standard library only, no network calls.

    python3 ccusage.py            # serve at http://127.0.0.1:8787
    python3 ccusage.py --json     # dump the parsed dataset to stdout
    python3 ccusage.py --summary  # print a text summary
"""

import argparse
import http.server
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.expanduser("~/.claude/projects")
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Pricing: USD per million tokens (first-party Anthropic API rates).
# These are list API rates. If you use Claude Code on a subscription plan the
# dollar figures are "what this traffic would have cost at API rates", not a
# bill. Edit this table if rates change.
# ---------------------------------------------------------------------------
PRICING = {
    "claude-fable-5":    {"in": 10.0, "out": 50.0},
    "claude-mythos-5":   {"in": 10.0, "out": 50.0},
    "claude-opus-5":     {"in": 5.0,  "out": 25.0, "fast": (10.0, 50.0)},
    "claude-opus-4-8":   {"in": 5.0,  "out": 25.0},
    "claude-opus-4-7":   {"in": 5.0,  "out": 25.0},
    "claude-opus-4-6":   {"in": 5.0,  "out": 25.0},
    "claude-opus-4-5":   {"in": 5.0,  "out": 25.0},
    # Sonnet 5 ran at intro rates ($2/$10) through 2026-08-31.
    "claude-sonnet-5":   {"in": 3.0,  "out": 15.0, "until": ("2026-08-31", 2.0, 10.0)},
    "claude-sonnet-4-6": {"in": 3.0,  "out": 15.0},
    "claude-sonnet-4-5": {"in": 3.0,  "out": 15.0},
    "claude-haiku-4-5":  {"in": 1.0,  "out": 5.0},
    "claude-haiku-4-0":  {"in": 1.0,  "out": 5.0},
}

CACHE_WRITE_5M = 1.25   # x input rate
CACHE_WRITE_1H = 2.00   # x input rate
CACHE_READ     = 0.10   # x input rate

# Claude's plan limit is metered in a five-hour window, but that window belongs
# to the *account*: it opens with the first request from any surface — this
# machine, claude.ai, a phone, a container — and the transcripts here only see
# Claude Code on this machine. Reconstructing the window from local timestamps
# therefore produces a boundary that merely coincides with the real one, so the
# panel reports a plain trailing five hours instead and takes the reset instant
# from an authoritative source or from the user (see `quota_reset` below).
WINDOW_H = 5
BUCKET_S = 60        # resolution of the recent-activity buckets
RECENT_SPAN_H = 12   # how much recent history to bucket, so the panel can roll
                     # the window forward without the server rescanning


def normalize_model(model):
    """'claude-haiku-4-5-20251001' -> 'claude-haiku-4-5'."""
    if not model:
        return "unknown"
    parts = model.split("-")
    if parts and len(parts[-1]) == 8 and parts[-1].isdigit():
        return "-".join(parts[:-1])
    return model


def rates_for(model, day, speed):
    """Return (input_rate, output_rate) per million tokens, or None if unpriced."""
    p = PRICING.get(model)
    if not p:
        return None
    if speed == "fast" and "fast" in p:
        return p["fast"]
    if "until" in p:
        end, in_r, out_r = p["until"]
        if day <= end:
            return (in_r, out_r)
    return (p["in"], p["out"])


def iter_transcripts(root=ROOT):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".jsonl"):
                yield os.path.join(dirpath, name)


def five_hour_reset(line):
    """Epoch seconds of a five-hour limit reset carried on a rate-limited record.

    Claude Code writes `quotaLimits` only when the API actually refuses a
    request, so this fires a handful of times in a long history — but when it
    does, it is the real window boundary rather than a guess.
    """
    try:
        q = (json.loads(line) or {}).get("quotaLimits") or {}
    except ValueError:
        return 0
    if q.get("rateLimitType") != "five_hour":
        return 0
    try:
        return int(q.get("resetsAt") or 0)
    except (TypeError, ValueError):
        return 0


def scan(root=ROOT):
    """Parse every transcript into one deduplicated record per API request.

    Claude Code writes one JSONL line per content block, so a single API
    response appears as several lines that repeat the same usage object with a
    growing output_tokens. Counting lines naively roughly doubles every total.
    Dedup key is requestId; input/cache tokens are taken once and output_tokens
    is the max across the group.
    """
    requests = {}
    files = 0
    quota_reset = None   # newest five-hour reset the API actually told us about
    for path in iter_transcripts(root):
        files += 1
        try:
            fh = open(path, "r", errors="ignore")
        except OSError:
            continue
        with fh:
            for lineno, line in enumerate(fh):
                if '"quotaLimits"' in line:
                    quota_reset = max(quota_reset or 0, five_hour_reset(line))
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message") or {}
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue

                key = d.get("requestId") or msg.get("id") or "%s:%d" % (path, lineno)
                out = usage.get("output_tokens") or 0

                prev = requests.get(key)
                if prev is not None:
                    # Same API response, later content block: only output grows.
                    if out > prev["out"]:
                        prev["out"] = out
                        think = ((usage.get("output_tokens_details") or {})
                                 .get("thinking_tokens") or 0)
                        prev["think"] = think
                    continue

                ts_raw = d.get("timestamp") or ""
                try:
                    dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    dt = dt.astimezone()  # local time
                except ValueError:
                    continue

                cc = usage.get("cache_creation") or {}
                cw5 = cc.get("ephemeral_5m_input_tokens")
                cw1 = cc.get("ephemeral_1h_input_tokens")
                if cw5 is None and cw1 is None:
                    cw5 = usage.get("cache_creation_input_tokens") or 0
                    cw1 = 0
                cw5 = cw5 or 0
                cw1 = cw1 or 0

                cwd = d.get("cwd") or ""
                requests[key] = {
                    "dt": dt,
                    "model": normalize_model(msg.get("model")),
                    "project": cwd,
                    "session": d.get("sessionId") or d.get("session_id") or "",
                    "side": 1 if d.get("isSidechain") else 0,
                    "speed": usage.get("speed") or "standard",
                    "in": usage.get("input_tokens") or 0,
                    "cw5": cw5,
                    "cw1": cw1,
                    "cr": usage.get("cache_read_input_tokens") or 0,
                    "out": out,
                    "think": ((usage.get("output_tokens_details") or {})
                              .get("thinking_tokens") or 0),
                }
    if quota_reset:
        quota_reset = datetime.fromtimestamp(quota_reset).astimezone().isoformat(
            timespec="seconds")
    return requests, files, quota_reset or None


def peak_window(records, hours=WINDOW_H):
    """Heaviest `hours` of wall-clock anywhere in the record.

    Two pointers over the timestamp-sorted records, so it is the true maximum
    over every possible window position, not the best of a fixed grid.
    """
    items = sorted(
        ((r["dt"], r["in"] + r["cw5"] + r["cw1"] + r["cr"] + r["out"], r["cost"])
         for r in records),
        key=lambda x: x[0],
    )
    span = timedelta(hours=hours)
    best = {"tokens": 0, "cost": 0.0, "start": None}
    lo, tok, cost = 0, 0, 0.0
    for t, k, c in items:
        tok += k
        cost += c
        while t - items[lo][0] >= span:
            tok -= items[lo][1]
            cost -= items[lo][2]
            lo += 1
        if tok > best["tokens"]:
            best = {"tokens": tok, "cost": round(cost, 6),
                    "start": items[lo][0].isoformat(timespec="seconds")}
    return best


def recent_buckets(records, now):
    """Bucket the last RECENT_SPAN_H hours so the browser can roll its own window.

    The freshness cache serves the same bytes until a transcript changes, so a
    total computed here would freeze while the clock kept moving. Sending
    per-minute buckets instead lets the panel re-sum the trailing five hours on
    every tick without the server touching the disk again.
    """
    t0 = (now - timedelta(hours=RECENT_SPAN_H)).replace(second=0, microsecond=0)
    acc = {}
    for r in records:
        if r["dt"] < t0:
            continue
        i = int((r["dt"] - t0).total_seconds() // BUCKET_S)
        a = acc.setdefault(i, [0, 0, 0, 0, 0.0, 0])
        a[0] += r["in"]
        a[1] += r["cw5"] + r["cw1"]
        a[2] += r["cr"]
        a[3] += r["out"]
        a[4] += r["cost"]
        a[5] += 1
    rows = [[i, a[0], a[1], a[2], a[3], round(a[4], 6), a[5]]
            for i, a in sorted(acc.items())]
    return t0, rows


def recent_payload(records, quota_reset, now=None):
    now = now or datetime.now().astimezone()
    t0, buckets = recent_buckets(records, now)
    return {
        "hours": WINDOW_H,
        "bucket_s": BUCKET_S,
        "t0": t0.isoformat(timespec="seconds"),
        "buckets": buckets,
        "peak": peak_window(records),
        # Authoritative when present: Claude Code records the real reset instant
        # on a request the server actually rate-limited. Rare, and only useful
        # while it is still in the future, but it beats any reconstruction.
        "limit_reset": quota_reset,
    }


def build(root=ROOT):
    requests, files, quota_reset = scan(root)

    models, projects, sessions, days = {}, {}, {}, {}

    def idx(table, value):
        if value not in table:
            table[value] = len(table)
        return table[value]

    rows = []
    unpriced = {}
    for r in requests.values():
        dt = r["dt"]
        day = dt.strftime("%Y-%m-%d")
        rate = rates_for(r["model"], day, r["speed"])
        if rate is None:
            unpriced[r["model"]] = unpriced.get(r["model"], 0) + 1
            cost = 0.0
        else:
            in_r, out_r = rate
            cost = (
                r["in"] * in_r
                + r["cw5"] * in_r * CACHE_WRITE_5M
                + r["cw1"] * in_r * CACHE_WRITE_1H
                + r["cr"] * in_r * CACHE_READ
                + r["out"] * out_r
            ) / 1_000_000.0

        r["cost"] = cost
        rows.append([
            idx(days, day),
            dt.hour,
            dt.weekday(),              # 0 = Monday
            idx(models, r["model"]),
            idx(projects, r["project"]),
            idx(sessions, r["session"]),
            r["side"],
            r["in"],
            r["cw5"] + r["cw1"],
            r["cr"],
            r["out"],
            r["think"],
            round(cost, 6),
        ])

    def keys_in_order(table):
        return [k for k, _ in sorted(table.items(), key=lambda kv: kv[1])]

    return {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": files,
        "requests": len(rows),
        "days": keys_in_order(days),
        "models": keys_in_order(models),
        "projects": keys_in_order(projects),
        "sessions": keys_in_order(sessions),
        "rows": rows,
        "recent": recent_payload(requests.values(), quota_reset),
        "unpriced": unpriced,
        "pricing": {m: {k: v for k, v in p.items()} for m, p in PRICING.items()},
    }


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------

def human(n):
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= size:
            return "%.1f%s" % (n / size, unit)
    return str(int(n))


def summary(data):
    rows = data["rows"]
    tot = [0, 0, 0, 0]
    cost = 0.0
    per_model = {}
    for r in rows:
        tot[0] += r[7]; tot[1] += r[8]; tot[2] += r[9]; tot[3] += r[10]
        cost += r[12]
        m = data["models"][r[3]]
        acc = per_model.setdefault(m, [0, 0.0])
        acc[0] += r[7] + r[8] + r[9] + r[10]
        acc[1] += r[12]
    total = sum(tot)
    out = []
    out.append("Claude Code usage — %d requests across %d transcripts, %d days"
               % (data["requests"], data["files"], len(data["days"])))
    out.append("  input %s | cache write %s | cache read %s | output %s"
               % (human(tot[0]), human(tot[1]), human(tot[2]), human(tot[3])))
    out.append("  total %s tokens | API-rate equivalent $%.2f" % (human(total), cost))
    out.append("")
    rec = data.get("recent")
    if rec:
        now = datetime.now().astimezone()
        t0 = datetime.fromisoformat(rec["t0"])
        cut = (now - timedelta(hours=rec["hours"]) - t0).total_seconds() / rec["bucket_s"]
        tok = cost = n = 0
        for b in rec["buckets"]:
            if b[0] >= cut:
                tok += b[1] + b[2] + b[3] + b[4]; cost += b[5]; n += b[6]
        out.append("  last %dh: %s tokens | $%.2f | %d requests"
                   % (rec["hours"], human(tok), cost, n))
        if rec.get("limit_reset"):
            reset = datetime.fromisoformat(rec["limit_reset"])
            if reset > now:
                mins = int((reset - now).total_seconds() // 60)
                out.append("  plan window resets %s (in %dh %02dm), per a rate-limited request"
                           % (reset.strftime("%H:%M"), mins // 60, mins % 60))
        out.append("")
    out.append("  %-24s %10s %12s" % ("model", "tokens", "cost"))
    for m, (t, c) in sorted(per_model.items(), key=lambda kv: -kv[1][1]):
        out.append("  %-24s %10s %11.2f" % (m, human(t), c))
    if data["unpriced"]:
        out.append("")
        out.append("  unpriced models (counted as $0): %s"
                   % ", ".join("%s x%d" % (k, v) for k, v in data["unpriced"].items()))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class Cache:
    def __init__(self, root):
        self.root = root
        self.payload = None
        self.stamp = None

    def fingerprint(self):
        newest, count, size = 0.0, 0, 0
        for path in iter_transcripts(self.root):
            try:
                st = os.stat(path)
            except OSError:
                continue
            count += 1
            size += st.st_size
            if st.st_mtime > newest:
                newest = st.st_mtime
        return (newest, count, size)

    def get(self, force=False):
        fp = self.fingerprint()
        if force or self.payload is None or fp != self.stamp:
            t0 = time.time()
            data = build(self.root)
            data["scan_ms"] = int((time.time() - t0) * 1000)
            self.payload = json.dumps(data).encode()
            self.stamp = fp
        return self.payload


def host_of(header):
    """Hostname from a Host header, minus the port. Handles [::1]:8787."""
    h = (header or "").strip()
    if h.startswith("["):
        return h[: h.find("]") + 1] if "]" in h else h
    return h.split(":")[0]


def serve(host, port, root, open_browser):
    cache = Cache(root)
    # A loopback bind alone does not stop DNS rebinding: a page on the open web
    # can point its own hostname at 127.0.0.1 and read /api/data, which lists
    # every project path and session id. Pinning the Host header shuts that off.
    allowed_hosts = {"127.0.0.1", "localhost", "::1", "[::1]", host}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if host_of(self.headers.get("Host")) not in allowed_hosts:
                self.send_error(403, "Forbidden")
                return
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                try:
                    with open(os.path.join(HERE, "panel.html"), "rb") as fh:
                        self._send(fh.read(), "text/html; charset=utf-8")
                except OSError:
                    self.send_error(500, "panel.html not found next to ccusage.py")
                return
            if path == "/api/data":
                force = "refresh=1" in self.path
                try:
                    self._send(cache.get(force), "application/json")
                except Exception:  # keep the server alive on a bad transcript
                    import traceback
                    traceback.print_exc()
                    self.send_error(500, "scan failed")
                return
            self.send_error(404)

        def log_message(self, *_args):
            pass

    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d/" % (host, port)
    home = os.path.expanduser("~")
    shown = root.replace(home, "~", 1) if root.startswith(home) else root
    print("Claude Code usage panel -> %s   (ctrl-c to stop)" % url)
    print("Reading %s" % shown)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("\n  WARNING: bound to %s, not loopback." % host)
        print("  There is no authentication. Anyone who can reach this port can")
        print("  read your project paths, session ids and usage figures.\n")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    ap = argparse.ArgumentParser(description="Claude Code token usage panel")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--root", default=ROOT, help="Claude Code projects directory")
    ap.add_argument("--json", action="store_true", help="dump dataset as JSON")
    ap.add_argument("--summary", action="store_true", help="print a text summary")
    ap.add_argument("--open", action="store_true", help="open a browser")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        sys.exit("No transcripts directory at %s" % args.root)

    if args.json:
        json.dump(build(args.root), sys.stdout)
        return
    if args.summary:
        print(summary(build(args.root)))
        return
    serve(args.host, args.port, args.root, args.open)


if __name__ == "__main__":
    main()
