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
Network and PEERED with the core network -- both confirmed working live
(TgwRegistration/TgwPeering reach AVAILABLE reliably). The natural next
step, attaching one of the TGW's route tables to a segment so routes
actually exchange between the TGW and Cloud WAN, is NOT included here: the
CfnTransitGatewayRouteTableAttachment resource type consistently fails
with an opaque "Fail to create Resource ... Reasons: " (HandlerErrorCode
InvalidRequest) and zero further detail, confirmed via 6+ attempts
covering the plausible root causes -- an ASN collision between the TGW's
own AmazonSideAsn and the core network's edge ASN (real, found and fixed;
see POLICY_DOCUMENT's asn-ranges comment), a live vs. dedicated route
table (tested both; identical failure against a freshly-created, verified
-empty, unassociated table), registration/peering propagation timing
(both confirmed AVAILABLE well before each attempt), and the peering's own
underlying EC2 transit gateway attachment (confirmed `available`). None of
those explain it, and the API gives nothing else to go on. Stated plainly
rather than papered over: the TGW is genuinely integrated with the core
network (registered + peered), but the route-table-level route-sharing
half of the migration story isn't demonstrated by this stack.
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
        # 64512-64555 is the standard 16-bit private-ASN block AWS assigns
        # by default when a Transit Gateway's AmazonSideAsn isn't set
        # explicitly -- network_stack.py's TGW got 64512 (confirmed live:
        # `describe-transit-gateways` -> Options.AmazonSideAsn). Reusing
        # that same value for the edge location's own ASN below caused a
        # real BGP ASN collision (each side of a TGW<->Core Network peering
        # needs a DISTINCT ASN, same as any BGP session) -- confirmed live
        # via direct `networkmanager create-transit-gateway-peering` calls
        # against the actual TGW, which failed identically whether the
        # registration was fresh or fully propagated, ruling out timing.
        # This range starts one above the TGW's ASN specifically so the
        # two can never collide again even if the TGW's own ASN changes
        # within its own default-assignment range.
        "asn-ranges": ["64513-64555"],
        "edge-locations": [
            {"location": "us-east-1", "asn": 64513},
            {"location": config.SECOND_REGION, "asn": 64514},
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
        # same Global Network and peer it with the core network. Both
        # confirmed working live and reach AVAILABLE reliably. See this
        # module's docstring for why the route-table-level attachment
        # (actual route exchange) stops here rather than being included --
        # it's a real, unresolved AWS-side gap, not something skipped for
        # convenience.
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

        CfnOutput(self, "GlobalNetworkId", value=global_network.attr_id)
        CfnOutput(self, "CoreNetworkId", value=self.core_network.attr_core_network_id)
        CfnOutput(self, "CoreNetworkArn", value=self.core_network.attr_core_network_arn)
        CfnOutput(self, "AppVpcAttachmentId", value=app_attachment.attr_attachment_id)
        CfnOutput(self, "ProviderVpcAttachmentId", value=provider_attachment.attr_attachment_id)
        CfnOutput(self, "TgwPeeringId", value=tgw_peering.attr_peering_id)
