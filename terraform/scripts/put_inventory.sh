#!/usr/bin/env bash
# Create/update this stack's version-level S3 Inventory configuration on the
# target bucket. Faithful port of the InventoryConfig CloudFormation custom
# resource. Called by null_resource.target_bucket_inventory via local-exec.
#
# Requests exactly the OptionalFields the original template did, including
# ObjectLockEventHoldStatus — the field the native aws_s3_bucket_inventory
# resource's validation rejects and the field the eligibility query depends on.
#
# Env: TARGET_BUCKET SOLUTION_BUCKET INVENTORY_ID PREFIX DETECTION_SCHEDULE
#      ACCOUNT_ID PARTITION
set -euo pipefail

: "${TARGET_BUCKET:?}" "${SOLUTION_BUCKET:?}" "${INVENTORY_ID:?}" "${ACCOUNT_ID:?}" "${PARTITION:?}"
DETECTION_SCHEDULE="${DETECTION_SCHEDULE:-WEEKLY}"
PREFIX="${PREFIX:-}"

# Map DAILY/WEEKLY to the API's title-case Frequency, matching the custom resource.
if [ "$(printf '%s' "$DETECTION_SCHEDULE" | tr '[:lower:]' '[:upper:]')" = "DAILY" ]; then
  FREQUENCY="Daily"
else
  FREQUENCY="Weekly"
fi

# Optional prefix filter block, included only when PREFIX is non-empty.
FILTER_JSON=""
if [ -n "$PREFIX" ]; then
  FILTER_JSON=$(printf '"Filter": {"Prefix": "%s"},' "$PREFIX")
fi

INVENTORY_CONFIG=$(cat <<JSON
{
  "Id": "${INVENTORY_ID}",
  "IsEnabled": true,
  "Destination": {
    "S3BucketDestination": {
      "AccountId": "${ACCOUNT_ID}",
      "Bucket": "arn:${PARTITION}:s3:::${SOLUTION_BUCKET}",
      "Format": "Parquet",
      "Prefix": "inventory"
    }
  },
  "IncludedObjectVersions": "All",
  ${FILTER_JSON}
  "OptionalFields": [
    "Size",
    "LastModifiedDate",
    "ObjectLockMode",
    "ObjectLockRetainUntilDate",
    "ObjectLockLegalHoldStatus",
    "ObjectLockEventHoldStatus"
  ],
  "Schedule": {"Frequency": "${FREQUENCY}"}
}
JSON
)

aws s3api put-bucket-inventory-configuration \
  --bucket "$TARGET_BUCKET" \
  --id "$INVENTORY_ID" \
  --inventory-configuration "$INVENTORY_CONFIG"

echo "Applied inventory configuration '${INVENTORY_ID}' to bucket '${TARGET_BUCKET}' (frequency=${FREQUENCY}, prefix='${PREFIX}')."
