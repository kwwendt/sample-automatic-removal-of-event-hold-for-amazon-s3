import hashlib
import json
import os
import re
import time
import urllib.parse
import boto3

# S3 Inventory delivery timestamp: YYYY-MM-DDThh-mmZ
_DT_PATTERN = re.compile(
    r'\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}Z\Z'
)

# S3 event sequencers are hex-encoded strings (typically 16 chars for
# PUT/DELETE). Validated before use in S3 key paths or SQL to prevent
# path manipulation and SQL injection via a crafted/direct-invoke event.
_SEQUENCER_PATTERN = re.compile(r'\A[0-9A-Fa-f]{1,64}\Z')

# Field declarations inside an S3 Inventory manifest.json "fileSchema"
# string. Parquet deliveries carry a Parquet message schema, e.g.
#   message s3.inventory {  required binary bucket (STRING);
#   optional boolean is_latest;
#   optional int64 last_modified_date (TIMESTAMP(MILLIS,true));}
# The field NAME (group 2) always follows the repetition and type
# tokens; logical-type annotations are parenthesised AFTER the name, so
# they never match. Confirmed against a real delivery's manifest.json.
_PARQUET_FIELD_PATTERN = re.compile(
    r'\b(?:required|optional|repeated)\s+(\w+)\s+(\w+)'
)

# The inventory columns the eligibility SQL actually reads -- see
# _build_candidate_ctes and _build_superseder_ctes. Deliberately NOT the
# full GlueTable column list: size, object_lock_retain_until_date and
# object_lock_legal_hold_status are declared for completeness but are
# never projected by any query, so their absence cannot change a result
# and is logged rather than raised. `bucket` is excluded too -- the
# manifest UNLOAD emits the literal Target_Bucket name, it does not read
# the inventory column. `dt` is a projected partition key, not a data
# column.
#
# WHY THIS GUARD EXISTS. Athena resolves Parquet columns by NAME and
# yields NULL for a Glue-declared column that the data file does not
# contain -- it does not raise. So if the column names S3 Inventory
# delivers ever diverge from what GlueTable declares, the predicate
# `object_lock_event_hold_status = 'ON'` matches zero rows: the run
# reports 0 eligible versions and SUCCEEDS, with no error, no DLQ
# message, and no alarm, for as long as the mismatch persists. A
# zero-eligible run is indistinguishable from a healthy one, which makes
# this the only failure in the pipeline that is silent by default --
# every other stage raises and reaches the DLQ. Verifying the delivered
# schema up front converts it into a loud failure.
_REQUIRED_INVENTORY_COLUMNS = frozenset({
    'key',
    'version_id',
    'is_latest',
    'is_delete_marker',
    'last_modified_date',
    'object_lock_mode',
    'object_lock_event_hold_status',
})

# Declared by GlueTable but never projected by any query. Absence is
# reported for drift visibility only; it never fails a run.
#
# DO NOT move object_lock_retain_until_date into a query predicate. While
# an event hold is ON, S3 Inventory reports a ROLLING retain-until-date of
# (report generation time + configured duration), recomputed on every
# delivery -- it is not fixed at write time. Confirmed from a real delivery
# generated 2026-08-13: five objects written between 2026-08-03 and
# 2026-08-12, each carrying a 1 YEARS hold, ALL reported
# 2027-08-13T00:00:00.000Z -- that report's own creationTimestamp plus one
# year, with no relation to any object's creation time. Eligibility logic
# comparing this column against a date would therefore drift between runs,
# and unit fixtures with static timestamps would not catch it. The value
# only settles once the hold is released, which is why "retain-until-date
# has stopped advancing" is the post-release verification signal and never
# an input to selection.
_OPTIONAL_INVENTORY_COLUMNS = frozenset({
    'bucket',
    'size',
    'object_lock_retain_until_date',
    'object_lock_legal_hold_status',
})


def handler(event, context):
    solution_bucket = os.environ['SOLUTION_BUCKET']
    db_name = os.environ['GLUE_DATABASE']
    table_name = os.environ['GLUE_TABLE']
    previous_manifest_table = os.environ['PREVIOUS_MANIFEST_TABLE']
    workgroup = os.environ['ATHENA_WORKGROUP']
    target_bucket = os.environ['TARGET_BUCKET']
    inventory_id = os.environ['INVENTORY_ID']
    release_mode = os.environ['RELEASE_MODE']

    # Parse the S3 event record
    record = event['Records'][0]
    raw_key = record['s3']['object']['key']
    key = urllib.parse.unquote_plus(raw_key)
    sequencer = record['s3']['object']['sequencer']

    # Key format: inventory/{target_bucket}/{inventory_id}/{dt}/manifest.json
    key_parts = key.split('/')
    dt = key_parts[3]

    # Validate dt against the S3 Inventory delivery timestamp format.
    # Abort before any Glue or Athena call if the value is unexpected —
    # prevents SQL injection and partition-location poisoning (Req 2.1–2.3).
    if not _DT_PATTERN.match(dt):
        print(json.dumps({
            'event': 'dt_validation_failed',
            'dt': dt,
            'key': key,
            'reason': 'dt does not match expected YYYY-MM-DDThh-mmZ format',
        }))
        raise ValueError(
            f"Invalid dt '{dt}' extracted from key '{key}'; "
            "expected S3 Inventory format YYYY-MM-DDThh-mmZ"
        )

    # Validate sequencer before it is used in the UNLOAD output S3 path
    # (embedded directly in Athena SQL) or the ClientRequestToken. S3's
    # own sequencers always match this format; a value that doesn't is
    # either a malformed event or a direct, non-S3-triggered invocation
    # and must not be trusted for SQL or key-path construction.
    if not _SEQUENCER_PATTERN.match(sequencer):
        print(json.dumps({
            'event': 'sequencer_validation_failed',
            'sequencer': sequencer,
            'key': key,
            'reason': 'sequencer does not match expected hex format',
        }))
        raise ValueError(
            f"Invalid sequencer '{sequencer}' in event for key '{key}'; "
            "expected a hex-encoded S3 event sequencer"
        )

    # Verify this delivery actually carries the columns the eligibility
    # SQL reads, BEFORE any Athena call. Raises on a mismatch rather than
    # letting Athena's NULL-for-missing-column behavior turn it into a
    # successful zero-eligible run (see _REQUIRED_INVENTORY_COLUMNS).
    # The triggering object IS the delivery's manifest.json -- the S3
    # notification filters on that suffix -- so no lookup is needed, and
    # StartQueryRole's existing s3:GetObject on inventory/* covers it.
    s3 = boto3.client('s3')
    _verify_inventory_schema(s3, solution_bucket, key)

    # No explicit Glue partition registration: GlueTable has
    # projection.enabled=true, so Athena computes partition locations
    # itself from the dt column value and IGNORES any partition metadata
    # registered in the Glue Data Catalog (confirmed via AWS's partition
    # projection documentation). SQL below uses hive_dt (the dt value in
    # the format Athena's partition projection expects, matching S3
    # Inventory's hive/dt=.../ symlink-manifest folder naming), not the
    # raw manifest-path dt (which uses a different format -- see
    # _to_hive_dt).
    hive_dt = _to_hive_dt(dt)

    # Count eligible rows before creating any manifest or Batch Operations
    # job. GetQueryExecution does not expose a DML row count for UNLOAD, so
    # dedicated COUNT(*) queries are the authoritative safety-control input.
    athena = boto3.client('athena')
    modes = ['COMPLIANCE', 'GOVERNANCE']
    deadline = time.time() + 13 * 60
    count_query_ids = {}
    count_query_hashes = {}

    for mode in modes:
        sql = _build_count_query(
            mode, hive_dt, target_bucket, release_mode,
            table_name, previous_manifest_table,
        )
        count_query_hashes[mode] = hashlib.sha256(sql.encode()).hexdigest()
        count_query_ids[mode] = _start_query(
            athena, sql, db_name, workgroup, solution_bucket,
            dt, mode, 'count',
        )

    # The withheld-candidates diagnostic reports exactly the candidates
    # the eligibility queries above withheld due to ambiguous superseder
    # classification -- same predicate, inverted condition (see
    # _build_withheld_candidates_ctes). Only the delete/overwrite path
    # can withhold; in `either` mode nothing is withheld, so the
    # diagnostic is skipped and the count is 0 by construction. It
    # reports both lock modes in one query (object_lock_mode is a
    # column) and never gates eligibility; it is purely informational
    # (see README "Timestamp ties fail closed").
    withheld_count_hash = None
    if release_mode != 'either':
        withheld_count_sql = _build_withheld_candidates_count_query(
            hive_dt, table_name,
        )
        withheld_count_hash = hashlib.sha256(
            withheld_count_sql.encode()
        ).hexdigest()
        count_query_ids['_WITHHELD'] = _start_query(
            athena, withheld_count_sql, db_name, workgroup,
            solution_bucket, dt, '_WITHHELD', 'count',
        )

    _wait_for_queries(athena, count_query_ids, deadline, 'count')
    row_counts = {
        mode: _read_count(athena, count_query_ids[mode])
        for mode in modes
    }
    withheld_candidate_count = 0
    if '_WITHHELD' in count_query_ids:
        withheld_candidate_count = _read_count(
            athena, count_query_ids['_WITHHELD']
        )

    # Only non-empty modes need an UNLOAD manifest. Count queries and
    # UNLOAD queries share the exact candidate CTE, so the threshold count
    # and manifest selection cannot drift within this implementation.
    unload_query_ids = {}
    for mode in modes:
        if row_counts[mode] == 0:
            continue
        output_prefix = (
            f's3://{solution_bucket}/manifests/parts/{sequencer}/{mode}/'
        )
        sql = _build_query(
            mode, hive_dt, output_prefix, solution_bucket, target_bucket,
            release_mode, table_name, db_name,
            previous_manifest_table,
        )
        unload_query_ids[mode] = _start_query(
            athena, sql, db_name, workgroup, solution_bucket,
            dt, mode, 'unload',
        )

    if withheld_candidate_count > 0:
        withheld_output_prefix = (
            f's3://{solution_bucket}/manifests/parts/{sequencer}/_WITHHELD/'
        )
        withheld_unload_sql = _build_withheld_candidates_query(
            hive_dt, withheld_output_prefix, table_name,
        )
        unload_query_ids['_WITHHELD'] = _start_query(
            athena, withheld_unload_sql, db_name, workgroup,
            solution_bucket, dt, '_WITHHELD', 'unload',
        )

    _wait_for_queries(athena, unload_query_ids, deadline, 'unload')

    # Any failed/cancelled/timed-out query raises before this point, causing
    # the asynchronous invocation to be retried and eventually sent to the
    # DLQ. Never convert query failure into a successful zero-row run.
    result = {
        'run_id': sequencer,
        'dt': dt,
        'target_bucket': target_bucket,
        'inventory_id': inventory_id,
        'modes_succeeded': modes,
        'row_counts': row_counts,
        'withheld_candidate_count': withheld_candidate_count,
        'withheld_manifest_written': '_WITHHELD' in unload_query_ids,
        'query_execution_ids': unload_query_ids,
        'count_query_execution_ids': count_query_ids,
        'count_query_hashes': count_query_hashes,
        'withheld_count_hash': withheld_count_hash,
    }

    print(json.dumps({
        'event': 'startquery_complete',
        'dt': dt,
        'run_id': sequencer,
        'row_counts': row_counts,
        'withheld_candidate_count': withheld_candidate_count,
        'modes_succeeded': modes,
    }))
    s3.put_object(
        Bucket=solution_bucket,
        Key=f'manifests/parts/{sequencer}/_query_complete.json',
        Body=json.dumps(result),
        ContentType='application/json',
    )
    return result


def _to_hive_dt(dt):
    """Convert the manifest-path dt (YYYY-MM-DDThh-mmZ, from the S3 event
    key that triggers this Lambda) to the format S3 Inventory's
    hive/dt=.../ symlink-manifest folder actually uses
    (YYYY-MM-DD-hh-mm, dashes only, no T/Z) -- confirmed these are two
    genuinely different strings for the same delivery against a real S3
    Inventory delivery, not just a cosmetic reformat. GlueTable's
    partition projection (projection.dt.format='yyyy-MM-dd-HH-mm')
    expects this second format.

    dt has already passed _DT_PATTERN validation by the time this is
    called, so this is a straight character substitution, not a parse
    that can fail on unexpected input.
    """
    return dt.replace('T', '-').rstrip('Z')


def _parse_file_schema(file_schema):
    """Extract the column names from a manifest.json fileSchema string.

    Returns a set of names, or an empty set if nothing parsed.
    """
    return {
        name for _type, name
        in _PARQUET_FIELD_PATTERN.findall(file_schema)
    }


def _verify_inventory_schema(s3, solution_bucket, manifest_key):
    """Fail loudly when a delivery lacks a column the eligibility SQL reads.

    Athena returns NULL -- not an error -- for a Glue-declared column that
    the underlying Parquet file does not contain, so an inventory schema
    that has drifted away from GlueTable's declarations produces a
    silently empty eligible set instead of a failure. Every caller of the
    eligibility SQL depends on this check having run first; see
    _REQUIRED_INVENTORY_COLUMNS for the full rationale.

    Raises RuntimeError on any condition that would make the eligibility
    result untrustworthy: an unreadable or malformed manifest, a
    non-Parquet delivery, an unparseable fileSchema, or a missing required
    column. Never downgrades a mismatch to a warning.
    """
    body = s3.get_object(
        Bucket=solution_bucket, Key=manifest_key
    )['Body'].read()
    try:
        manifest = json.loads(body)
    except ValueError as exc:
        raise RuntimeError(
            f"Inventory manifest '{manifest_key}' is not valid JSON: {exc}"
        ) from exc

    file_format = manifest.get('fileFormat')
    if file_format != 'Parquet':
        raise RuntimeError(
            f"Inventory delivery '{manifest_key}' has fileFormat "
            f"{file_format!r}, expected 'Parquet'. GlueTable reads Parquet "
            "via ParquetHiveSerDe, so no query result from this delivery "
            "would be trustworthy."
        )

    file_schema = manifest.get('fileSchema') or ''
    delivered = _parse_file_schema(file_schema)
    if not delivered:
        raise RuntimeError(
            f"Could not parse any column names from the fileSchema of "
            f"inventory delivery '{manifest_key}'. Refusing to query a "
            "delivery whose schema cannot be verified, because a missing "
            "column would yield a silently empty eligible set. "
            f"fileSchema was: {file_schema[:500]!r}"
        )

    missing_required = sorted(_REQUIRED_INVENTORY_COLUMNS - delivered)
    missing_optional = sorted(_OPTIONAL_INVENTORY_COLUMNS - delivered)
    unexpected = sorted(
        delivered
        - _REQUIRED_INVENTORY_COLUMNS
        - _OPTIONAL_INVENTORY_COLUMNS
    )

    print(json.dumps({
        'event': 'inventory_schema_checked',
        'manifest_key': manifest_key,
        'delivered_columns': sorted(delivered),
        'missing_required': missing_required,
        # Declared by GlueTable but unused by any query -- informational.
        'missing_optional': missing_optional,
        # Delivered but not declared by GlueTable, and therefore invisible
        # to Athena. An event-hold column arriving under a name this
        # solution does not declare would surface here.
        'undeclared_columns': unexpected,
    }))

    if missing_required:
        raise RuntimeError(
            "S3 Inventory delivery is missing "
            f"{len(missing_required)} column(s) the eligibility query "
            f"reads: {', '.join(missing_required)}. Delivered columns: "
            f"{', '.join(sorted(delivered)) or '(none)'}. Athena would "
            "return NULL for each missing column rather than failing, so "
            "this run would have reported 0 eligible versions and "
            "succeeded. Failing instead. Check that the inventory "
            "configuration requests the matching OptionalFields and that "
            "the Glue table's column names match what S3 Inventory "
            f"delivers (manifest: {manifest_key})."
        )


def _start_query(
    athena, sql, database, workgroup, solution_bucket,
    dt, mode, phase,
):
    """Start an idempotent query whose token changes when its SQL changes."""
    sql_hash = hashlib.sha256(sql.encode()).hexdigest()
    token = hashlib.sha256(
        f'{solution_bucket}/{dt}/{mode}/{phase}/{sql_hash}'.encode()
    ).hexdigest()
    response = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={'Database': database},
        WorkGroup=workgroup,
        ClientRequestToken=token,
    )
    return response['QueryExecutionId']


def _wait_for_queries(athena, query_ids, deadline, phase):
    """Wait for all queries or raise so a partial/failed run cannot advance."""
    pending = set(query_ids)
    failures = {}
    while pending and time.time() < deadline:
        time.sleep(5)
        for mode in list(pending):
            response = athena.get_query_execution(
                QueryExecutionId=query_ids[mode]
            )
            status = response['QueryExecution']['Status']
            state = status['State']
            if state == 'SUCCEEDED':
                pending.discard(mode)
            elif state in ('FAILED', 'CANCELLED'):
                failures[mode] = status.get('StateChangeReason', state)
                pending.discard(mode)

    for mode in pending:
        failures[mode] = 'TIMEOUT'

    if failures:
        print(json.dumps({
            'event': 'athena_query_batch_failed',
            'phase': phase,
            'failed_modes': sorted(failures),
            'failures': failures,
        }))
        raise RuntimeError(
            f"Athena {phase} query failure: {json.dumps(failures)}"
        )


def _read_count(athena, query_execution_id):
    """Read and strictly validate the single COUNT(*) result row."""
    response = athena.get_query_results(
        QueryExecutionId=query_execution_id,
        MaxResults=2,
    )
    rows = response.get('ResultSet', {}).get('Rows', [])
    if len(rows) != 2:
        raise ValueError(
            f"Count query {query_execution_id} returned {len(rows)} rows; "
            "expected one header and one data row"
        )
    data = rows[1].get('Data', [])
    raw_count = data[0].get('VarCharValue') if data else None
    if raw_count is None or not raw_count.isdigit():
        raise ValueError(
            f"Count query {query_execution_id} returned invalid count "
            f"{raw_count!r}"
        )
    return int(raw_count)


def _build_superseder_ctes(dt, glue_table):
    """Build the shared per-candidate superseder-classification CTEs.

    This is THE tie-ambiguity predicate: the eligibility CTEs
    (_build_candidate_ctes, delete/overwrite path) and the withheld-
    candidates diagnostic (_build_withheld_candidates_ctes) both compose
    these CTEs, so what is withheld and what is reported cannot drift.

    S3 Inventory has no sub-second chronological tiebreaker, but
    is_latest does carry ordering information: the current version is
    always LAST in its key's chain. Rather than guessing an order among
    timestamp-tied versions, classify each candidate C (noncurrent,
    hold=ON, data version -- a delete marker can never carry an event
    hold) by the SET of versions its immediate superseder could be:

      possible_superseders(C) =
          every other row at C's (key, last_modified_date)
            -- a same-timestamp noncurrent peer may sit on either side
            -- of C; a same-timestamp CURRENT row sorts last, so it
            -- supersedes C only if no noncurrent peer does, but it is
            -- still a possibility and must be in the set
        UNION
          the possibly-first rows at the key's next-greater timestamp
            -- the noncurrent rows of that group; or, if the group is
            -- only the current version, that current version

    If every possible superseder agrees on is_delete_marker
    (superseder_kinds = 1), C's delete-vs-overwrite classification is
    determined regardless of the unknowable order. Only genuine
    disagreement -- a data version AND a delete marker both possible --
    withholds C (superseder_kinds > 1). A candidate with NO possible
    superseder (superseder_class row absent) cannot be classified and is
    not eligible, but it is not "withheld by ambiguity" either.

    Returns CTE text WITHOUT a leading WITH so callers can compose it.
    """
    return f"""inv AS (
    SELECT key, version_id, is_latest, is_delete_marker,
           object_lock_event_hold_status, object_lock_mode, last_modified_date
    FROM {glue_table}
    WHERE dt = '{dt}'
  ),
  ts_order AS (
    SELECT key, last_modified_date,
           LEAD(last_modified_date) OVER (
             PARTITION BY key ORDER BY last_modified_date ASC
           ) AS next_lmd
    FROM (SELECT DISTINCT key, last_modified_date FROM inv)
  ),
  group_noncurrent AS (
    SELECT key, last_modified_date,
           MAX(CASE WHEN is_latest = false THEN 1 ELSE 0 END) AS has_nc
    FROM inv
    GROUP BY key, last_modified_date
  ),
  candidate_base AS (
    SELECT key, version_id, object_lock_mode, last_modified_date
    FROM inv
    WHERE is_latest = false
      AND is_delete_marker = false
      AND object_lock_event_hold_status = 'ON'
  ),
  possible_superseders AS (
    SELECT c.key, c.version_id,
           p.is_delete_marker AS superseder_is_delete_marker,
           CASE WHEN p.is_latest = false THEN 1 ELSE 0 END
             AS via_tied_noncurrent_peer
    FROM candidate_base c
    JOIN inv p
      ON p.key = c.key
     AND p.last_modified_date = c.last_modified_date
     AND p.version_id <> c.version_id
    UNION ALL
    SELECT c.key, c.version_id,
           s.is_delete_marker AS superseder_is_delete_marker,
           0 AS via_tied_noncurrent_peer
    FROM candidate_base c
    JOIN ts_order t
      ON t.key = c.key AND t.last_modified_date = c.last_modified_date
    JOIN inv s
      ON s.key = c.key AND s.last_modified_date = t.next_lmd
    JOIN group_noncurrent g
      ON g.key = s.key AND g.last_modified_date = s.last_modified_date
    WHERE s.is_latest = false OR g.has_nc = 0
  ),
  superseder_class AS (
    SELECT key, version_id,
           COUNT(DISTINCT superseder_is_delete_marker) AS superseder_kinds,
           MIN(superseder_is_delete_marker) AS superseder_is_delete_marker,
           MAX(via_tied_noncurrent_peer) AS has_tied_noncurrent_peer
    FROM possible_superseders
    GROUP BY key, version_id
  )"""


def _build_candidate_ctes(
    mode, dt, release_mode, glue_table,
    previous_manifest_table,
):
    """Build the shared eligibility CTEs used by COUNT and UNLOAD.

    `either` mode only needs is_latest='false' to establish noncurrency
    -- S3 ensures at most one is_latest='true' entry (version or
    delete marker) per key -- so it needs no superseder lookup and no
    tie handling at all: it is always the fast path, and nothing is ever
    withheld (the withheld-candidates diagnostic is skipped for it).

    `delete`/`overwrite` must additionally classify HOW each candidate
    was superseded (delete marker vs data version). That classification
    comes from the shared possible-superseders predicate (see
    _build_superseder_ctes): a candidate is eligible when its possible
    superseders unanimously agree on is_delete_marker
    (superseder_kinds = 1) AND that unanimous value matches the release
    mode; it is withheld when they disagree (superseder_kinds > 1).

    Every path excludes candidates already present in previous_manifest_table
    -- the manifest CreateJob most recently submitted a non-suspended job
    for, per mode. This is a one-run-back dedup against re-selecting versions
    whose release may already be in flight (e.g. DAILY inventory delivery lag
    exceeding the DetectionSchedule interval) or already succeeded (e.g. a
    stale withheld manifest re-evaluated after SafetyThreshold is raised).
    previous_manifest_table is only ever populated after a job that actually
    runs (ReportOnly=false, under SafetyThreshold), so this is a no-op in
    ReportOnly mode and does not suppress anything a report-only run would
    otherwise show. previous_manifest keys are url_encode()'d the same way
    the manifest UNLOAD encodes them (see _build_query), so the join compares
    like with like.

    That last sentence holds only because PreviousManifestTable declares all
    three of the manifest CSV's columns (bucket, key, version_id). Its SerDe
    maps fields to columns by position, so a column list missing `bucket`
    shifts every field left and this join then matches nothing at all,
    silently. Read the comment on that table's Columns before changing either
    end of this comparison.
    """
    previous_manifest_filter = f"""
    LEFT JOIN (
      SELECT key, version_id FROM {previous_manifest_table}
      WHERE mode = '{mode}'
    ) pm ON pm.key = url_encode(c.key) AND pm.version_id = c.version_id"""

    if release_mode == 'either':
        return f"""WITH candidates AS (
    SELECT c.key, c.version_id
    FROM {glue_table} c{previous_manifest_filter}
    WHERE c.dt = '{dt}'
      AND c.is_latest = false
      AND c.is_delete_marker = false
      AND c.object_lock_event_hold_status = 'ON'
      AND c.object_lock_mode = '{mode}'
      AND pm.version_id IS NULL
  )"""

    if release_mode == 'delete':
        mode_filter = "AND sc.superseder_is_delete_marker = true"
    else:  # overwrite
        mode_filter = "AND sc.superseder_is_delete_marker = false"

    superseder_ctes = _build_superseder_ctes(dt, glue_table)
    return f"""WITH {superseder_ctes},
  candidates AS (
    SELECT c.key, c.version_id
    FROM candidate_base c
    JOIN superseder_class sc
      ON sc.key = c.key AND sc.version_id = c.version_id{previous_manifest_filter}
    WHERE c.object_lock_mode = '{mode}'
      AND sc.superseder_kinds = 1
      AND pm.version_id IS NULL
      {mode_filter}
  )"""


def _build_count_query(
    mode, dt, target_bucket, release_mode,
    glue_table, previous_manifest_table,
):
    """Build the authoritative pre-job eligible-row count query."""
    ctes = _build_candidate_ctes(
        mode, dt, release_mode, glue_table,
        previous_manifest_table,
    )
    return f"""{ctes}
  SELECT COUNT(*) AS eligible_count
  FROM candidates"""


def _build_query(
    mode, dt, output_prefix, solution_bucket, target_bucket,
    release_mode, glue_table, glue_database,
    previous_manifest_table,
):
    """Build the manifest UNLOAD from the shared eligibility CTEs."""
    ctes = _build_candidate_ctes(
        mode, dt, release_mode, glue_table,
        previous_manifest_table,
    )
    return f"""UNLOAD (
  {ctes}
  SELECT '{target_bucket}' AS bucket, url_encode(key) AS key, version_id
  FROM candidates
)
TO '{output_prefix}'
WITH (format='TEXTFILE', field_delimiter=',', compression='NONE')"""


def _build_withheld_candidates_ctes(dt, glue_table):
    """Build the diagnostic CTEs listing candidates withheld by ambiguity.

    Reports exactly the candidates the delete/overwrite eligibility path
    withholds: composed from the SAME possible-superseders predicate
    (_build_superseder_ctes) as _build_candidate_ctes, with the inverted
    condition (superseder_kinds > 1), so withheld and reported cannot
    drift. Union across lock modes: object_lock_mode is carried through
    as a column; ambiguity detection itself is mode-independent, and the
    withheld set is identical for `delete` and `overwrite` (ambiguity is
    "cannot tell delete marker from data version", regardless of which
    one the release mode selects). In `either` mode nothing is ever
    withheld, so the handler skips this diagnostic entirely.

    The reason column distinguishes the two withholding topologies:
    `tied_noncurrent_peer` (the candidate shares its timestamp with
    another noncurrent version) vs `ambiguous_successor_classification`
    (the candidate is not itself tied, but the rows at the next
    timestamp mix a data version and a delete marker).

    No previous_manifest anti-join here, deliberately: the withheld set
    and the pm-deduped set are DISJOINT by construction. A withheld
    candidate produces no job, so it is never written to
    previous_manifest; and a pm-recorded version was released, so its
    hold is OFF next run and it is not a candidate at all.
    """
    superseder_ctes = _build_superseder_ctes(dt, glue_table)
    return f"""WITH {superseder_ctes},
  withheld_candidates AS (
    SELECT c.key, c.version_id, c.object_lock_mode,
           c.last_modified_date,
           CASE WHEN sc.has_tied_noncurrent_peer = 1
                THEN 'tied_noncurrent_peer'
                ELSE 'ambiguous_successor_classification'
           END AS reason
    FROM candidate_base c
    JOIN superseder_class sc
      ON sc.key = c.key AND sc.version_id = c.version_id
    WHERE sc.superseder_kinds > 1
  )"""


def _build_withheld_candidates_count_query(dt, glue_table):
    """Count candidates withheld this run due to timestamp ambiguity."""
    ctes = _build_withheld_candidates_ctes(dt, glue_table)
    return f"""{ctes}
  SELECT COUNT(*) AS withheld_candidate_count
  FROM withheld_candidates"""


def _build_withheld_candidates_query(dt, output_prefix, glue_table):
    """Build the diagnostic UNLOAD listing ambiguity-withheld candidates."""
    ctes = _build_withheld_candidates_ctes(dt, glue_table)
    return f"""UNLOAD (
  {ctes}
  SELECT url_encode(key) AS key, version_id, object_lock_mode,
         last_modified_date, reason
  FROM withheld_candidates
)
TO '{output_prefix}'
WITH (format='TEXTFILE', field_delimiter=',', compression='NONE')"""
