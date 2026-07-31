"""auto-heal remediate Lambda -- the middle step of auto_heal_stack.py's
Step Functions loop (NotifyBefore -> this -> NotifyAfter). Dispatches on
`remediation_type`, one narrow, resource-specific action per type -- see
auto_heal_stack.py's module docstring for the full blast-radius reasoning
(same "one named resource, never a sweep" posture as
drift_remediation_stack.py).
"""

import os

import boto3

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")

LIBRESWAN_INSTANCE_ID = os.environ["LIBRESWAN_INSTANCE_ID"]
LATTICE_TARGET_INSTANCE_ID = os.environ["LATTICE_TARGET_INSTANCE_ID"]


def _restart_vpn_tunnel() -> str:
    # SSM Run Command, not a full instance reboot -- surgical (just restarts
    # the ipsec service to force IKE renegotiation) rather than dropping the
    # instance's EIP/other services for the ~1min a reboot would take.
    resp = ssm.send_command(
        InstanceIds=[LIBRESWAN_INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": ["systemctl restart ipsec"]},
    )
    return f"SSM RunCommand {resp['Command']['CommandId']} sent to restart ipsec on {LIBRESWAN_INSTANCE_ID}"


def _reboot_lattice_target() -> str:
    ec2.reboot_instances(InstanceIds=[LATTICE_TARGET_INSTANCE_ID])
    return f"Rebooted {LATTICE_TARGET_INSTANCE_ID} (Lattice INSTANCE-type target host failed its EC2 status check)"


def _bedrock_throttle_note() -> str:
    return ("No automated remediation exists for Bedrock throttling -- it's an account-level TPS quota, fixable "
            "only via a support-ticket quota increase. This step exists for alert symmetry with the other two "
            "failure modes, not because there's an infrastructure action available.")


REMEDIATIONS = {
    "vpn-tunnel": _restart_vpn_tunnel,
    "lattice-target": _reboot_lattice_target,
    "bedrock-throttle": _bedrock_throttle_note,
}


def handler(event, context):
    remediation_type = event.get("remediation_type")
    action = REMEDIATIONS.get(remediation_type)
    if not action:
        return {"remediation_type": remediation_type, "result": f"unknown remediation_type -- no action taken"}
    return {"remediation_type": remediation_type, "result": action()}
