#!/bin/bash
# Extract skola's matematik + teknik berakning DIAGRAM images for the Lärbild
# review-library import (sibling of the SVG lanes). Runs ON VM500 (the only host
# with both the skola DB and the rendered PNGs), producing a manifest + a PNG dir
# that import_skola_diagrams.py (run on Sibbis) consumes.
#
# WHY a separate extract step: Lärbild runs on Sibbis and serves from an NFS
# storage root there; the skola DB + the concept-image PNGs live on VM500 and are
# NOT reachable from Sibbis. So we extract on VM500, ship the small (~7.4 MB)
# bundle to Sibbis, and import there into the live library + STORAGE_DIR.
#
# Output (under OUT, default /tmp/skola-diagrams):
#   manifest.json  — one record per image_url row: question_id, subject, strand,
#                    area_slug, area_name, image_file, gate_status, level, prompt,
#                    image_url.
#   png/           — every referenced PNG, copied verbatim (basename = image_file).
#
# Usage (on VM500):
#   bash extract_skola_diagrams.sh [OUT_DIR]
set -euo pipefail

OUT="${1:-/tmp/skola-diagrams}"
DB_CONTAINER="${SKOLA_DB_CONTAINER:-b03d7f91b635_skola-db}"
CONCEPT_ROOT="${SKOLA_CONCEPT_ROOT:-/home/app/projects/skola/frontend/public/concept-images}"

mkdir -p "$OUT/png"

echo "[extract] querying skola DB ($DB_CONTAINER) for matematik+teknik diagram rows..."
# One JSON array, one object per question_bank row that has an image_url and whose
# subject is matematik or teknik. area_slug/strand/name come from the joined area.
docker exec -i "$DB_CONTAINER" psql -U skola -d skola -tA -c "
SELECT json_agg(json_build_object(
    'question_id', qb.id,
    'subject',     a.subject_id,
    'strand',      a.strand,
    'area_slug',   a.slug,
    'area_name',   a.name,
    'image_url',   qb.image_url,
    'image_file',  regexp_replace(qb.image_url, '^.*/', ''),
    'gate_status', qb.gate_status,
    'level',       qb.level,
    'prompt',      qb.prompt
))
FROM question_bank qb
JOIN lgr22_areas a ON a.id = qb.area_id
WHERE qb.image_url IS NOT NULL
  AND a.subject_id IN ('matematik','teknik');
" > "$OUT/manifest.json"

COUNT=$(python3 -c "import json;print(len(json.load(open('$OUT/manifest.json'))))")
echo "[extract] manifest.json: $COUNT records"

echo "[extract] copying PNGs into $OUT/png/ ..."
copied=0; missing=0
# Pull each image_file basename out of the manifest and copy from CONCEPT_ROOT.
# image_url is /concept-images/<bucket>/<file>; resolve it under CONCEPT_ROOT.
while IFS=$'\t' read -r url file; do
  [ -z "$file" ] && continue
  # The URL after the /concept-images/ prefix is the path under CONCEPT_ROOT.
  rel="${url#/concept-images/}"
  src="$CONCEPT_ROOT/$rel"
  if [ -f "$src" ]; then
    cp -f "$src" "$OUT/png/$file"
    copied=$((copied+1))
  else
    echo "[extract] MISSING: $src" >&2
    missing=$((missing+1))
  fi
done < <(python3 -c "
import json
for r in json.load(open('$OUT/manifest.json')):
    print(r['image_url'] + chr(9) + r['image_file'])
")

echo "[extract] copied=$copied missing=$missing → $OUT/png/"
echo "[extract] done. Ship $OUT to Sibbis, then run import_skola_diagrams.py there."
