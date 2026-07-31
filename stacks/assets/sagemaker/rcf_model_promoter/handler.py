"""rcf-model-promoter -- triggered by EventBridge on a SageMaker Training Job
State Change event with status=Completed. Creates a new SageMaker Model +
EndpointConfig pointing at the newly trained artifact, then either creates
the Async Inference endpoint (first successful training run) or updates it
in place (every run after) -- closing the retrain loop started by
rcf_trainer. A failed/stopped training job is a no-op (nothing to promote).

Model/EndpointConfig names are immutable-per-generation (CreateModel/
CreateEndpointConfig can't be updated in place), so each promotion creates a
new NAME_PREFIX-suffixed generation and best-effort deletes the PREVIOUS
one afterward -- bounds steady-state orphan accumulation to at most one
extra generation between promotions. teardown_cleanup/handler.py (wired to
`cdk destroy` via a custom resource) sweeps up anything this misses.
"""

import os
import time

import boto3

sagemaker = boto3.client("sagemaker")
appautoscaling = boto3.client("application-autoscaling")

TRAINING_IMAGE = os.environ["RCF_TRAINING_IMAGE"]
ROLE_ARN = os.environ["SAGEMAKER_EXECUTION_ROLE_ARN"]
SECURITY_GROUP_ID = os.environ["SAGEMAKER_SECURITY_GROUP_ID"]
SUBNET_IDS = os.environ["SAGEMAKER_SUBNET_IDS"].split(",")
ENDPOINT_NAME = os.environ["ENDPOINT_NAME"]
ASYNC_OUTPUT_S3_PATH = os.environ["ASYNC_OUTPUT_S3_PATH"]
KMS_KEY_ID = os.environ["KMS_KEY_ID"]
SUCCESS_TOPIC_ARN = os.environ["SUCCESS_TOPIC_ARN"]
ERROR_TOPIC_ARN = os.environ["ERROR_TOPIC_ARN"]
MAX_CAPACITY = int(os.environ.get("MAX_CAPACITY", "1"))


def _endpoint_exists() -> tuple:
    try:
        ep = sagemaker.describe_endpoint(EndpointName=ENDPOINT_NAME)
        cfg = sagemaker.describe_endpoint_config(EndpointConfigName=ep["EndpointConfigName"])
        return True, cfg["EndpointConfigName"], cfg["ProductionVariants"][0]["ModelName"]
    except sagemaker.exceptions.ResourceNotFound:
        return False, None, None


def _register_autoscaling():
    resource_id = f"endpoint/{ENDPOINT_NAME}/variant/primary"
    appautoscaling.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=resource_id,
        ScalableDimension="sagemaker:variant:DesiredInstanceCount",
        MinCapacity=0,
        MaxCapacity=MAX_CAPACITY,
    )
    appautoscaling.put_scaling_policy(
        PolicyName="rcf-flowlog-scale-to-zero",
        ServiceNamespace="sagemaker",
        ResourceId=resource_id,
        ScalableDimension="sagemaker:variant:DesiredInstanceCount",
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "TargetValue": 5.0,
            "CustomizedMetricSpecification": {
                "MetricName": "ApproximateBacklogSizePerInstance",
                "Namespace": "AWS/SageMaker",
                "Dimensions": [{"Name": "EndpointName", "Value": ENDPOINT_NAME}],
                "Statistic": "Average",
            },
            "ScaleInCooldown": 600,
            "ScaleOutCooldown": 60,
        },
    )


def handler(event, context):
    detail = event.get("detail", {})
    if detail.get("TrainingJobStatus") != "Completed":
        return {"promoted": False, "reason": detail.get("TrainingJobStatus", "unknown status")}

    job_name = detail["TrainingJobName"]
    model_artifacts = detail["ModelArtifacts"]["S3ModelArtifacts"]
    suffix = int(time.time())

    already_exists, previous_config_name, previous_model_name = _endpoint_exists()

    model_name = f"rcf-flowlog-model-{suffix}"
    sagemaker.create_model(
        ModelName=model_name,
        PrimaryContainer={"Image": TRAINING_IMAGE, "ModelDataUrl": model_artifacts},
        ExecutionRoleArn=ROLE_ARN,
        VpcConfig={"SecurityGroupIds": [SECURITY_GROUP_ID], "Subnets": SUBNET_IDS},
    )

    config_name = f"rcf-flowlog-endpoint-config-{suffix}"
    sagemaker.create_endpoint_config(
        EndpointConfigName=config_name,
        ProductionVariants=[{
            "VariantName": "primary",
            "ModelName": model_name,
            "InstanceType": "ml.m5.large",
            "InitialInstanceCount": 1,
        }],
        AsyncInferenceConfig={
            "OutputConfig": {
                "S3OutputPath": ASYNC_OUTPUT_S3_PATH,
                "KmsKeyId": KMS_KEY_ID,
                "NotificationConfig": {"SuccessTopic": SUCCESS_TOPIC_ARN, "ErrorTopic": ERROR_TOPIC_ARN},
            },
        },
    )

    if already_exists:
        sagemaker.update_endpoint(EndpointName=ENDPOINT_NAME, EndpointConfigName=config_name)
    else:
        sagemaker.create_endpoint(EndpointName=ENDPOINT_NAME, EndpointConfigName=config_name)
        _register_autoscaling()

    if previous_config_name:
        try:
            sagemaker.delete_endpoint_config(EndpointConfigName=previous_config_name)
        except Exception:  # noqa: BLE001 -- best-effort; teardown_cleanup sweeps anything left over
            pass
    if previous_model_name:
        try:
            sagemaker.delete_model(ModelName=previous_model_name)
        except Exception:  # noqa: BLE001
            pass

    return {"promoted": True, "training_job_name": job_name, "model_name": model_name, "endpoint_config_name": config_name, "first_creation": not already_exists}
