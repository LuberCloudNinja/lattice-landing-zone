"""rcf-findings-processor -- triggered by EventBridge on a SageMaker
Transform Job State Change event with status=Completed. Reads the batch
scorer's JSON-lines RCF output, keeps records above ANOMALY_SCORE_THRESHOLD
(3.0 -- the "3 standard deviations" rule of thumb from AWS's own RCF intro
material), and writes each as a finding to DynamoDB + a durable S3 copy,
then publishes a summary to SNS if anything qualified.

NOTE: RCF's exact JSON-lines output shape ({"score": <float>} per line, 1:1
with input row order) is this project's best-verified-from-docs
understanding, not yet confirmed against a live transform job's actual
output -- same "provisioned honestly, first real run may need a follow-up
fix" posture as agentic_ai_stack.py's AgentCore Runtime stub.
"""

import json
import os
import time
from urllib.parse import urlparse

import boto3

s3 = boto3.client("s3")
sns = boto3.client("sns")
dynamodb = boto3.resource("dynamodb")

ANOMALY_FINDINGS_TABLE = os.environ["ANOMALY_FINDINGS_TABLE"]
FINDINGS_BUCKET = os.environ["FINDINGS_BUCKET"]
TOPIC_ARN = os.environ["TOPIC_ARN"]
ANOMALY_SCORE_THRESHOLD = float(os.environ.get("ANOMALY_SCORE_THRESHOLD", "3.0"))


def handler(event, context):
    detail = event.get("detail", {})
    if detail.get("TransformJobStatus") != "Completed":
        return {"processed": False, "reason": detail.get("TransformJobStatus", "unknown status")}

    output_path = detail["TransformOutput"]["S3OutputPath"]
    parsed = urlparse(output_path)
    bucket, prefix = parsed.netloc, parsed.path.lstrip("/")

    anomalies = []
    for obj in s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", []):
        body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode("utf-8", errors="ignore")
        for line in body.splitlines():
            if not line.strip():
                continue
            try:
                score = json.loads(line).get("score")
            except json.JSONDecodeError:
                continue
            if score is not None and score >= ANOMALY_SCORE_THRESHOLD:
                anomalies.append(score)

    if not anomalies:
        return {"processed": True, "anomalies_found": 0}

    now = int(time.time())
    table = dynamodb.Table(ANOMALY_FINDINGS_TABLE)
    table.put_item(Item={
        "finding_type": "vpc-flow-log-anomaly",
        "detected_at": now,
        "anomaly_score": max(anomalies),
        "flow_log_summary": f"{len(anomalies)} flow-log records scored >= {ANOMALY_SCORE_THRESHOLD} (max {max(anomalies):.2f}) in transform job {detail['TransformJobName']}",
    })
    s3.put_object(
        Bucket=FINDINGS_BUCKET,
        Key=f"findings/{now}.json",
        Body=json.dumps({"transform_job": detail["TransformJobName"], "scores": anomalies, "detected_at": now}).encode("utf-8"),
    )
    sns.publish(
        TopicArn=TOPIC_ARN,
        Subject="VPC Flow Log anomaly detected",
        Message=f"{len(anomalies)} anomalous flow-log records detected (max RCF score {max(anomalies):.2f}) -- see the detect_anomalies MCP tool or the anomaly-findings DynamoDB table for details.",
    )

    return {"processed": True, "anomalies_found": len(anomalies)}
