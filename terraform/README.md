# Terraform deployment

This directory is a Terraform port of the core [Automatic Event Hold Release
for Amazon S3 Object Lock](../README.md) pipeline, originally shipped as the
CloudFormation template [`../template.yaml`](../template.yaml).

It deploys the same batch pipeline — S3 Inventory → Athena → S3 Batch Operations
— that releases the Object Lock event hold on eligible noncurrent object
versions. The four pipeline Lambdas run the exact same code as the
CloudFormation version; only the surrounding infrastructure is expressed in
Terraform.

> The original `template.yaml` is left in place. This is an alternative way to
> deploy the same solution, not a replacement.

## Prerequisites

- Terraform >= 1.5.0
- AWS provider >= 5.60 (installed automatically by `terraform init`)
- The **AWS CLI**, authenticated to the target account/Region. Two things shell
  out to it:
  - the deploy-time prerequisite check (`scripts/check_prereqs.sh`)
  - the target-bucket S3 Inventory configuration (`scripts/put_inventory.sh`) —
    see [S3 Inventory](#s3-inventory-uses-the-aws-cli) below for why
- A target S3 bucket that already has **Object Lock and Versioning enabled**,
  deployed in the **same Region** you run Terraform against.

## Deployment

### 1. Authenticate to AWS

Terraform and the AWS CLI must both resolve credentials for the account and
Region that holds the target bucket. Use whatever you normally use (a named
profile, SSO, environment variables). The CLI is used at plan/apply time for the
prerequisite check and the inventory configuration, so it must see the same
account as Terraform.

```bash
export AWS_PROFILE=my-profile
export AWS_REGION=us-east-1          # must match the target bucket's Region
aws sts get-caller-identity          # confirm account + Region
```

### 2. Set your variables

```bash
cd terraform
cp example.tfvars terraform.tfvars
```

Edit `terraform.tfvars`. At minimum set `target_bucket` and `release_mode`;
leave `report_only = true` for the first deployment. See [`variables.tf`](variables.tf)
for every variable and its default.

```hcl
target_bucket = "amzn-s3-demo-object-lock-bucket"
release_mode  = "either"   # delete | overwrite | either
report_only   = true
safety_threshold = 500
```

### 3. Initialize, plan, apply

```bash
terraform init                              # downloads providers, writes the lock file
terraform plan  -var-file=terraform.tfvars  # review what will be created
terraform apply -var-file=terraform.tfvars  # create it
```

`plan`/`apply` run `scripts/check_prereqs.sh` first. If the target bucket is
missing Object Lock or Versioning — or the active-mode + `safety_threshold = 0`
combination is set — the apply fails immediately with a descriptive error and
creates nothing.

> **State backend.** This module uses local state by default. For team or CI
> use, configure a remote backend (e.g. S3 with native locking) by adding a
> `backend "s3" {}` block to `versions.tf` and re-running `terraform init`.

### 4. Subscribe to notifications

```bash
aws sns subscribe \
  --topic-arn "$(terraform output -raw sns_topic_arn)" \
  --protocol email \
  --notification-endpoint you@example.com
```

Confirm the subscription from your inbox. The **first S3 Inventory delivery can
take up to 48 hours**; once it arrives the pipeline runs end-to-end
automatically.

### 5. Review the first run, then switch to active mode

With `report_only = true`, the first run creates every mode's Batch Operations
job **suspended**, so no holds are released. Review the eligibility manifests in
the solution bucket (`terraform output -raw solution_bucket_name`) and the
suspended jobs in the [S3 Batch Operations console](https://console.aws.amazon.com/s3/batch-jobs).

When you're satisfied, switch to active release by setting `report_only = false`
(and a positive `safety_threshold`) in `terraform.tfvars` and re-applying:

```bash
terraform apply -var-file=terraform.tfvars
```

This only updates the CreateJob Lambda's environment; the change takes effect on
the next pipeline run — no teardown or redeploy is required. Any of `prefix`,
`release_mode`, `detection_schedule`, `report_only`, and `safety_threshold` can
be changed the same way. Changing `target_bucket` re-targets the whole solution
and is best done as a fresh deployment (a new `name_prefix`).

### Covering more than one bucket

One deployment covers exactly one bucket, in that bucket's account and Region.
For several buckets, use several deployments with distinct `name_prefix` values
(and separate state — e.g. a workspace or state key per bucket). See the parent
repo's [MULTI-ACCOUNT-DEPLOYMENT.md](../MULTI-ACCOUNT-DEPLOYMENT.md) for the
naming limits and what is shared across deployments in an account and Region.

### Teardown

`terraform destroy` intentionally fails on the solution bucket, which carries
`prevent_destroy = true` to mirror the template's `Retain` policy (its audit
trail is meant to outlive the stack). To tear down deliberately:

1. Empty the solution bucket (see the parent repo's manual-cleanup guidance).
2. Remove the `lifecycle { prevent_destroy = true }` block from
   `aws_s3_bucket.solution` in [`storage.tf`](storage.tf).
3. Run `terraform destroy -var-file=terraform.tfvars`.

The destroy-time provisioner on `null_resource.target_bucket_inventory` removes
only this deployment's inventory configuration from the target bucket, leaving
any other inventory configurations untouched.

## Variable ↔ parameter mapping

Every CloudFormation parameter that applies to the core pipeline maps to a
Terraform variable:

| CloudFormation parameter | Terraform variable   |
| ------------------------ | -------------------- |
| (stack name)             | `name_prefix`        |
| `TargetBucket`           | `target_bucket`      |
| `Prefix`                 | `prefix`             |
| `ReleaseMode`            | `release_mode`       |
| `DetectionSchedule`      | `detection_schedule` |
| `ReportOnly`             | `report_only` (bool) |
| `SafetyThreshold`        | `safety_threshold`   |
| `KMSKeyArn`              | `kms_key_arn`        |

The CloudFormation stack derived every resource name from `${AWS::StackName}`.
Terraform has no stack name, so `name_prefix` plays that role (default
`auto-event-hold-release`). As in the template, it is lowercased and truncated
to 32 characters to build the solution bucket name
`<name_prefix>-<account>-<region>-an`.

## How CloudFormation concepts were translated

The template leaned on five Lambda-backed **custom resources** for things
CloudFormation can't do declaratively. Four of them become native Terraform and
one keeps an imperative helper:

| CloudFormation custom resource | Terraform equivalent |
| ------------------------------ | -------------------- |
| `LowerCase` (lowercases the stack name) | `lower()` / `substr()` in [`locals.tf`](locals.tf) — no Lambda |
| `PrereqCheck` (Object Lock + Versioning enabled) | `external` data source + `precondition`s on `null_resource.prereq` ([`data.tf`](data.tf), [`main.tf`](main.tf)) |
| `SetupNotifications` (S3 event notifications) | native `aws_s3_bucket_notification` ([`wiring.tf`](wiring.tf)) |
| `InventoryConfig` (target-bucket inventory) | `null_resource` + AWS CLI `local-exec` — see below |
| `S3TableIntegrationSetup` | **dropped** (optional feature) |

Other structural mappings:

- **`Conditions` / `!If [HasKMSKey, ...]`** → the `local.has_kms_key` boolean
  and `count`/`dynamic` blocks. When `kms_key_arn` is set, the solution bucket,
  SNS topic, DLQ, and log groups use the CMK, and each execution role gets an
  extra `kms:GenerateDataKey*`/`kms:Decrypt` policy — same as the template.
- **`Rules` (cross-parameter validation)** → a `precondition` on
  `null_resource.prereq`: active release mode (`report_only = false`) with
  `safety_threshold = 0` is rejected, exactly as the CloudFormation `Rules`
  block did.
- **`DeletionPolicy: Retain`** on the solution bucket and its policy →
  `lifecycle { prevent_destroy = true }` on `aws_s3_bucket.solution`.
- **Inline `ZipFile` Lambda code** → extracted verbatim into
  `functions/<name>/index.py` and packaged with the `archive` provider. The
  extraction is reproducible via [`extract_lambdas.py`](extract_lambdas.py).

### S3 Inventory uses the AWS CLI

`null_resource.target_bucket_inventory` configures the target bucket's inventory
by shelling out to `aws s3api put-bucket-inventory-configuration` (and
`delete-bucket-inventory-configuration` on destroy) rather than using the native
`aws_s3_bucket_inventory` resource.

This is deliberate and load-bearing. The eligibility query keys entirely on
`object_lock_event_hold_status = 'ON'`, which requires the inventory to request
the `ObjectLockEventHoldStatus` optional field. The AWS provider's
`aws_s3_bucket_inventory` resource validates `optional_fields` against a
hardcoded list that **does not include `ObjectLockEventHoldStatus`**, so the
native resource cannot request the one field the whole pipeline depends on.
CloudFormation had the same gap in `AWS::S3::Bucket`, which is exactly why the
original template configured inventory through a Lambda. The CLI call is the
faithful equivalent and requests the identical field set.

## What was dropped

Per the scope of this port, the optional, off-by-default features were not
converted:

- **SolutionBucket server access logs** (`EnableServerAccessLogs`,
  `ServerAccessLogsDestinationLogGroupArn`, `ServerAccessLogsRetentionDays`) and
  the `AWS::Logs::DeliverySource`/`DeliveryDestination`/`Delivery` resources.
- **S3 Tables integration** (`EnableS3TablesIntegration`) and its
  account-and-Region-wide setup Lambda.

Both are off by default in the template and provide SQL-queryable access logs
for the solution's *working* bucket (which holds no customer data) — an
operational nicety, not part of the release pipeline. If you need them, set them
up separately or extend this configuration.

Everything else — the four pipeline Lambdas, the Batch Operations service role,
Glue database and three tables, Athena workgroup, SQS DLQ, SNS topic, S3 event
notifications, the EventBridge completion rule, both CloudWatch alarms, and
optional KMS encryption — is present and matches the template.

## File layout

| File | Contents |
| ---- | -------- |
| `versions.tf`   | Terraform + provider version constraints, provider block |
| `variables.tf`  | Input variables (the ported parameters) |
| `locals.tf`     | Derived names, `has_kms_key`, ARNs |
| `data.tf`       | Caller identity / region / partition, prerequisite check |
| `main.tf`       | `null_resource.prereq` validation gate |
| `storage.tf`    | Solution bucket, encryption, lifecycle, bucket policy |
| `glue.tf`       | Glue database, 3 tables, Athena workgroup |
| `messaging.tf`  | SQS DLQ + policy, SNS topic + policy |
| `iam.tf`        | Execution roles + policies, Batch Operations role |
| `lambda.tf`     | Packaging, log groups, 4 functions, invoke permissions |
| `wiring.tf`     | S3 notifications, target-bucket inventory, EventBridge rule |
| `monitoring.tf` | Metric filter + 2 CloudWatch alarms |
| `outputs.tf`    | Bucket name, SNS ARN, function names, Batch Ops role, etc. |
| `functions/`    | Extracted pipeline Lambda source (`index.py` per function) |
| `scripts/`      | AWS CLI helpers for prereq check and inventory config |

## Notes and caveats

- **`prevent_destroy` on the solution bucket.** Matching the template's
  `Retain` policy, `terraform destroy` will fail on the bucket by design — see
  [Teardown](#teardown) for the deliberate removal steps.
- **AWS CLI dependency.** The prerequisite check and inventory configuration run
  the CLI locally at plan/apply time. In CI, ensure the CLI is installed and the
  same credentials Terraform uses are available to it.
- **Runtime behavior is unchanged.** The Lambda code is byte-for-byte the
  template's, so the pipeline's stage handoffs, idempotency, safety threshold,
  and report-only/active semantics all behave as documented in the parent repo's
  [ARCHITECTURE.md](../ARCHITECTURE.md) and [OPERATIONS.md](../OPERATIONS.md).
```
