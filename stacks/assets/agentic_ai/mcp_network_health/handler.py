"""get-network-health MCP tool -- read-only ALB target-group health +
Site-to-Site VPN tunnel status.
"""

import os

import boto3

elbv2 = boto3.client("elbv2")
ec2 = boto3.client("ec2")

APP_TARGET_GROUP_ARN = os.environ["APP_TARGET_GROUP_ARN"]
VPN_CONNECTION_ID = os.environ["VPN_CONNECTION_ID"]


def handler(event, context):
    target_health = elbv2.describe_target_health(TargetGroupArn=APP_TARGET_GROUP_ARN)
    targets = [
        {
            "target": d["Target"].get("Id"),
            "state": d["TargetHealth"].get("State"),
            "reason": d["TargetHealth"].get("Reason"),
        }
        for d in target_health.get("TargetHealthDescriptions", [])
    ]

    vpn = ec2.describe_vpn_connections(VpnConnectionIds=[VPN_CONNECTION_ID])
    vpn_connections = vpn.get("VpnConnections", [])
    tunnels = []
    if vpn_connections:
        tunnels = [
            {"outside_ip": t.get("OutsideIpAddress"), "status": t.get("Status"), "status_message": t.get("StatusMessage")}
            for t in vpn_connections[0].get("VgwTelemetry", [])
        ]

    return {"app_target_group": targets, "vpn_tunnels": tunnels}
