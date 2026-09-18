# ─────────────────────────────────────────────────────────────────────────────
# Glue database + external tables (partition projection, no crawler).
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_glue_catalog_database" "this" {
  catalog_id  = local.account_id
  name        = local.glue_database_name
  description = "Glue database for the Automatic Event Hold Release for Amazon S3 Object Lock solution. Contains the external inventory table used by Athena for eligibility evaluation."
}

# ── inventory table ───────────────────────────────────────────────────────────
# External Parquet table over version-level S3 Inventory deliveries, read via
# the Hive-compatible symlink manifest (SymlinkTextInputFormat). Partition
# projection on dt (type=date).
resource "aws_glue_catalog_table" "inventory" {
  catalog_id    = local.account_id
  database_name = aws_glue_catalog_database.this.name
  name          = local.glue_table_name
  table_type    = "EXTERNAL_TABLE"
  description   = "External table over version-level S3 Inventory deliveries, read via the Hive-compatible symlink manifest S3 Inventory publishes under hive/dt=.../ (SymlinkTextInputFormat)."

  parameters = {
    "projection.enabled"          = "true"
    "projection.dt.type"          = "date"
    "projection.dt.format"        = "yyyy-MM-dd-HH-mm"
    "projection.dt.range"         = "2024-01-01-00-00,NOW"
    "projection.dt.interval"      = "1"
    "projection.dt.interval.unit" = "HOURS"
    "classification"              = "parquet"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.solution.id}/inventory/${var.target_bucket}/${var.name_prefix}/hive/"
    input_format  = "org.apache.hadoop.hive.ql.io.SymlinkTextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "bucket"
      type = "string"
    }
    columns {
      name = "key"
      type = "string"
    }
    columns {
      name = "version_id"
      type = "string"
    }
    columns {
      name = "is_latest"
      type = "boolean"
    }
    columns {
      name = "is_delete_marker"
      type = "boolean"
    }
    columns {
      name = "size"
      type = "bigint"
    }
    columns {
      name = "last_modified_date"
      type = "timestamp"
    }
    columns {
      name = "object_lock_retain_until_date"
      type = "timestamp"
    }
    columns {
      name = "object_lock_mode"
      type = "string"
    }
    columns {
      name = "object_lock_legal_hold_status"
      type = "string"
    }
    columns {
      name = "object_lock_event_hold_status"
      type = "string"
    }
  }

  partition_keys {
    name = "dt"
    type = "string"
  }
}

# ── previous_manifest table (dedup) ───────────────────────────────────────────
# External CSV table over manifests/previous/{mode}/manifest.csv. Partition
# projection type=enum (COMPLIANCE, GOVERNANCE).
resource "aws_glue_catalog_table" "previous_manifest" {
  catalog_id    = local.account_id
  database_name = aws_glue_catalog_database.this.name
  name          = local.previous_manifest_table_name
  table_type    = "EXTERNAL_TABLE"
  description   = "External CSV table over the per-mode manifest most recently submitted as a non-suspended S3PutObjectRetention job. Used to anti-join eligibility queries against versions whose release may already be in flight or complete."

  parameters = {
    "projection.enabled"        = "true"
    "projection.mode.type"      = "enum"
    "projection.mode.values"    = "COMPLIANCE,GOVERNANCE"
    "storage.location.template" = "s3://${aws_s3_bucket.solution.id}/manifests/previous/$${mode}/"
    "classification"            = "csv"
    "skip.header.line.count"    = "0"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.solution.id}/manifests/previous/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe"
      parameters = {
        "field.delim" = ","
      }
    }

    columns {
      name = "bucket"
      type = "string"
    }
    columns {
      name = "key"
      type = "string"
    }
    columns {
      name = "version_id"
      type = "string"
    }
  }

  partition_keys {
    name = "mode"
    type = "string"
  }
}

# ── withheld_candidates_history table ─────────────────────────────────────────
# External CSV table over diagnostics/withheld-candidates/{dt}/manifest.csv.
# Partition projection type=injected.
resource "aws_glue_catalog_table" "withheld_candidates_history" {
  catalog_id    = local.account_id
  database_name = aws_glue_catalog_database.this.name
  name          = local.withheld_history_table_name
  table_type    = "EXTERNAL_TABLE"
  description   = "External CSV table over per-run diagnostic listings of candidates withheld from automatic eligibility because their superseder classification is ambiguous (timestamp ties)."

  parameters = {
    "projection.enabled"        = "true"
    "projection.dt.type"        = "injected"
    "storage.location.template" = "s3://${aws_s3_bucket.solution.id}/diagnostics/withheld-candidates/$${dt}/"
    "classification"            = "csv"
    "skip.header.line.count"    = "0"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.solution.id}/diagnostics/withheld-candidates/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe"
      parameters = {
        "field.delim" = ","
      }
    }

    columns {
      name = "key"
      type = "string"
    }
    columns {
      name = "version_id"
      type = "string"
    }
    columns {
      name = "object_lock_mode"
      type = "string"
    }
    columns {
      name = "last_modified_date"
      type = "string"
    }
    columns {
      name = "reason"
      type = "string"
    }
  }

  partition_keys {
    name = "dt"
    type = "string"
  }
}

# ── Athena WorkGroup ──────────────────────────────────────────────────────────
resource "aws_athena_workgroup" "this" {
  name = local.athena_workgroup
  tags = local.tags

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = false

    result_configuration {
      output_location = "s3://${aws_s3_bucket.solution.id}/athena-results/"
    }
  }
}
