#!/usr/bin/env bash
set -euo pipefail

if ! command -v vendir >/dev/null 2>&1; then
  echo "vendir is required. Install it from https://carvel.dev/vendir/ and rerun this script." >&2
  exit 1
fi

case "${1:-}" in
  "")
    vendir sync --chdir external-sources
    ;;
  --locked)
    # vendir refreshes descriptive git tags even when the commit SHA is locked.
    # Verify from a copy so upstream tag churn cannot dirty the committed lock.
    verification_lock=$(mktemp)
    trap 'rm -f "$verification_lock"' EXIT
    cp external-sources/vendir.lock.yml "$verification_lock"
    vendir sync --chdir external-sources --locked --lock-file "$verification_lock"
    ;;
  *)
    echo "Usage: $0 [--locked]" >&2
    exit 2
    ;;
esac
python3 scripts/import_external_catalog_sources.py
