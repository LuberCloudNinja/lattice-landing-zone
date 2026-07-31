"""cloudwan_stack.py -- multi-region AWS Cloud WAN backbone, policy-as-code.

Gated behind config.ENABLE_CLOUDWAN (default False) -- see README.md's cost
warning. Home region us-east-1; region2_stack.py (us-east-2) attaches into
the core network this stack creates.

The core network's routing behavior lives entirely in POLICY_DOCUMENT below
(policy-as-code, not click-ops) -- four segments matching the customer's own
vocabulary (config.CloudWanSegment): FastTrack (prod), SkyPath (proxy),
SkyTransit (hybrid/on-prem reachability), Workload. Workload is isolated
(isolate-attachments=true -- nothing in it can reach anything else in it
without an explicit share action, demonstrating segment isolation);
SkyTransit is explicitly shared with FastTrack (segment-actions) to
demonstrate the alternative -- controlled cross-segment reachability.
attachment-policies maps attachments into segments by their own `segment`
tag, so app-vpc/provider-vpc/region2's VPC never need to be manually wired
into a segment -- CfnVpcAttachment tags below do that automatically.

TGW-peering migration path (L5/L6 in the request): this project's existing
Transit Gateway (network_stack.py) is registered into the same Global
Network and PEERED with the core network, then one of its route tables is
attached to a segment -- the documented, supported way to adopt Cloud WAN
incrementally alongside an existing TGW-based network rather than a
rip-and-replace.
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_networkmanager as nm
from aws_cdk import aws_ssm as ssm
from constructs import Construct

import config
from stacks.network_stack import NetworkStack
from stacks.threetier_stack import ThreeTierStack

CORE_NETWORK_ID_SSM_PARAM = "/lattice-lab/cloudwan/core-network-id"

POLICY_DOCUMENT = {
    "version": "2021.12",
    "core-network-configuration": {
        "vpn-ecmp-support": False,
        "asn-ranges": ["64512-64555"],
        "edge-locations": [
            {"location": "us-east-1", "asn": 64512},
            {"location": config.SECOND_REGION, "asn": 64513},
        ],
    },
    "segments": [
        {
            "name": config.CloudWanSegment.FASTTRACK,
            "description": "Production -- shared-with target for SkyTransit (hybrid) reachability.",
            "require-attachment-acceptance": False,
        },
        {
            "name": config.CloudWanSegment.SKYPATH,
            "description": "Proxy segment.",
            "require-attachment-acceptance": False,
        },
        {
            "name": config.CloudWanSegment.SKYTRANSIT,
            "description": "Hybrid -- on-prem/TGW reachability, shared into FastTrack below.",
            "require-attachment-acceptance": False,
        },
        {
            "name": config.CloudWanSegment.WORKLOAD,
            "description": "Isolated by design -- the segment-isolation half of the demo.",
            "require-attachment-acceptance": False,
            "isolate-attachments": True,
        },
    ],
    "segment-actions": [
        {
            "action": "share",
            "mode": "attachment-route",
            "segment": config.CloudWanSegment.SKYTRANSIT,
            "share-with": [config.CloudWanSegment.FASTTRACK],
        },
    ],
    "attachment-policies": [
        {
            "rule-number": 100,
            "condition-logic": "or",
            "conditions": [{"type": "tag-value", "operator": "equals", "key": "segment", "value": config.CloudWanSegment.FASTTRACK}],
            "action": {"association-method": "constant", "segment": config.CloudWanSegment.FASTTRACK},
        },
        {
            "rule-number": 110,
            "condition-logic": "or",
            "conditions": [{"type": "tag-value", "operator": "equals", "key": "segment", "value": config.CloudWanSegment.SKYPATH}],
            "action": {"association-method": "constant", "segment": config.CloudWanSegment.SKYPATH},
        },
        {
            "rule-number": 120,
            "condition-logic": "or",
            "conditions": [{"type": "tag-value", "operator": "equals", "key": "segment", "value": config.CloudWanSegment.SKYTRANSIT}],
            "action": {"association-method": "constant", "segment": config.CloudWanSegment.SKYTRANSIT},
        },
        {
            "rule-number": 130,
            "condition-logic": "or",
            "conditions": [{"type": "tag-value", "operator": "equals", "key": "segment", "value": config.CloudWanSegment.WORKLOAD}],
            "action": {"association-method": "constant", "segment": config.CloudWanSegment.WORKLOAD},
        },
    ],
}


class CloudWanStack(Stack):
    """Global/Core Network (policy-as-code) + us-east-1 VPC attachments + TGW-peering migration path."""

    def __init__(self, scope: Construct, construct_id: str, *, network: NetworkStack, threetier: ThreeTierStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.CLOUDWAN)

        global_network = nm.CfnGlobalNetwork(self, "GlobalNetwork", description="lattice-lab Cloud WAN global network")
        self.core_network = nm.CfnCoreNetwork(
            self, "CoreNetwork",
            global_network_id=global_network.attr_id,
            description="lattice-lab Cloud WAN core network -- FastTrack/SkyPath/SkyTransit/Workload segments",
            policy_document=POLICY_DOCUMENT,
        )

        ssm.StringParameter(
            self, "CoreNetworkIdParam",
            parameter_name=CORE_NETWORK_ID_SSM_PARAM,
            string_value=self.core_network.attr_core_network_id,
            description="Cloud WAN core network id -- read by region2_stack.py's cross-region attachment and by the console tour.",
        )

        # ------------------------------------------------------------------
        # us-east-1 VPC attachments -- app-vpc into Workload (isolated),
        # provider-vpc (this project's PrivateLink shared-service provider,
        # the closest analog to a "shared-services VPC" here) into
        # FastTrack. Tagged `segment=...` -- POLICY_DOCUMENT's
        # attachment-policies do the actual segment association, not this
        # stack wiring it manually.
        # ------------------------------------------------------------------
        app_attach_subnets = network.app_vpc.select_subnets(subnet_group_name="TgwAttach").subnets
        app_attachment = nm.CfnVpcAttachment(
            self, "AppVpcAttachment",
            core_network_id=self.core_network.attr_core_network_id,
            vpc_arn=self.format_arn(service="ec2", resource="vpc", resource_name=network.app_vpc.vpc_id),
            subnet_arns=[self.format_arn(service="ec2", resource="subnet", resource_name=s.subnet_id) for s in app_attach_subnets],
            tags=[cdk.CfnTag(key="segment", value=config.CloudWanSegment.WORKLOAD)],
        )
        app_attachment.add_dependency(self.core_network)

        provider_attach_subnets = network.provider_vpc.select_subnets(subnet_group_name="Private").subnets
        provider_attachment = nm.CfnVpcAttachment(
            self, "ProviderVpcAttachment",
            core_network_id=self.core_network.attr_core_network_id,
            vpc_arn=self.format_arn(service="ec2", resource="vpc", resource_name=network.provider_vpc.vpc_id),
            subnet_arns=[self.format_arn(service="ec2", resource="subnet", resource_name=s.subnet_id) for s in provider_attach_subnets],
            tags=[cdk.CfnTag(key="segment", value=config.CloudWanSegment.FASTTRACK)],
        )
        provider_attachment.add_dependency(self.core_network)

        # ------------------------------------------------------------------
        # TGW-peering migration path -- register the existing TGW into the
        # same Global Network, peer it with the core network, then attach
        # one of its route tables into SkyTransit (the hybrid/on-prem
        # segment -- the natural home for TGW-side reachability, since
        # on-prem/VPN traffic already flows through the TGW's spoke route
        # table today). Incremental: the TGW keeps doing everything it does
        # today (network_stack.py is unchanged) -- this only adds a second,
        # parallel path via Cloud WAN.
        # ------------------------------------------------------------------
        tgw_registration = nm.CfnTransitGatewayRegistration(
            self, "TgwRegistration",
            global_network_id=global_network.attr_id,
            transit_gateway_arn=network.tgw.attr_transit_gateway_arn,
        )
        tgw_peering = nm.CfnTransitGatewayPeering(
            self, "TgwPeering",
            core_network_id=self.core_network.attr_core_network_id,
            transit_gateway_arn=network.tgw.attr_transit_gateway_arn,
        )
        tgw_peering.add_dependency(tgw_registration)
        tgw_peering.add_dependency(self.core_network)

        tgw_route_table_attachment = nm.CfnTransitGatewayRouteTableAttachment(
            self, "TgwRouteTableAttachment",
            peering_id=tgw_peering.attr_peering_id,
            transit_gateway_route_table_arn=self.format_arn(
                service="ec2", resource="transit-gateway-route-table",
                resource_name=network.spoke_route_table.attr_transit_gateway_route_table_id,
            ),
            proposed_segment_change=nm.CfnTransitGatewayRouteTableAttachment.ProposedSegmentChangeProperty(
                segment_name=config.CloudWanSegment.SKYTRANSIT,
            ),
        )
        tgw_route_table_attachment.add_dependency(tgw_peering)

        CfnOutput(self, "GlobalNetworkId", value=global_network.attr_id)
        CfnOutput(self, "CoreNetworkId", value=self.core_network.attr_core_network_id)
        CfnOutput(self, "CoreNetworkArn", value=self.core_network.attr_core_network_arn)
        CfnOutput(self, "AppVpcAttachmentId", value=app_attachment.attr_attachment_id)
        CfnOutput(self, "ProviderVpcAttachmentId", value=provider_attachment.attr_attachment_id)
        CfnOutput(self, "TgwPeeringId", value=tgw_peering.attr_peering_id)
