"""flow-log-preprocessor -- scheduled daily. Reads the last day of raw VPC
Flow Logs (default AWS space-delimited format) from RAW_PREFIX, extracts a
numeric feature vector per record, and writes a single CSV batch to
PROCESSED_PREFIX for rcf_trainer/rcf_batch_scorer to consume.

Default flow-log record fields (in order): version account-id interface-id
srcaddr dstaddr srcport dstport protocol packets bytes start end action
log-status. Feature vector extracted here: [srcport, dstport, protocol,
packets, bytes, duration_seconds, action_is_reject].
"""

import csv
import io
import os
from datetime import datetime, timedelta, timezone

import boto3

s3 = boto3.client("s3")

BUCKET = os.environ["FLOW_LOGS_BUCKET"]
RAW_PREFIX = os.environ.get("RAW_PREFIX", "raw/")
PROCESSED_PREFIX = os.environ.get("PROCESSED_PREFIX", "processed/")


def _extract_features(line: str):
    fields = line.split()
    if len(fields) < 13 or fields[0] != "2" or fields[-1] != "OK":
        return None  # skip header/malformed/NODATA records
    try:
        _, _, _, _, _, srcport, dstport, protocol, packets, byte_count, start, end, action = fields[:13]
        duration = int(end) - int(start)
        return [int(srcport), int(dstport), int(protocol), int(packets), int(byte_count), max(duration, 0), 1 if action == "REJECT" else 0]
    except (ValueError, IndexError):
        return None


def handler(event, context):
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    paginator = s3.get_paginator("list_objects_v2")
    rows = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=RAW_PREFIX):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                continue
            body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read().decode("utf-8", errors="ignore")
            for line in body.splitlines():
                features = _extract_features(line)
                if features:
                    rows.append(features)

    if not rows:
        return {"rows_written": 0, "message": "no new flow-log records in the last 24h"}

    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    batch_key = f"{PROCESSED_PREFIX}batch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.csv"
    s3.put_object(Bucket=BUCKET, Key=batch_key, Body=buf.getvalue().encode("utf-8"))

    return {"rows_written": len(rows), "batch_key": batch_key}
