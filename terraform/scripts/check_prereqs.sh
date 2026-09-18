#!/usr/bin/env bash
# Read-only prerequisite check for the target bucket, used by the Terraform
# `external` data source. Replaces the PrereqCheck CloudFormation custom
# resource. Prints a JSON object with the Object Lock and Versioning state so
# Terraform preconditions can assert on it. Never writes anything.
#
# Usage: check_prereqs.sh <target-bucket>
set -euo pipefail

BUCKET="${1:?target bucket name required}"

# Object Lock: get-object-lock-configuration returns
# ObjectLockConfiguration.ObjectLockEnabled == "Enabled" when on; the call
# errors (ObjectLockConfigurationNotFoundError) when Object Lock was never
# enabled. Treat any failure as "Disabled" rather than aborting, so the
# Terraform precondition produces the descriptive error instead of a raw CLI
# stack trace.
object_lock="$(
  aws s3api get-object-lock-configuration --bucket "$BUCKET" \
    --query 'ObjectLockConfiguration.ObjectLockEnabled' --output text 2>/dev/null || echo "Disabled"
)"
if [ "$object_lock" = "None" ] || [ -z "$object_lock" ]; then
  object_lock="Disabled"
fi

# Versioning: Status is "Enabled" / "Suspended" / empty (never configured).
versioning="$(
  aws s3api get-bucket-versioning --bucket "$BUCKET" \
    --query 'Status' --output text 2>/dev/null || echo "Disabled"
)"
if [ "$versioning" = "None" ] || [ -z "$versioning" ]; then
  versioning="Disabled"
fi

printf '{"object_lock":"%s","versioning":"%s"}\n' "$object_lock" "$versioning"
