#!/usr/bin/env bash
# build-css.sh — reproducible local Tailwind build for biozone_web.
#
# Regenerates biozone_web/public/css/app.<content-sha>.css from
# tailwind.config.js + templates, and points every page at the new file.
# Idempotent: rebuilding unchanged sources yields the same hash and
# leaves templates untouched. Clean rebuild: rm -rf node_modules, rerun.
#
# Usage: bash scripts/build-css.sh   (from the biozone_web app root)
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_ROOT"

[ -f package.json ] || { echo "ERROR: package.json missing" >&2; exit 1; }
[ -f tailwind.config.js ] || { echo "ERROR: tailwind.config.js missing" >&2; exit 1; }
if [ ! -d node_modules ]; then
  npm install --no-audit --no-fund
fi

CSS_SRC="css-src/input.css"
OUT_DIR="biozone_web/public/css"
[ -f "$CSS_SRC" ] || { echo "ERROR: $CSS_SRC missing" >&2; exit 1; }
mkdir -p "$OUT_DIR"

npx tailwindcss -i "$CSS_SRC" -o "$OUT_DIR/app.css"
HASH="$(sha256sum "$OUT_DIR/app.css" | cut -c1-8)"
mv -f "$OUT_DIR/app.css" "$OUT_DIR/app.$HASH.css"

# Point templates at the hashed file (only prior hashed names; nothing else).
grep -rlE "/assets/biozone_web/css/app\.[0-9a-f]+\.css" biozone_web/www biozone_web/templates \
  | while read -r f; do
    sed -i -E "s|/assets/biozone_web/css/app\.[0-9a-f]+\.css|/assets/biozone_web/css/app.$HASH.css|g" "$f"
  done

# Fail closed: every CDN-migrated page must reference the built file.
# (not-available.html is intentionally unstyled and out of scope.)
MISSING="$(grep -rL "app\.$HASH\.css" biozone_web/www --include='*.html' | grep -v 'not-available\.html' | tr '\n' ' ')"
[ -z "$MISSING" ] || { echo "ERROR: pages missing hashed css link: $MISSING" >&2; exit 1; }

echo "CSS built: biozone_web/public/css/app.$HASH.css"
