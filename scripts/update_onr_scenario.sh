#!/usr/bin/env bash

# Refresh the local ONR scenario tree from the shared Google Drive folder.
#
# Drive layout: one <scenario>.zip per scenario (each holding
# <scenario>/collision/** and <scenario>/non_collision/**) plus the unzipped
# asset_showcase/ folder. Local static/ and map/ trees are not on Drive and are
# never touched.
#
# Steps:
#   1. rclone copy the zips into a staging dir (unchanged zips are skipped).
#   2. rclone sync asset_showcase/ (replaced files go to the backup dir).
#   3. Per zip: integrity-test it, move the old collision/ and non_collision/
#      into the backup dir, then unzip, so scenarios removed upstream do not
#      linger locally.
#
# Overrides:
#   ONR_SCENARIO_DIR       local scenario tree  (default /data/ccu/sukaih/ONR/onr_scenario)
#   ONR_SCENARIO_ZIP_DIR   zip staging dir      (default /data/ccu/sukaih/ONR/onr_scenario_zips)
#   ONR_SCENARIO_BAK_DIR   backup dir           (default <scenario dir>.bak-YYYYmmdd-HHMM)
#   ONR_SCENARIO_RCLONE    rclone binary
#   ONR_SCENARIO_DRIVE_ID  Drive root folder id
#   ONR_SCENARIO_DRY_RUN=1 rclone --dry-run and print extraction plan only

set -euo pipefail

readonly ONR_ROOT="/data/ccu/sukaih/ONR"
readonly RCLONE="${ONR_SCENARIO_RCLONE:-$ONR_ROOT/onr_solution/yolo_weight/rclone-v1.75.1-linux-amd64/rclone}"
readonly DRIVE_ID="${ONR_SCENARIO_DRIVE_ID:-1HwxFtplpAfv_W4-Bum6Pzj5E_ktfk0Jk}"
readonly SCENARIO_DIR="${ONR_SCENARIO_DIR:-$ONR_ROOT/onr_scenario}"
readonly ZIP_DIR="${ONR_SCENARIO_ZIP_DIR:-$ONR_ROOT/onr_scenario_zips}"
readonly BAK_DIR="${ONR_SCENARIO_BAK_DIR:-$SCENARIO_DIR.bak-$(date +%Y%m%d-%H%M)}"
readonly DRY_RUN="${ONR_SCENARIO_DRY_RUN:-0}"

for tool in "$RCLONE" unzip; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 1; }
done

rclone_args=(--drive-root-folder-id "$DRIVE_ID" --progress --transfers 8)
[[ "$DRY_RUN" == 1 ]] && rclone_args+=(--dry-run)

echo "== 1/3 downloading scenario zips -> $ZIP_DIR"
"$RCLONE" copy gdrive: "$ZIP_DIR" --include "/*.zip" "${rclone_args[@]}"

echo "== 2/3 syncing asset_showcase -> $SCENARIO_DIR/asset_showcase"
"$RCLONE" sync gdrive:asset_showcase "$SCENARIO_DIR/asset_showcase" \
  --backup-dir "$BAK_DIR/asset_showcase" "${rclone_args[@]}"

echo "== 3/3 extracting zips into $SCENARIO_DIR (backup: $BAK_DIR)"
shopt -s nullglob
zips=("$ZIP_DIR"/*.zip)
if [[ ${#zips[@]} -eq 0 ]]; then
  [[ "$DRY_RUN" == 1 ]] && { echo "dry run: no zips staged yet"; exit 0; }
  echo "no zips found in $ZIP_DIR" >&2
  exit 1
fi

for zip in "${zips[@]}"; do
  scenario="$(basename "$zip" .zip)"
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "dry run: would replace $SCENARIO_DIR/$scenario/{collision,non_collision} from $zip"
    continue
  fi
  echo "-- $scenario"
  unzip -tq "$zip" >/dev/null || { echo "corrupt zip, leaving $scenario untouched: $zip" >&2; exit 1; }
  mkdir -p "$BAK_DIR/$scenario"
  for sub in collision non_collision; do
    if [[ -d "$SCENARIO_DIR/$scenario/$sub" ]]; then
      mv "$SCENARIO_DIR/$scenario/$sub" "$BAK_DIR/$scenario/"
    fi
  done
  if ! unzip -q -o "$zip" -d "$SCENARIO_DIR"; then
    echo "unzip failed for $zip; previous data is in $BAK_DIR/$scenario" >&2
    exit 1
  fi
done

echo "done; replaced data backed up in $BAK_DIR"
