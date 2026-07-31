"""query-kafka MCP tool -- read-only status of the on-prem Kafka broker's VPC
Lattice resource gateway/resource-configuration (lattice_stack.py's L4e).

KafkaStack itself (kafka_stack.py) is still a SPEC.md TODO skeleton -- there
is no real Kafka protocol handshake to report on yet, so this honestly
reports Lattice-resource-gateway status (a real, live AWS API) rather than
fabricating Kafka-specific metrics that don't exist.
"""

import os

import boto3

vpc_lattice = boto3.client("vpc-lattice")
ec2 = boto3.client("ec2")

RESOURCE_CONFIGURATION_ID = os.environ["RESOURCE_CONFIGURATION_ID"]
RESOURCE_GATEWAY_ID = os.environ["RESOURCE_GATEWAY_ID"]
BROKER_INSTANCE_ID = os.environ["BROKER_INSTANCE_ID"]


def handler(event, context):
    resource_config = vpc_lattice.get_resource_configuration(
        resourceConfigurationIdentifier=RESOURCE_CONFIGURATION_ID
    )
    resource_gateway = vpc_lattice.get_resource_gateway(
        resourceGatewayIdentifier=RESOURCE_GATEWAY_ID
    )
    instance_status = ec2.describe_instance_status(InstanceIds=[BROKER_INSTANCE_ID])
    statuses = instance_status.get("InstanceStatuses", [])

    return {
        "resource_configuration_status": resource_config.get("status"),
        "resource_configuration_name": resource_config.get("name"),
        "resource_gateway_status": resource_gateway.get("status"),
        "broker_instance_state": statuses[0]["InstanceState"]["Name"] if statuses else "unknown (instance not running or status not yet reporting)",
    }
