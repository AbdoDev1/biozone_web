#!/usr/bin/env bash
# check-frontend.sh — closed-loop frontend guard for biozone_web.
#
# Fails (exit 1) on: Play CDN references, inline tailwind.config blocks,
# www pages without the local CSS link (except the documented allowlist),
# dangling fingerprints, build failure, post-rebuild drift (generated CSS
# or links out of sync — commit them), or unscannable dynamic class
# assembly. Exit 0 means shippable. No writes except the rebuild itself.
#
# Usage: bash scripts/check-frontend.sh   (from the biozone_web app root)
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_ROOT"

fail=0
say() { printf '%s\n' "$*"; }
err() { printf 'ERROR: %s\n' "$*" >&2; fail=1; }

# 1. No Play CDN anywhere in shipped templates/pages.
if grep -rn "cdn.tailwindcss.com" biozone_web/www biozone_web/templates 2>/dev/null; then
  err "cdn.tailwindcss.com reference found (use the local build)"
else
  say "ok: no cdn.tailwindcss.com"
fi

# 2. No inline tailwind.config blocks in HTML (the central file owns config).
if grep -rnE "tailwind\.config[\"']?\s*=" biozone_web/www biozone_web/templates --include='*.html' 2>/dev/null; then
  err "inline tailwind.config block found (use tailwind.config.js)"
else
  say "ok: no inline tailwind.config"
fi

# 3. Every www page links the local CSS (allowlist: intentionally unstyled).
missing=""
while IFS= read -r f; do
  case "$f" in *not-available.html) continue ;; esac
  grep -qE "/assets/biozone_web/css/app\.[0-9a-f]+\.css" "$f" || missing="$missing $f"
done < <(find biozone_web/www -name '*.html')
if [ -n "$missing" ]; then
  err "pages without local css link:$missing"
else
  say "ok: all pages link local css"
fi

# 4. Every referenced fingerprint resolves to a real file.
bad=""
for ref in $(grep -rhoE "app\.[0-9a-f]+\.css" biozone_web/www biozone_web/templates | sort -u); do
  [ -f "biozone_web/public/css/$ref" ] || bad="$bad $ref"
done
if [ -n "$bad" ]; then
  err "dangling css fingerprints:$bad"
else
  say "ok: fingerprints resolve"
fi

# 5. Rebuild must succeed (also repoints links when the hash moves).
if ! bash scripts/build-css.sh; then
  err "build-css.sh failed"
fi

# 6. Post-rebuild drift: generated css + links must already be in sync.
if [ -n "$(git status --porcelain -- biozone_web/public/css biozone_web/www biozone_web/templates)" ]; then
  err "rebuild drift: generated css/links differ from tree — commit them"
  git status --porcelain -- biozone_web/public/css biozone_web/www biozone_web/templates | head -5 >&2
else
  say "ok: rebuild in sync"
fi

# 7. Unscannable dynamic class assembly. Static literals (incl. ternaries
# of literals, e.g. customers.html toast/badges) pass: Tailwind sees them.
# Only genuinely computed names fail.
python3 - biozone_web/www biozone_web/templates <<'PYEOF' || err "unscannable dynamic class assembly (use static literals or a documented safelist)"
import glob
import re
import sys

roots = sys.argv[1:]
bad = []
for root in roots:
    for f in glob.glob(root + "/**/*.html", recursive=True):
        for i, ln in enumerate(open(f, encoding="utf-8"), 1):
            s = ln.strip()
            has_class = bool(re.search(r"class", s, re.IGNORECASE))
            # 7a. template-literal interpolation in class context
            if has_class and "${" in s and "`" in s:
                bad.append(f"{f}:{i}: interpolated class")
            # 7b. utility-prefix fragment joined at runtime ('bg-' + x)
            if re.search(r"""['"](bg|text|border|ring|from|via|to|px|py|p|m|gap|rounded|w|h|max|min|flex|grid|hidden|block|absolute|relative|top|left|right|bottom|z|opacity|shadow|transition|duration|scale|rotate|translate|font|leading|cursor|overflow|inset)-['"]?\s*\+""", s):
                bad.append(f"{f}:{i}: prefix-fragment concat")
            # 7c. sole-identifier classList argument: classList.add(cls) where
            # the value is computed elsewhere (static literals and ternaries
            # like add(ok ? 'a' : 'b') pass: the quote or ? breaks the match).
            if re.search(r"classList\.(add|remove|toggle)\(\s*[A-Za-z_$][\w$]*\s*\)", s):
                bad.append(f"{f}:{i}: identifier classList argument")
            # 7d. bare-identifier className assignment without ternary
            m = re.search(r"className\s*=\s*([A-Za-z_$][\w$]*)\s*;", s)
            if m:
                bad.append(f"{f}:{i}: bare className assignment")
for b in bad:
    print("DYNAMIC-CLASS:", b)
sys.exit(1 if bad else 0)
PYEOF

if [ "$fail" = 0 ]; then
  say "ok: classes statically discoverable"
  say "FRONTEND-OK"
else
  echo "FRONTEND-FAIL" >&2
  exit 1
fi
