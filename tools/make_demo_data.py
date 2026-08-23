#!/usr/bin/env python3
"""
Generate a synthetic Claude Code transcript tree.

Used to produce the screenshots in docs/ without publishing anything from a
real machine, and useful as a fixture when changing the parser. Output is
deterministic for a given --seed.

    python3 tools/make_demo_data.py --out /tmp/demo-projects
    python3 ccusage.py --root /tmp/demo-projects

The generated files mirror the real transcript shape, including the detail
that makes this tool non-trivial: one API response is written as SEVERAL
JSONL lines, one per content block, each repeating the same usage object
except output_tokens, which grows as the response streams.
"""

import argparse
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

# Invented projects. Nothing here corresponds to a real directory.
PROJECTS = [
    ("/home/dev/projects/acme-web",        "main",     0.30),
    ("/home/dev/projects/acme-api",        "develop",  0.22),
    ("/home/dev/projects/data-pipeline",   "main",     0.16),
    ("/home/dev/projects/mobile-app",      "feature",  0.12),
    ("/home/dev/projects/infra",           "main",     0.10),
    ("/home/dev/scratch",                  "main",     0.10),
]

MODELS = [
    ("claude-opus-5",             0.46),
    ("claude-sonnet-5",           0.40),
    ("claude-haiku-4-5-20251001", 0.09),
    ("claude-opus-4-8",           0.05),
]

# Hour-of-day weights: a working day with an afternoon peak and a light evening.
HOURS = ([0.2] * 7) + [1, 3, 6, 8, 9, 7, 5, 8, 9, 9, 8, 6, 4, 3, 2, 2, 1.5, 1]


def weighted(rng, pairs):
    total = sum(w for _, w in pairs)
    r = rng.random() * total
    acc = 0.0
    for value, w in pairs:
        acc += w
        if r <= acc:
            return value
    return pairs[-1][0]


def encode_cwd(cwd):
    """Claude Code encodes the project path into the directory name."""
    return cwd.replace("/", "-")


def gen(out, days, seed):
    rng = random.Random(seed)
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    files = {}
    requests = lines = 0

    for day_offset in range(days, -1, -1):
        day = today - timedelta(days=day_offset)
        # Quiet weekends, and some days off entirely.
        weekday = day.weekday()
        load = 1.0 if weekday < 5 else 0.35
        # Always leave activity on the most recent day so the "Today" tile
        # in a screenshot is populated.
        if day_offset > 0 and rng.random() < (0.10 if weekday < 5 else 0.45):
            continue

        for _ in range(rng.randint(1, 3)):  # sessions that day
            cwd = weighted(rng, [(p, w) for p, _b, w in PROJECTS])
            branch = next(b for p, b, _w in PROJECTS if p == cwd)
            session = str(uuid.UUID(int=rng.getrandbits(128)))
            model = weighted(rng, MODELS)

            hour = weighted(rng, list(enumerate(HOURS)))
            ts = day + timedelta(hours=hour, minutes=rng.randint(0, 59))

            path = os.path.join(out, encode_cwd(cwd), session + ".jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = files.get(path) or open(path, "w")
            files[path] = fh

            turns = int(rng.triangular(3, 60, 14) * load)
            # The cached prefix grows through a session and is re-read each turn.
            cached = rng.randint(6000, 20000)

            for _turn in range(turns):
                ts += timedelta(seconds=rng.randint(8, 240))
                sidechain = rng.random() < 0.18
                turn_model = "claude-haiku-4-5-20251001" if (
                    sidechain and rng.random() < 0.3) else model

                cache_read = cached
                cache_write = rng.choice([0, 0, 0, rng.randint(400, 9000)])
                cached += cache_write + rng.randint(300, 2500)
                inp = rng.randint(1, 6)
                out_total = int(rng.triangular(40, 4000, 500))
                thinking = int(out_total * rng.uniform(0, 0.45))

                req = "req_%s" % uuid.UUID(int=rng.getrandbits(128)).hex[:24]
                msg = "msg_%s" % uuid.UUID(int=rng.getrandbits(128)).hex[:24]

                # One response -> several lines, one per content block.
                blocks = ["thinking"] if thinking else []
                blocks += rng.choice([["text"], ["tool_use"], ["text", "tool_use"],
                                      ["tool_use", "tool_use"]])
                running = 0
                for n, block in enumerate(blocks):
                    last = n == len(blocks) - 1
                    running = out_total if last else min(
                        out_total, running + max(1, out_total // (len(blocks) + 1)))
                    rec = {
                        "parentUuid": str(uuid.UUID(int=rng.getrandbits(128))),
                        "isSidechain": sidechain,
                        "message": {
                            "model": turn_model,
                            "id": msg,
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": block}],
                            "stop_reason": "tool_use" if block == "tool_use" else "end_turn",
                            "usage": {
                                "input_tokens": inp,
                                "cache_creation_input_tokens": cache_write,
                                "cache_read_input_tokens": cache_read,
                                "output_tokens": running,
                                "output_tokens_details": {"thinking_tokens": thinking if last else 0},
                                "service_tier": "standard",
                                "cache_creation": {
                                    "ephemeral_1h_input_tokens": cache_write if rng.random() < 0.25 else 0,
                                    "ephemeral_5m_input_tokens": 0 if rng.random() < 0.25 else cache_write,
                                },
                                "speed": "standard",
                            },
                        },
                        "requestId": req,
                        "type": "assistant",
                        "uuid": str(uuid.UUID(int=rng.getrandbits(128))),
                        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "sessionId": session,
                        "cwd": cwd,
                        "gitBranch": branch,
                        "version": "2.1.0",
                        "userType": "external",
                    }
                    fh.write(json.dumps(rec) + "\n")
                    lines += 1
                requests += 1

    for fh in files.values():
        fh.close()
    return len(files), requests, lines


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="/tmp/tokens-monitor-demo")
    ap.add_argument("--days", type=int, default=34)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    n_files, requests, lines = gen(args.out, args.days, args.seed)
    print("wrote %d transcripts to %s" % (n_files, args.out))
    print("  %d API responses across %d JSONL lines (%.2fx inflation if summed naively)"
          % (requests, lines, lines / float(requests)))
    print("\n  python3 ccusage.py --root %s" % args.out)


if __name__ == "__main__":
    main()
