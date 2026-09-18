#!/usr/bin/env bash
# Build a Lambda layer containing an up-to-date boto3/botocore.
#
# The managed python3.14 runtime bundles botocore 1.42.x, which does not declare
# the EventHold parameter on the s3control S3PutObjectRetention.Retention shape.
# CreateJob's create_job call is therefore rejected inside the function and the
# GOVERNANCE mode is withheld ("event_hold_unsupported_by_runtime"). Layering a
# newer boto3 ahead of the bundled SDK on sys.path restores the parameter.
#
# boto3/botocore are pure Python, so a plain `pip install --target` produces an
# artifact that works on Lambda regardless of the machine this runs on.
#
# Env:
#   BOTO3_VERSION   boto3 version to install (e.g. 1.43.98)
#   LAYER_BUILD_DIR directory to assemble the layer under (its python/ subdir)
set -euo pipefail

: "${BOTO3_VERSION:?BOTO3_VERSION is required}"
: "${LAYER_BUILD_DIR:?LAYER_BUILD_DIR is required}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
TARGET_DIR="${LAYER_BUILD_DIR}/python"

# Clean any previous build so removed/renamed files don't linger in the zip.
rm -rf "$LAYER_BUILD_DIR"
mkdir -p "$TARGET_DIR"

# --only-binary=:all: is fine here (wheels are pure-Python); no compiled deps.
"$PYTHON_BIN" -m pip install \
  --quiet \
  --disable-pip-version-check \
  --no-cache-dir \
  --target "$TARGET_DIR" \
  "boto3==${BOTO3_VERSION}"

# Trim files that add weight but nothing the runtime needs, keeping the layer
# under the 250 MB unzipped limit with margin.
find "$TARGET_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$TARGET_DIR" -type d -name "*.dist-info" -prune -exec rm -rf {} +
find "$TARGET_DIR" -type d -name "tests" -prune -exec rm -rf {} +

echo "Built boto3 ${BOTO3_VERSION} layer under ${TARGET_DIR}"
