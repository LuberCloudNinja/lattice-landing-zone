"""rcf-batch-scorer -- scheduled every 6h. Runs a SageMaker Batch Transform
job (a systematic bulk sweep, distinct from the Async Inference endpoint's
on-demand path) using the CURRENTLY PROMOTED model against every processed
flow-log batch written since the last run. rcf_findings_processor picks up
the result via the "SageMaker Transform Job State Change" -> Completed rule.
"""

import os
import time

import boto3

sagemaker = boto3.client("sagemaker")

FLOW_LOGS_BUCKET = os.environ["FLOW_LOGS_BUCKET"]
PROCESSED_PREFIX = os.environ.get("PROCESSED_PREFIX", "processed/")
MODEL_BUCKET = os.environ["MODEL_BUCKET"]
ENDPOINT_NAME = os.environ["ENDPOINT_NAME"]
SECURITY_GROUP_ID = os.environ["SAGEMAKER_SECURITY_GROUP_ID"]
SUBNET_IDS = os.environ["SAGEMAKER_SUBNET_IDS"].split(",")


def _current_model_name() -> str | None:
    endpoint = sagemaker.describe_endpoint(EndpointName=ENDPOINT_NAME)
    if endpoint["EndpointStatus"] not in ("InService", "Updating"):
        return None
    config = sagemaker.describe_endpoint_config(EndpointConfigName=endpoint["EndpointConfigName"])
    return config["ProductionVariants"][0]["ModelName"]


def handler(event, context):
    model_name = _current_model_name()
    if not model_name:
        return {"submitted": False, "reason": "endpoint has no InService model yet -- no training run has been promoted"}

    job_name = f"rcf-batch-score-{int(time.time())}"
    sagemaker.create_transform_job(
        TransformJobName=job_name,
        ModelName=model_name,
        TransformInput={
            "DataSource": {"S3DataSource": {"S3DataType": "S3Prefix", "S3Uri": f"s3://{FLOW_LOGS_BUCKET}/{PROCESSED_PREFIX}"}},
            "ContentType": "text/csv",
            "SplitType": "Line",
        },
        # No DataProcessing.JoinSource -- output is one RCF "{"score": <float>}"
        # JSON-lines record per input row, 1:1 with input order. rcf_findings_
        # processor/handler.py's docstring notes this is best-verified-from-docs,
        # not yet confirmed against a live transform job's actual output.
        TransformOutput={"S3OutputPath": f"s3://{MODEL_BUCKET}/transform-output/{job_name}/", "AssembleWith": "Line", "Accept": "application/jsonlines"},
        TransformResources={"InstanceType": "ml.m5.large", "InstanceCount": 1},
    )
    return {"submitted": True, "transform_job_name": job_name, "model_name": model_name}
