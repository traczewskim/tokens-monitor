#!/usr/bin/env bash
#
# Shared leak checks for the tokens-monitor git hooks.
#
# This repo is public and the tool reads private Claude Code transcripts, so
# these checks refuse to let real paths, session state, transcripts, or
# screenshots of real data reach it.
#
# Sourced by .githooks/pre-commit (staged changes) and .githooks/pre-push
# (the commit range being published).

RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; DIM=$'\033[2m'; OFF=$'\033[0m'

guard_fail=0
say()  { printf '%s\n' "$*" >&2; }
bad()  { say "${RED}BLOCKED${OFF} $*"; guard_fail=1; }
warn() { say "${YEL}warn${OFF}    $*"; }
ok()   { say "${GRN}ok${OFF}      $*"; }

# guard_run <files> <diff> <blob-prefix>
#   files       newline-separated paths added/changed
#   diff        unified diff text to scan
#   blob-prefix ":" for the index, or "<sha>:" for a commit
guard_run() {
  local files="$1" diff="$2" blobref="$3"
  [ -z "$files" ] && return 0

  # -- 1. File-type policy ---------------------------------------------------
  local f
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      *.jsonl)   bad "$f — Claude Code transcripts contain full conversation text." ;;
      .claude/*) bad "$f — local agent/session state." ;;
      *.json)
        case "$f" in
          */*) : ;;
          *)   bad "$f — looks like a \`--json\` dataset dump (real paths + session ids)." ;;
        esac ;;
    esac
  done <<< "$files"

  # -- 2. Local identity in added lines --------------------------------------
  # The username is resolved at runtime; never hardcode it, this file is public.
  local me added offenders
  me=$(id -un 2>/dev/null || echo "")
  if [ -n "$diff" ]; then
    added=$(printf '%s\n' "$diff" | grep '^+' | grep -v '^+++')

    if [ -n "$me" ] && [ "$me" != "dev" ]; then
      if printf '%s\n' "$added" | grep -qE "(/home/|/Users/)$me\b"; then
        bad "content contains your home path (/home/$me or /Users/$me)."
      elif printf '%s\n' "$added" | grep -qE "\b$me\b"; then
        warn "content mentions your username ('$me') — confirm it is intentional."
      fi
    fi

    # Any real home path, not just yours. /home/dev is the synthetic value
    # used by tools/make_demo_data.py and the docs.
    if printf '%s\n' "$added" | grep -oE "(/home/|/Users/)[A-Za-z0-9._-]+" \
        | grep -vE "(/home/dev)$" | grep -q .; then
      offenders=$(printf '%s\n' "$added" | grep -oE "(/home/|/Users/)[A-Za-z0-9._-]+" \
        | grep -vE "(/home/dev)$" | sort -u | tr '\n' ' ')
      bad "content contains real home paths: $offenders"
    fi
  fi

  # -- 3. Screenshots must be rendered from synthetic data -------------------
  # .gitignore does not apply to tracked files, and the documented
  # regeneration commands write straight back to these paths, so verify the
  # pixels rather than trusting the filename.
  local shots tmp shot txt markers verified=0
  shots=$(printf '%s\n' "$files" | grep -E '^docs/screenshot.*\.png$' || true)
  if [ -n "$shots" ]; then
    if ! command -v tesseract >/dev/null 2>&1; then
      bad "screenshots staged but tesseract is not installed, so their contents
        cannot be verified. Install tesseract-ocr, or use --no-verify only if
        you rendered them via tools/make_demo_data.py."
    else
      tmp=$(mktemp -d)
      while IFS= read -r shot; do
        [ -z "$shot" ] && continue
        # Fail closed: an image we cannot resolve is an image we cannot verify.
        if ! git show "${blobref}${shot}" > "$tmp/s.png" 2>/dev/null; then
          bad "$shot could not be resolved at '${blobref}' — cannot verify its
        contents, refusing rather than assuming it is safe."
          continue
        fi
        verified=$((verified + 1))
        tesseract "$tmp/s.png" "$tmp/out" --psm 6 >/dev/null 2>&1
        txt=$(cat "$tmp/out.txt" 2>/dev/null || echo "")

        markers=$(printf '%s' "$txt" | grep -oiE "acme|data-pipeline|mobile-app" | wc -l)
        if [ "$markers" -lt 3 ]; then
          bad "$shot does not look like it was rendered from synthetic data
        (found $markers synthetic project markers, expected 3+).
        Regenerate: python3 tools/make_demo_data.py --out /tmp/demo-projects
                    python3 ccusage.py --root /tmp/demo-projects --port 8790"
        fi
        if [ -n "$me" ] && [ "$me" != "dev" ] \
           && printf '%s' "$txt" | grep -qiE "\b$me\b|/home/$me"; then
          bad "$shot appears to show your real home path or username."
        fi
      done <<< "$shots"
      rm -rf "$tmp"
      # Only claim success for images actually OCR'd.
      if [ "$guard_fail" -eq 0 ] && [ "$verified" -gt 0 ]; then
        ok "screenshots verified as synthetic ($verified checked)"
      fi
    fi
  fi

  return 0
}
