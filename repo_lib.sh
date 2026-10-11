#!/bin/bash
# Shared helpers for the box scripts (push_sentiment.sh, pull_state.sh, repo_flag.sh). Sourced, not run.
# The GitHub token is read from /workspace/trading/.github_token (GH_TOKEN=...) into the environment and handed to git
# via GIT_ASKPASS - it is never written to git config, URLs, logs or the repo clone.
BASE=/workspace/trading
REPO_DIR=${REPO_DIR:-/workspace/trading-repo}      # box clone of the repo (no secrets inside)
REPO_SLUG=oscarwy08/trading-dashboard
REMOTE="https://github.com/${REPO_SLUG}.git"
mkdir -p "$BASE/logs"

repo_auth() {
  [ -f "$BASE/.github_token" ] || { echo "ERROR: $BASE/.github_token missing" >&2; return 1; }
  set -a; . "$BASE/.github_token"; set +a
  [ -n "${GH_TOKEN:-}" ] || { echo "ERROR: GH_TOKEN empty" >&2; return 1; }
  ASKPASS="$BASE/.publish/askpass.sh"; mkdir -p "$BASE/.publish"
  printf '#!/bin/sh\ncase "$1" in Username*) echo "x-access-token" ;; *) echo "$GH_TOKEN" ;; esac\n' > "$ASKPASS"; chmod 700 "$ASKPASS"
  export GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 GH_TOKEN
}

G() { git -C "$REPO_DIR" -c credential.helper= -c user.name="box-ai-routine" -c user.email="box-ai-routine@users.noreply.github.com" "$@"; }

# clone (shallow) if missing, then hard-sync to origin/main. The clone never keeps local commits: Actions squashes
# (force-pushes) main from time to time, so we always reset instead of merging/rebasing.
repo_sync() {
  if [ ! -d "$REPO_DIR/.git" ]; then
    git -c credential.helper= clone -q --depth 1 --branch main "$REMOTE" "$REPO_DIR" || return 1
  fi
  G remote set-url origin "$REMOTE"
  G fetch -q --depth 1 origin main && G reset -q --hard origin/main && G clean -qfd -- data/sentiment
}

# refuse to push anything that contains the token / dashboard password
repo_leakcheck() {   # args: files (absolute)
  local PW; PW=$(cut -d: -f2- "$BASE/.dashboard_auth" 2>/dev/null | head -n1)
  for f in "$@"; do
    if grep -aqF -- "$GH_TOKEN" "$f" || { [ -n "$PW" ] && grep -aqF -- "$PW" "$f"; } || \
       grep -aqE -- '(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|BEGIN [A-Z ]*PRIVATE KEY)' "$f"; then
      echo "ABORT: sensitive content in $(basename "$f")" >&2; return 1
    fi
  done
}
