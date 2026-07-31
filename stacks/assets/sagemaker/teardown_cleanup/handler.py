"""Custom-resource cleanup handler -- ONLY acts on CloudFormation Delete.

The RCF endpoint/model/endpoint-config lifecycle (rcf_model_promoter/
handler.py) is deliberately NOT CDK-managed -- CreateModel/CreateEndpointConfig
are immutable, so each retrain promotion creates a new NAME_PREFIX-suffixed
generation, which CDK/CloudFormation has no way to track or delete (it never
created them). Without this, `cdk destroy` would leave every promoted
model/endpoint-config/endpoint/autoscaling-target behind as a real, billable
orphan -- directly against this project's "fully destroyable" goal. This
handler sweeps everything under NAME_PREFIX on delete instead.
"""

import os

import boto3

sagemaker = boto3.client("sagemaker")
appautoscaling = boto3.client("application-autoscaling")

NAME_PREFIX = os.environ.get("NAME_PREFIX", "rcf-flowlog-")


def _ignore_not_found(fn, **kwargs):
    try:
        fn(**kwargs)
    except Exception as e:  # noqa: BLE001 -- best-effort teardown sweep, don't let one already-gone resource abort the rest
        if "ValidationException" not in str(e) and "not found" not in str(e).lower():
            raise


def handler(event, context):
    if event.get("RequestType") != "Delete":
        return {"PhysicalResourceId": "rcf-teardown-cleanup"}

    endpoints = sagemaker.list_endpoints(NameContains=NAME_PREFIX).get("Endpoints", [])
    for ep in endpoints:
        name = ep["EndpointName"]
        _ignore_not_found(appautoscaling.deregister_scalable_target,
                           ServiceNamespace="sagemaker",
                           ResourceId=f"endpoint/{name}/variant/primary",
                           ScalableDimension="sagemaker:variant:DesiredInstanceCount")
        _ignore_not_found(sagemaker.delete_endpoint, EndpointName=name)

    for cfg in sagemaker.list_endpoint_configs(NameContains=NAME_PREFIX).get("EndpointConfigs", []):
        _ignore_not_found(sagemaker.delete_endpoint_config, EndpointConfigName=cfg["EndpointConfigName"])

    for model in sagemaker.list_models(NameContains=NAME_PREFIX).get("Models", []):
        _ignore_not_found(sagemaker.delete_model, ModelName=model["ModelName"])

    return {"PhysicalResourceId": "rcf-teardown-cleanup"}
