#!/usr/bin/env bash
# Persist the SQLite state between CI runs on an orphan `state` branch.
#
# The branch is force-pushed as a single fresh commit every time, so the state
# history never grows and the code history stays free of binary churn.
set -euo pipefail

DB_PATH="data/listings.db"
BRANCH="state"

restore() {
  mkdir -p "$(dirname "$DB_PATH")"
  if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    git fetch --depth=1 origin "$BRANCH:refs/remotes/origin/$BRANCH" >/dev/null 2>&1
    if git show "origin/$BRANCH:listings.db" > "$DB_PATH" 2>/dev/null; then
      echo "state: restored $(du -h "$DB_PATH" | cut -f1) database"
      return
    fi
  fi
  rm -f "$DB_PATH"
  echo "state: no prior database — this run will seed silently"
}

save() {
  if [ ! -f "$DB_PATH" ]; then
    echo "state: nothing to save"
    return
  fi
  git config user.name "lausanne-watcher[bot]"
  git config user.email "actions@github.com"

  cp "$DB_PATH" /tmp/listings.db
  git checkout --orphan tmp-state >/dev/null 2>&1
  git rm -rf --cached . >/dev/null 2>&1 || true
  find . -maxdepth 1 ! -name . ! -name .git ! -name listings.db -exec rm -rf {} + 2>/dev/null || true
  cp /tmp/listings.db ./listings.db
  git add -f listings.db
  git commit -q -m "state: $(date -u +%FT%TZ)"
  git push -f -q origin "tmp-state:$BRANCH"
  echo "state: saved"
}

case "${1:-}" in
  restore) restore ;;
  save)    save ;;
  *) echo "usage: $0 {restore|save}" >&2; exit 1 ;;
esac
