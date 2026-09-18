#!/usr/bin/env bash
# Replay the CreateJob Lambda against an already-produced run, without waiting
# for a new S3 Inventory. CreateJob is normally triggered when ManifestMaker
# writes manifests/combined/<run_id>/_manifests_ready.json to the solution
# bucket. This script synthesizes that same S3 event and invokes the function
# directly, after clearing the run's idempotency lock so the invocation is not
# skipped as a duplicate.
#
# All the artifacts the function needs (combined manifests, the trigger file,
# and the recorded Athena count-query execution IDs) already exist from the
# original run. Athena keeps query results for 45 days, so the recorded
# execution IDs re-fetch fine.
#
# Usage:
#   ./replay_createjob.sh -b SOLUTION_BUCKET -r RUN_ID [-f FUNCTION_NAME] [-k]
#
#   -b  Solution bucket name (required)
#   -r  Run ID, e.g. 006AADB98115CD67C1 (required)
#   -f  CreateJob function name (default: auto-event-hold-release-createjob)
#   -k  Keep the idempotency lock (do NOT delete it). Default is to delete it.
#   -h  Help
set -euo pipefail

REGION="us-west-2"
FUNCTION_NAME="auto-event-hold-release-createjob"
DELETE_LOCK=1
SOLUTION_BUCKET=""
RUN_ID=""

usage() { sed -n '2,25p' "$0"; }

while getopts "b:r:f:kh" opt; do
  case "$opt" in
    b) SOLUTION_BUCKET="$OPTARG" ;;
    r) RUN_ID="$OPTARG" ;;
    f) FUNCTION_NAME="$OPTARG" ;;
    k) DELETE_LOCK=0 ;;
    h) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

if [[ -z "$SOLUTION_BUCKET" || -z "$RUN_ID" ]]; then
  echo "Error: -b SOLUTION_BUCKET and -r RUN_ID are required." >&2
  usage
  exit 1
fi

TRIGGER_KEY="manifests/combined/${RUN_ID}/_manifests_ready.json"
LOCK_KEY="manifests/combined/${RUN_ID}/_createjob_lock.json"

# Confirm the trigger file exists before doing anything.
if ! aws s3api head-object \
    --bucket "$SOLUTION_BUCKET" \
    --key "$TRIGGER_KEY" \
    --region "$REGION" >/dev/null 2>&1; then
  echo "Error: trigger file not found: s3://${SOLUTION_BUCKET}/${TRIGGER_KEY}" >&2
  echo "The run's artifacts may have expired (14-day lifecycle) or the run_id is wrong." >&2
  exit 1
fi

# Clear the idempotency lock so the replay is not skipped as a duplicate.
if (( DELETE_LOCK == 1 )); then
  echo "Deleting idempotency lock s3://${SOLUTION_BUCKET}/${LOCK_KEY} ..."
  aws s3api delete-object \
    --bucket "$SOLUTION_BUCKET" \
    --key "$LOCK_KEY" \
    --region "$REGION" >/dev/null 2>&1 || true
fi

# Build a minimal S3 put event record matching what the handler parses:
#   event['Records'][0]['s3']['object']['key']  -> the trigger key
#   event['Records'][0]['s3']['bucket']['name'] -> the solution bucket
EVENT_JSON="$(cat <<JSON
{
  "Records": [
    {
      "eventSource": "aws:s3",
      "awsRegion": "${REGION}",
      "eventName": "ObjectCreated:Put",
      "s3": {
        "bucket": { "name": "${SOLUTION_BUCKET}" },
        "object": { "key": "${TRIGGER_KEY}" }
      }
    }
  ]
}
JSON
)"

OUT_FILE="$(mktemp)"
echo "Invoking ${FUNCTION_NAME} for run ${RUN_ID} ..."
aws lambda invoke \
  --function-name "$FUNCTION_NAME" \
  --region "$REGION" \
  --cli-binary-format raw-in-base64-out \
  --payload "$EVENT_JSON" \
  --log-type Tail \
  --query 'LogResult' \
  --output text \
  "$OUT_FILE" | base64 --decode

echo ""
echo "=== Function response ==="
cat "$OUT_FILE"
echo ""
rm -f "$OUT_FILE"
