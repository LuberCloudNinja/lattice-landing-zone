"""cloudwan-topology MCP tool -- read-only Cloud WAN core network segments +
attachments. Only meaningful when config.ENABLE_CLOUDWAN is on; this Lambda
is only ever deployed at all when that flag is set (agentic_ai_stack.py),
but still defends against a missing/empty CORE_NETWORK_ID for a clean
degrade instead of a raw API error surfaced to the agent.
"""

import os

import boto3

networkmanager = boto3.client("networkmanager")

CORE_NETWORK_ID = os.environ.get("CORE_NETWORK_ID", "")


def handler(event, context):
    if not CORE_NETWORK_ID:
        return {"enabled": False, "message": "Cloud WAN layer is not enabled in this deployment."}

    core_network = networkmanager.get_core_network(CoreNetworkId=CORE_NETWORK_ID)["CoreNetwork"]
    segments = [{"name": s.get("Name"), "edge_locations": s.get("EdgeLocations")} for s in core_network.get("Segments", [])]

    attachments_resp = networkmanager.list_attachments(CoreNetworkId=CORE_NETWORK_ID)
    attachments = [
        {
            "type": a.get("AttachmentType"),
            "segment": a.get("SegmentName"),
            "state": a.get("State"),
            "edge_location": a.get("EdgeLocation"),
        }
        for a in attachments_resp.get("Attachments", [])
    ]

    return {"enabled": True, "core_network_state": core_network.get("State"), "segments": segments, "attachments": attachments}
