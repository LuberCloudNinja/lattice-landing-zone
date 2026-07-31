"""rcf-trainer -- scheduled weekly. Kicks off a SageMaker Random Cut Forest
training job (built-in algorithm, CSV/unsupervised) against everything
under PROCESSED_PREFIX so far. Fire-and-forget: training runs async on
SageMaker-managed infra; rcf_model_promoter (triggered by the "SageMaker
Training Job State Change" -> Completed EventBridge rule) picks up the
resulting model artifact and swaps the live Async Inference endpoint's model.
"""

import os
import time

import boto3

sagemaker = boto3.client("sagemaker")

FLOW_LOGS_BUCKET = os.environ["FLOW_LOGS_BUCKET"]
PROCESSED_PREFIX = os.environ.get("PROCESSED_PREFIX", "processed/")
MODEL_BUCKET = os.environ["MODEL_BUCKET"]
TRAINING_IMAGE = os.environ["RCF_TRAINING_IMAGE"]
ROLE_ARN = os.environ["SAGEMAKER_EXECUTION_ROLE_ARN"]
SECURITY_GROUP_ID = os.environ["SAGEMAKER_SECURITY_GROUP_ID"]
SUBNET_IDS = os.environ["SAGEMAKER_SUBNET_IDS"].split(",")
FEATURE_DIM = os.environ.get("RCF_FEATURE_DIM", "7")


def handler(event, context):
    job_name = f"rcf-flowlog-training-{int(time.time())}"
    sagemaker.create_training_job(
        TrainingJobName=job_name,
        AlgorithmSpecification={"TrainingImage": TRAINING_IMAGE, "TrainingInputMode": "File"},
        RoleArn=ROLE_ARN,
        InputDataConfig=[{
            "ChannelName": "train",
            "DataSource": {"S3DataSource": {
                "S3DataType": "S3Prefix",
                "S3Uri": f"s3://{FLOW_LOGS_BUCKET}/{PROCESSED_PREFIX}",
                "S3DataDistributionType": "ShardedByS3Key",
            }},
            "ContentType": "text/csv;label_size=0",
        }],
        OutputDataConfig={"S3OutputPath": f"s3://{MODEL_BUCKET}/training-output/"},
        ResourceConfig={"InstanceType": "ml.m5.large", "InstanceCount": 1, "VolumeSizeInGB": 10},
        StoppingCondition={"MaxRuntimeInSeconds": 3600},
        HyperParameters={
            "feature_dim": FEATURE_DIM,
            "num_trees": "100",
            "num_samples_per_tree": "256",
        },
        VpcConfig={"SecurityGroupIds": [SECURITY_GROUP_ID], "Subnets": SUBNET_IDS},
    )
    return {"training_job_name": job_name}
