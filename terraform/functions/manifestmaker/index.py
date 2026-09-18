import json
import os
import re
import urllib.parse
import boto3

FIVE_MB = 5 * 1024 * 1024

# S3 Inventory delivery timestamp: YYYY-MM-DDThh-mmZ. Re-validated here
# even though StartQuery already checks it, because dt reaches this
# function through a trigger file rather than directly from the S3 event
# that carried it, and is used to build the diagnostics/withheld-
# candidates/{dt}/ key path. CreateJob re-validates it again for the same
# reason.
_DT_PATTERN = re.compile(
    r'\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}Z\Z'
)

# S3 event sequencer / run_id: hex string, validated before use in
# key paths. Same pattern as StartQuery's _SEQUENCER_PATTERN and
# JobCompletion's _RUN_ID_PATTERN -- without this, a bucket-policy
# bypass could inject a trigger file at a path with a non-hex run_id
# that ManifestMaker accepts but JobCompletion rejects, releasing
# holds with no notification.
_RUN_ID_PATTERN = re.compile(r'\A[0-9A-Fa-f]{1,64}\Z')


def handler(event, context):
    bucket = os.environ['SOLUTION_BUCKET']
    s3 = boto3.client('s3')

    # Parse the S3 event record to get the _query_complete.json key.
    # Key format: manifests/parts/{run_id}/_query_complete.json
    # run_id is the 3rd path segment (index 2).
    record = event['Records'][0]
    raw_key = record['s3']['object']['key']
    key = urllib.parse.unquote_plus(raw_key)
    run_id = key.split('/')[2]

    if not _RUN_ID_PATTERN.match(run_id):
        raise ValueError(
            f"Invalid run_id '{run_id}' in key '{key}'; "
            "expected a hex-encoded S3 event sequencer"
        )

    # Download and parse the trigger file written by StartQuery.
    obj = s3.get_object(Bucket=bucket, Key=key)
    trigger = json.loads(obj['Body'].read())

    # Validate trigger schema and mode values (Req 5.1, 5.2).
    _validate_trigger(trigger, key, required_fields=[
        ('run_id', str), ('dt', str), ('target_bucket', str),
        ('modes_succeeded', list), ('row_counts', dict),
        ('count_query_execution_ids', dict),
        ('count_query_hashes', dict),
    ], mode_field='modes_succeeded')

    # Cross-check the trigger's self-reported run_id against the run_id
    # derived from the S3 event key that actually delivered it. Without
    # this, the run_id written into _manifests_ready.json below is taken
    # from the key alone, so CreateJob's equivalent cross-check compares
    # that value against itself and can never fail -- the check exists
    # there but is inert unless the disagreement is caught here, where
    # both values are still independently available.
    if trigger['run_id'] != run_id:
        raise ValueError(
            f"Trigger file '{key}' run_id '{trigger['run_id']}' does not "
            f"match key-derived run_id '{run_id}'"
        )

    modes_with_manifests = []
    manifest_keys = {}
    row_counts = {}

    for mode in trigger.get('modes_succeeded', []):
        # Set unconditionally, before the no-manifest branch below --
        # CreateJob's count-verification step (_verify_count_query)
        # requires row_counts[mode] for every mode StartQuery counted,
        # including a genuine zero, to tell "verified zero eligible"
        # apart from "metadata missing" (they were previously
        # indistinguishable: a zero-eligible mode never reached this
        # assignment, so CreateJob saw the same missing-key state as a
        # real data-integrity problem and withheld it either way).
        row_counts[mode] = trigger.get('row_counts', {}).get(mode, 0)

        prefix = f'manifests/parts/{run_id}/{mode}/'
        part_keys = _list_part_files(s3, bucket, prefix)

        if not part_keys:
            # No eligible rows for this mode — skip, write no manifest.
            continue

        dest_key = f'manifests/combined/{run_id}/{mode}/manifest.csv'

        # Head each part to learn its size for strategy selection.
        sizes = {}
        total_size = 0
        for pk in part_keys:
            sz = s3.head_object(Bucket=bucket, Key=pk)['ContentLength']
            sizes[pk] = sz
            total_size += sz

        if total_size < FIVE_MB or len(part_keys) == 1:
            # Total < 5MB (or single part): multipart would violate the
            # 5MB minimum for non-final parts, so download, concatenate,
            # and PutObject instead.
            data = b''
            for pk in part_keys:
                data += s3.get_object(Bucket=bucket, Key=pk)['Body'].read()
            s3.put_object(Bucket=bucket, Key=dest_key, Body=data)
        else:
            _combine_parts_multipart(s3, bucket, part_keys, sizes, dest_key)

        modes_with_manifests.append(mode)
        manifest_keys[mode] = dest_key

    # Combine the diagnostic withheld-candidates parts (if StartQuery wrote
    # any) into the fixed, dt-partitioned diagnostics table location. This
    # is informational only -- it never affects modes_with_manifests, and a
    # failure here would fail the whole invocation (retried, then DLQ'd)
    # rather than silently dropping eligibility results, so it is combined
    # using the same helpers as the per-mode manifests above.
    withheld_candidate_count = trigger.get('withheld_candidate_count', 0)
    dt = trigger.get('dt')
    if trigger.get('withheld_manifest_written'):
        withheld_prefix = f'manifests/parts/{run_id}/_WITHHELD/'
        withheld_part_keys = _list_part_files(s3, bucket, withheld_prefix)
        if withheld_part_keys:
            # No "dt=" prefix: matches the raw-value convention already
            # used by the inventory table's injected partition projection
            # (storage.location.template ends in .../${dt}/, not
            # .../dt=${dt}/).
            withheld_dest_key = (
                f'diagnostics/withheld-candidates/{dt}/manifest.csv'
            )
            sizes = {}
            total_size = 0
            for pk in withheld_part_keys:
                sz = s3.head_object(Bucket=bucket, Key=pk)['ContentLength']
                sizes[pk] = sz
                total_size += sz

            if total_size < FIVE_MB or len(withheld_part_keys) == 1:
                data = b''
                for pk in withheld_part_keys:
                    data += s3.get_object(
                        Bucket=bucket, Key=pk
                    )['Body'].read()
                s3.put_object(
                    Bucket=bucket, Key=withheld_dest_key, Body=data,
                )
            else:
                _combine_parts_multipart(
                    s3, bucket, withheld_part_keys, sizes,
                    withheld_dest_key,
                )

    # Write _manifests_ready.json even when no modes produced manifests so
    # CreateJob fires and can report a zero-eligible run (Req 5.1).
    result = {
        'run_id': run_id,
        'dt': trigger.get('dt'),
        'target_bucket': trigger.get('target_bucket'),
        'inventory_id': trigger.get('inventory_id'),
        'modes_with_manifests': modes_with_manifests,
        'manifest_keys': manifest_keys,
        'row_counts': row_counts,
        'withheld_candidate_count': withheld_candidate_count,
        'query_execution_ids': trigger.get('query_execution_ids', {}),
        'count_query_execution_ids': trigger['count_query_execution_ids'],
        'count_query_hashes': trigger['count_query_hashes'],
    }

    # Audit log: structured record of the ManifestMaker run (Req 6.1).
    print(json.dumps({
        'event': 'manifestmaker_complete',
        'run_id': run_id,
        'dt': trigger.get('dt'),
        'modes_with_manifests': modes_with_manifests,
        'manifest_keys': manifest_keys,
        'row_counts': row_counts,
        'withheld_candidate_count': withheld_candidate_count,
    }))
    s3.put_object(
        Bucket=bucket,
        Key=f'manifests/combined/{run_id}/_manifests_ready.json',
        Body=json.dumps(result),
        ContentType='application/json',
    )
    return result


_ALLOWED_MODES = frozenset(['COMPLIANCE', 'GOVERNANCE'])


def _validate_trigger(trigger, source_key, required_fields, mode_field):
    """Validate trigger file schema and mode values (Req 5.1, 5.2).

    Raises ValueError on any missing field, wrong type, or disallowed mode.
    """
    for field_name, expected_type in required_fields:
        value = trigger.get(field_name)
        if value is None:
            raise ValueError(
                f"Trigger file '{source_key}' missing required field "
                f"'{field_name}'"
            )
        if not isinstance(value, expected_type):
            raise ValueError(
                f"Trigger file '{source_key}' field '{field_name}' has type "
                f"{type(value).__name__}, expected {expected_type.__name__}"
            )
        # dt reaches the diagnostics/withheld-candidates/{dt}/ key path
        # below, so a str type check alone does not bound it. Validate the
        # format, matching what StartQuery and CreateJob both already do.
        if field_name == 'dt' and not _DT_PATTERN.match(value):
            raise ValueError(
                f"Trigger file '{source_key}' has invalid dt '{value}'; "
                "expected YYYY-MM-DDThh-mmZ"
            )

    modes = trigger.get(mode_field, [])
    for mode in modes:
        if mode not in _ALLOWED_MODES:
            raise ValueError(
                f"Trigger file '{source_key}' contains disallowed mode "
                f"'{mode}'; allowed: {sorted(_ALLOWED_MODES)}"
            )


def _list_part_files(s3, bucket, prefix):
    """List sorted part file keys under prefix, skipping '_'-prefixed names."""
    part_keys = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            name = obj['Key'].split('/')[-1]
            if not name.startswith('_'):
                part_keys.append(obj['Key'])
    part_keys.sort()
    return part_keys


def _combine_parts_multipart(s3, bucket, part_keys, sizes, dest_key):
    """Combine sorted part files into a single object via multipart UploadPartCopy.

    Buffers consecutive sub-5MB parts until the buffer reaches the 5MB
    minimum required for all non-final multipart parts, then flushes by
    downloading the buffered parts and calling upload_part. Single parts
    that are >= 5MB are copied directly via upload_part_copy without
    downloading. The final part may be any size.

    On any exception the multipart upload is aborted before re-raising so
    that incomplete uploads do not linger in the bucket.
    """
    mpu = s3.create_multipart_upload(Bucket=bucket, Key=dest_key)
    upload_id = mpu['UploadId']
    parts = []
    part_num = 1
    buffer_keys = []
    buffer_size = 0

    try:
        for i, part_key in enumerate(part_keys):
            size = sizes[part_key]
            buffer_keys.append(part_key)
            buffer_size += size
            is_last = (i == len(part_keys) - 1)

            if buffer_size >= FIVE_MB or is_last:
                if len(buffer_keys) == 1 and buffer_size >= FIVE_MB:
                    # Single large part: copy directly via UploadPartCopy.
                    resp = s3.upload_part_copy(
                        Bucket=bucket,
                        Key=dest_key,
                        UploadId=upload_id,
                        PartNumber=part_num,
                        CopySource={'Bucket': bucket, 'Key': buffer_keys[0]},
                    )
                    parts.append({
                        'PartNumber': part_num,
                        'ETag': resp['CopyPartResult']['ETag'],
                    })
                else:
                    # Multiple small parts or final flush: download and re-upload.
                    data = b''
                    for bk in buffer_keys:
                        data += s3.get_object(
                            Bucket=bucket, Key=bk
                        )['Body'].read()
                    resp = s3.upload_part(
                        Bucket=bucket,
                        Key=dest_key,
                        UploadId=upload_id,
                        PartNumber=part_num,
                        Body=data,
                    )
                    parts.append({
                        'PartNumber': part_num,
                        'ETag': resp['ETag'],
                    })
                part_num += 1
                buffer_keys = []
                buffer_size = 0

        s3.complete_multipart_upload(
            Bucket=bucket,
            Key=dest_key,
            UploadId=upload_id,
            MultipartUpload={'Parts': parts},
        )
    except Exception:
        s3.abort_multipart_upload(
            Bucket=bucket, Key=dest_key, UploadId=upload_id
        )
        raise
