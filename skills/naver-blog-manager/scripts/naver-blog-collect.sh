#!/usr/bin/env bash
# naver-blog-collect.sh <blog-id> [count] [options]
#
# Collect the latest N posts of a Naver blog → body md / image zip.
# Self-contained: uses collect-latest.mjs from this script's directory.
#
# Options:
#   --mode both|md|zip   output format (default both: body md + image zip)
#   --split <MB>         image zip split limit (e.g. 20 → parts of ≤20MB). Default 0 = one full zip
#   --single             one full zip (default, same as --split 0)
#
# Output (machine-readable): MD=<path>, ZIP=<path> (may be multiple lines)
set -euo pipefail

BLOG_ID=""
COUNT=10
MODE="both"
SPLIT="0"

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)   MODE="$2"; shift 2 ;;
    --split)  SPLIT="$2"; shift 2 ;;
    --single) SPLIT="0"; shift ;;
    -*)       echo "unknown option: $1" >&2; exit 2 ;;
    *)        if [ -z "$BLOG_ID" ]; then BLOG_ID="$1"; else COUNT="$1"; fi; shift ;;
  esac
done

[ -n "$BLOG_ID" ] || { echo "usage: naver-blog-collect.sh <blog-id> [count] [--mode both|md|zip] [--split MB]" >&2; exit 2; }
case "$MODE" in both|md|zip) ;; *) echo "--mode must be both|md|zip" >&2; exit 2 ;; esac

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -d "$TOOLS/node_modules/cheerio" ]; then
  echo "cheerio not installed — run first: (cd \"$TOOLS\" && npm i cheerio)" >&2
  exit 3
fi
OUTDIR="$HOME/data/naver-blog/${BLOG_ID}"

# 1) collect body text
BLOG_ID="$BLOG_ID" COUNT="$COUNT" OUT_DIR="$OUTDIR" node "$TOOLS/collect-latest.mjs" >&2

MD=$(ls -t "$OUTDIR"/*.md 2>/dev/null | grep -v -- '-body.md$' | head -1)
BASE=$(basename "$MD" .md)

echo "MD=$MD"

# body-only request: stop here (skip image work — faster)
if [ "$MODE" = "md" ]; then
  echo "---- result ----" >&2
  echo "body md : $MD ($(du -h "$MD" | cut -f1))" >&2
  exit 0
fi

# 2) download original images (wiping previous leftovers first — keeps zip and manifest in sync)
rm -rf "$OUTDIR/images"
python3 "$TOOLS/download-images.py" "$MD" >&2

# 3) build zip
ZIPDIR="$OUTDIR/zip"
rm -rf "$ZIPDIR"; mkdir -p "$ZIPDIR"
PREFIX="$ZIPDIR/${BASE}"

if [ "$MODE" = "zip" ]; then
  # images only: pack manifest + images/ (no body md)
  :
else
  # both: place the local-link body md at the zip root (links are relative to images/)
  python3 "$TOOLS/localize-md.py" "$MD" >&2
fi

if [ "$SPLIT" = "0" ]; then
  # single zip: both → body md + images/, zip → images/ only
  ZIP="$ZIPDIR/${BASE}.zip"
  if [ "$MODE" = "both" ]; then
    ( cd "$OUTDIR" && zip -q -9 "$ZIP" "${BASE}-body.md" -r images/ )
  else
    ( cd "$OUTDIR" && zip -q -9 -r "$ZIP" images/ )
  fi
  echo "ZIP=$ZIP"
else
  # split zip: each part holds a slice of images/ (the body md is sent separately)
  PACK_OUT=$(python3 "$TOOLS/pack-images.py" "$OUTDIR/images" "$PREFIX" "$SPLIT" 2>"$OUTDIR/.pack.err")
  sed 's/^/  /' "$OUTDIR/.pack.err" >&2
  echo "$PACK_OUT"
fi

echo "---- result ----" >&2
echo "body md : $MD ($(du -h "$MD" | cut -f1))" >&2
echo "images  : $(ls "$OUTDIR/images" | grep -vc 'manifest.csv\|-body.md$') files" >&2
echo "zip     : $(ls "$ZIPDIR"/*.zip 2>/dev/null | wc -l) file(s) ($(du -sh "$ZIPDIR" | cut -f1))" >&2
echo "failed  : $(grep -c 'FAIL' "$OUTDIR/images/manifest.csv" 2>/dev/null || echo 0)" >&2
