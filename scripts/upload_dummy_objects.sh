#!/usr/bin/env bash
#
# Upload a specified number of dummy objects with random content to an S3 bucket.
#
# Usage:
#   ./upload_dummy_objects.sh -b my-bucket -c 25
#   ./upload_dummy_objects.sh -b my-bucket -c 10 -s 4096
#
set -euo pipefail

REGION="us-west-2"
SIZE=1024

usage() {
  cat <<EOF
Upload N dummy objects with random content to an S3 bucket.

Usage: $0 -b BUCKET -c COUNT [-s SIZE_BYTES]

  -b BUCKET   Target S3 bucket name (required)
  -c COUNT    Number of dummy objects to create (required, positive integer)
  -s SIZE     Size of each object in bytes (default: ${SIZE})
  -h          Show this help
EOF
}

BUCKET=""
COUNT=""

while getopts "b:c:s:h" opt; do
  case "$opt" in
    b) BUCKET="$OPTARG" ;;
    c) COUNT="$OPTARG" ;;
    s) SIZE="$OPTARG" ;;
    h) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

if [[ -z "$BUCKET" || -z "$COUNT" ]]; then
  echo "Error: -b BUCKET and -c COUNT are required." >&2
  usage
  exit 1
fi

if ! [[ "$COUNT" =~ ^[1-9][0-9]*$ ]]; then
  echo "Error: -c COUNT must be a positive integer." >&2
  exit 1
fi

if ! [[ "$SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "Error: -s SIZE must be a positive integer." >&2
  exit 1
fi

echo "Uploading ${COUNT} dummy object(s) to s3://${BUCKET}/ ..."

uploaded=0
for ((i = 1; i <= COUNT; i++)); do
  key="dummy-$(uuidgen | tr '[:upper:]' '[:lower:]').bin"
  tmpfile="$(mktemp)"
  # Generate SIZE bytes of random content.
  head -c "$SIZE" /dev/urandom > "$tmpfile"

  if aws s3api put-object \
      --bucket "$BUCKET" \
      --key "$key" \
      --body "$tmpfile" \
      --region "$REGION" >/dev/null 2>&1; then
    uploaded=$((uploaded + 1))
    echo "  [${i}/${COUNT}] uploaded ${key} (${SIZE} bytes)"
  else
    echo "  Failed to upload ${key}" >&2
  fi

  rm -f "$tmpfile"
done

echo "Done. Uploaded ${uploaded}/${COUNT} object(s) to s3://${BUCKET}/"
