# Test Data Scripts

Helper bash scripts for seeding an S3 bucket with dummy objects and for
randomly deleting a subset of them. Useful for exercising the event-hold
removal pipeline in this repo with throwaway data.

Both scripts use the **AWS CLI** only (no Python / boto3 required) and are
compatible with the stock macOS bash 3.2. The region is hardcoded to
`us-west-2`. Credentials come from your environment / AWS CLI configuration
(e.g. `AWS_PROFILE`, `~/.aws/credentials`, or an assumed role).

## Prerequisites

- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- Valid AWS credentials with permission to `s3:PutObject`, `s3:ListBucket`,
  and `s3:DeleteObject` on the target bucket
- `uuidgen`, `awk`, `sort` (all present by default on macOS/Linux)

Make the scripts executable once:

```bash
chmod +x scripts/*.sh
```

## upload_dummy_objects.sh

Creates N dummy objects with random binary content and random keys of the form
`dummy-<uuid>.bin`. No key prefix is used.

```
Usage: upload_dummy_objects.sh -b BUCKET -c COUNT [-s SIZE_BYTES]

  -b BUCKET   Target S3 bucket name (required)
  -c COUNT    Number of dummy objects to create (required, positive integer)
  -s SIZE     Size of each object in bytes (default: 1024)
  -h          Show help
```

Examples:

```bash
# Upload 25 objects, 1 KB each (default size)
./scripts/upload_dummy_objects.sh -b my-bucket -c 25

# Upload 10 objects, 4 KB each
./scripts/upload_dummy_objects.sh -b my-bucket -c 10 -s 4096
```

## random_delete_objects.sh

Lists every object in the bucket, then randomly selects a subset to delete —
either a percentage of the total (`-p`) or an absolute count (`-n`).

**Runs as a dry run by default.** Pass `-x` to actually delete. Without `-y`,
executing prompts you to type `delete` to confirm.

```
Usage: random_delete_objects.sh -b BUCKET (-p PERCENT | -n COUNT) [-x] [-y]

  -b BUCKET   Target S3 bucket name (required)
  -p PERCENT  Percentage of listed objects to delete (0-100)
  -n COUNT    Absolute number of objects to delete
  -x          Execute deletion (otherwise dry run)
  -y          Skip the interactive confirmation prompt (only with -x)
  -h          Show help

Exactly one of -p or -n must be provided.
```

Examples:

```bash
# Preview deleting 30% of objects (no changes made)
./scripts/random_delete_objects.sh -b my-bucket -p 30

# Delete a random 5 objects (prompts for confirmation)
./scripts/random_delete_objects.sh -b my-bucket -n 5 -x

# Delete 50% without the interactive prompt
./scripts/random_delete_objects.sh -b my-bucket -p 50 -x -y
```

Notes:

- Percentage selection uses floor rounding, e.g. 30% of 25 objects deletes 7.
- If `-n` exceeds the number of objects present, all objects are selected.

## Object Lock / event holds

The buckets in this project may have **S3 Object Lock** enabled. If an object
has an active event hold, legal hold, or retention period:

- On a versioned bucket, `delete-object` creates a delete marker rather than
  permanently removing the version.
- A locked version may fail to delete outright (`AccessDenied`).

This is expected when testing the event-hold-removal pipeline — the delete
script will not behave like it does against a plain, unversioned bucket. Use
throwaway buckets/data with these scripts.
