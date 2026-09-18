#!/usr/bin/env bash
#
# Randomly delete objects from an S3 bucket.
#
# Lists all objects in the bucket, then deletes a random subset selected either
# by a percentage of the total (-p) or by an absolute count (-n).
#
# Runs as a DRY RUN by default (prints what it would delete). Pass -x to
# actually delete, which also prompts for confirmation unless -y is given.
#
# Usage:
#   # Preview deleting 30% of objects (no changes made)
#   ./random_delete_objects.sh -b my-bucket -p 30
#
#   # Actually delete a random 5 objects
#   ./random_delete_objects.sh -b my-bucket -n 5 -x
#
#   # Delete 50% without the interactive prompt
#   ./random_delete_objects.sh -b my-bucket -p 50 -x -y
#
set -euo pipefail

REGION="us-west-2"

usage() {
  cat <<EOF
Randomly delete a percentage or count of objects from an S3 bucket.

Usage: $0 -b BUCKET (-p PERCENT | -n COUNT) [-x] [-y]

  -b BUCKET   Target S3 bucket name (required)
  -p PERCENT  Percentage of listed objects to delete (0-100)
  -n COUNT    Absolute number of objects to delete
  -x          Execute deletion. Without this flag the script is a dry run.
  -y          Skip the interactive confirmation prompt (only with -x)
  -h          Show this help

Exactly one of -p or -n must be provided.
EOF
}

BUCKET=""
PERCENT=""
COUNT=""
EXECUTE=0
ASSUME_YES=0

while getopts "b:p:n:xyh" opt; do
  case "$opt" in
    b) BUCKET="$OPTARG" ;;
    p) PERCENT="$OPTARG" ;;
    n) COUNT="$OPTARG" ;;
    x) EXECUTE=1 ;;
    y) ASSUME_YES=1 ;;
    h) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

if [[ -z "$BUCKET" ]]; then
  echo "Error: -b BUCKET is required." >&2
  usage
  exit 1
fi

if [[ -n "$PERCENT" && -n "$COUNT" ]]; then
  echo "Error: provide only one of -p PERCENT or -n COUNT, not both." >&2
  exit 1
fi

if [[ -z "$PERCENT" && -z "$COUNT" ]]; then
  echo "Error: provide one of -p PERCENT or -n COUNT." >&2
  usage
  exit 1
fi

if [[ -n "$PERCENT" ]]; then
  if ! [[ "$PERCENT" =~ ^[0-9]+$ ]] || (( PERCENT < 0 || PERCENT > 100 )); then
    echo "Error: -p PERCENT must be an integer between 0 and 100." >&2
    exit 1
  fi
fi

if [[ -n "$COUNT" ]]; then
  if ! [[ "$COUNT" =~ ^[0-9]+$ ]]; then
    echo "Error: -n COUNT must be a non-negative integer." >&2
    exit 1
  fi
fi

# List all object keys in the bucket (handles pagination via the CLI).
# Use a portable read loop instead of mapfile so this works on macOS's
# stock bash 3.2.
ALL_KEYS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && ALL_KEYS+=("$line")
done < <(
  aws s3api list-objects-v2 \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --query "Contents[].Key" \
    --output text 2>/dev/null | tr '\t' '\n'
)

TOTAL="${#ALL_KEYS[@]}"
if (( TOTAL == 0 )); then
  echo "Bucket s3://${BUCKET}/ has no objects. Nothing to delete."
  exit 0
fi

# Resolve how many to delete.
if [[ -n "$COUNT" ]]; then
  N_TO_DELETE="$COUNT"
  if (( N_TO_DELETE > TOTAL )); then
    N_TO_DELETE="$TOTAL"
  fi
else
  # floor(TOTAL * PERCENT / 100)
  N_TO_DELETE=$(( TOTAL * PERCENT / 100 ))
fi

if (( N_TO_DELETE == 0 )); then
  echo "Selection resolved to 0 objects out of ${TOTAL}. Nothing to delete."
  exit 0
fi

# Randomly shuffle the keys and take the first N.
# awk prefixes each line with a random sort key, we sort on it, then strip it.
# This avoids depending on `shuf` (GNU coreutils), which isn't on stock macOS.
VICTIMS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && VICTIMS+=("$line")
done < <(
  printf '%s\n' "${ALL_KEYS[@]}" \
    | awk 'BEGIN { srand() } { printf "%.17f\t%s\n", rand(), $0 }' \
    | sort -n \
    | cut -f2- \
    | head -n "$N_TO_DELETE"
)

echo "Bucket s3://${BUCKET}/ contains ${TOTAL} object(s)."
echo "Randomly selected ${N_TO_DELETE} object(s) for deletion:"
for k in "${VICTIMS[@]}"; do
  echo "  - ${k}"
done

if (( EXECUTE == 0 )); then
  echo ""
  echo "DRY RUN: no objects were deleted. Re-run with -x to delete these objects."
  exit 0
fi

if (( ASSUME_YES == 0 )); then
  echo ""
  read -r -p "Type 'delete' to permanently delete ${N_TO_DELETE} object(s) from s3://${BUCKET}/: " confirm
  confirm="$(printf '%s' "$confirm" | tr '[:upper:]' '[:lower:]')"
  if [[ "$confirm" != "delete" ]]; then
    echo "Aborted. No objects were deleted."
    exit 0
  fi
fi

deleted=0
for k in "${VICTIMS[@]}"; do
  if aws s3api delete-object \
      --bucket "$BUCKET" \
      --key "$k" \
      --region "$REGION" >/dev/null 2>&1; then
    deleted=$((deleted + 1))
  else
    echo "  Failed to delete ${k}" >&2
  fi
done

echo ""
echo "Done. Deleted ${deleted}/${N_TO_DELETE} object(s) from s3://${BUCKET}/"
