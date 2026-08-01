"""observability_stack.py -- log groups, access logs, metrics dashboards.

SPEC.md's repo-layout comment (Section 3) is the only explicit scope given for
this stack; it's the natural home for anything cross-cutting that the other
stacks emit into rather than own themselves.

Note on the Lattice service-network/service access-log destinations
specifically: those already live in lattice_stack.py (a CloudWatch Logs
group + an S3 bucket), created there because L4f needed them immediately to
wire CfnAccessLogSubscription. Not duplicated here -- this stack's log
groups are for the inspection fleet's Suricata/nftables logs, which nothing
else owns yet.

Known gap, stated plainly rather than left silent: the two log groups below
(Suricata eve.json, nftables PL) are created with the right names/retention
for docs/inspection-architecture-reference.md Section 5.3's design, but
inspection_stack.py's launch-template user-data does NOT yet install/
configure the CloudWatch Agent to actually ship /var/log/... into them --
they exist and cost nothing while empty, but nothing populates them until
that agent config is added as a follow-up.

Deployed AFTER GovernanceStack + DriftRemediationStack (see stage.py) --
those two used to deploy after this stack, but this stack's dashboard now
graphs governance_stack.py's custom metrics and drift_remediation_stack.py's
Lambda directly, which needs them to exist first. Cloud WAN/Agentic AI/
SageMaker/auto-heal are all OPTIONAL, conditionally-created stacks that
deploy even later -- their dashboard sections are added from
auto_heal_stack.py instead, via the same cross-stack `dashboard.add_widgets`
mutation pattern used there already (this stack's `self.dashboard` is a
plain CDK construct reference, not scoped to only be mutated from this
file's own code).

One "is everything healthy" dashboard, one section per architecture layer
that has anything meaningful to graph. Layers deliberately left off:
KafkaStack (still a SPEC.md TODO skeleton, nothing running to graph),
DiagramStack (a static CloudFront+S3 site, no runtime metrics worth a
widget), Region2Stack (an SSM-only test instance with no real workload --
nothing meaningful to graph beyond "does it exist," already covered by
CloudFormation's own stack-status).
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_logs as logs
from constructs import Construct

import config
from stacks.drift_remediation_stack import DriftRemediationStack
from stacks.governance_stack import GovernanceStack
from stacks.inspection_stack import InspectionStack
from stacks.lattice_stack import LatticeStack
from stacks.network_stack import NetworkStack
from stacks.privatelink_stack import PrivateLinkStack
from stacks.security_stack import SecurityStack
from stacks.threetier_stack import ThreeTierStack


def _elbv2_dimension_value(arn: str) -> str:
    """Extract the CloudWatch dimension value (e.g. "targetgroup/name/id" or
    "gwy/name/id") from an ELBv2 ARN -- the 6th colon-separated field. Same
    extraction works for ALB/NLB/GWLB alike (confirmed via research for
    NLB specifically -- AWS/NetworkELB uses the identical ARN-suffix
    format as AWS/GatewayELB/AWS/ApplicationELB)."""
    return cdk.Fn.select(5, cdk.Fn.split(":", arn))


class ObservabilityStack(Stack):
    """Cross-cutting log groups and CloudWatch dashboards for the other stacks."""

    def __init__(
        self, scope: Construct, construct_id: str, *,
        network: NetworkStack, inspection: InspectionStack, privatelink: PrivateLinkStack,
        threetier: ThreeTierStack, lattice: LatticeStack, governance: GovernanceStack,
        drift_remediation: DriftRemediationStack, security: SecurityStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.OBSERVABILITY)

        # ------------------------------------------------------------------
        # Log groups -- inspection fleet only (see module docstring for why
        # Lattice's own log destinations aren't duplicated here).
        # ------------------------------------------------------------------
        suricata_log_group = logs.LogGroup(
            self, "SuricataEveLogGroup",
            log_group_name="/inspection-vpc/suricata-eve",
            retention=logs.RetentionDays.ONE_WEEK,
            encryption_key=security.logs_key,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        nftables_log_group = logs.LogGroup(
            self, "NftablesPlLogGroup",
            log_group_name="/inspection-vpc/nft-pl",
            retention=logs.RetentionDays.ONE_WEEK,
            encryption_key=security.logs_key,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.dashboard = dashboard = cw.Dashboard(self, "Dashboard", dashboard_name="lattice-lab")
        dashboard.add_widgets(
            cw.TextWidget(markdown="# lattice-lab -- interview demo health check", width=24, height=1),
        )

        # ------------------------------------------------------------------
        # Inspection + Lattice + ThreeTier (unchanged from the original
        # build -- see git history for when each was added).
        # ------------------------------------------------------------------
        gwlb_arn_suffix = _elbv2_dimension_value(inspection.gwlb.attr_load_balancer_arn)
        firewall_tg_arn_suffix = _elbv2_dimension_value(inspection.target_group.ref)

        gwlb_healthy = cw.Metric(
            namespace="AWS/GatewayELB", metric_name="HealthyHostCount",
            dimensions_map={"LoadBalancer": gwlb_arn_suffix, "TargetGroup": firewall_tg_arn_suffix},
            statistic="Average", period=Duration.minutes(5),
        )
        gwlb_unhealthy = cw.Metric(
            namespace="AWS/GatewayELB", metric_name="UnHealthyHostCount",
            dimensions_map={"LoadBalancer": gwlb_arn_suffix, "TargetGroup": firewall_tg_arn_suffix},
            statistic="Average", period=Duration.minutes(5),
        )

        # AWS/VpcLattice namespace, "Service" dimension -- per AWS docs the
        # console filters "by name," so this uses the service's configured
        # Name (a stable string this stack controls, not a token) rather
        # than its generated id/arn; worth confirming against real console
        # output post-deploy if these come back empty.
        lattice_requests = cw.Metric(
            namespace="AWS/VpcLattice", metric_name="TotalRequestCount",
            dimensions_map={"Service": "lattice-lab-app"},
            statistic="Sum", period=Duration.minutes(5),
        )
        lattice_5xx = cw.Metric(
            namespace="AWS/VpcLattice", metric_name="HTTPCode_5XX_Count",
            dimensions_map={"Service": "lattice-lab-app"},
            statistic="Sum", period=Duration.minutes(5),
        )

        dashboard.add_widgets(
            cw.GraphWidget(title="GWLB firewall fleet health", left=[gwlb_healthy], right=[gwlb_unhealthy], width=12, height=6),
            cw.GraphWidget(title="Lattice service traffic", left=[lattice_requests], right=[lattice_5xx], width=12, height=6),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="App-tier ALB",
                left=[threetier.alb.metrics.request_count(), threetier.app_target_group.metrics.healthy_host_count()],
                right=[threetier.app_target_group.metrics.target_response_time()],
                width=12, height=6,
            ),
            cw.GraphWidget(
                title="DynamoDB",
                left=[threetier.database.metric_consumed_read_capacity_units(), threetier.database.metric_consumed_write_capacity_units()],
                right=[threetier.database.metric_throttled_requests()],
                width=12, height=6,
            ),
        )

        # ------------------------------------------------------------------
        # Network -- VPN tunnel, Transit Gateway, both NAT paths. Every
        # metric/dimension here verified via research before writing (AWS/
        # VPN TunnelState+VpnId already confirmed live for auto_heal_stack.py's
        # alarm; AWS/TransitGateway+AWS/NATGateway+AWS/EC2 confirmed via a
        # dedicated research pass, including the "the managed AppNatGateway
        # gets AWS/NATGateway metrics, the self-managed inspection_nat_
        # instance EC2 NAT does NOT and needs AWS/EC2 instance metrics
        # instead" distinction).
        # ------------------------------------------------------------------
        vpn_tunnel_state = cw.Metric(
            namespace="AWS/VPN", metric_name="TunnelState",
            dimensions_map={"VpnId": network.vpn_connection.attr_vpn_connection_id},
            statistic="Average", period=Duration.minutes(5),
        )
        tgw_bytes = [
            cw.Metric(namespace="AWS/TransitGateway", metric_name=m, dimensions_map={"TransitGateway": network.tgw.ref}, statistic="Sum", period=Duration.minutes(5))
            for m in ("BytesIn", "BytesOut")
        ]
        tgw_drops = [
            cw.Metric(namespace="AWS/TransitGateway", metric_name=m, dimensions_map={"TransitGateway": network.tgw.ref}, statistic="Sum", period=Duration.minutes(5))
            for m in ("PacketDropCountBlackhole", "PacketDropCountNoRoute")
        ]
        nat_gateway_bytes = cw.Metric(
            namespace="AWS/NATGateway", metric_name="BytesOutToDestination",
            dimensions_map={"NatGatewayId": network.nat_gateway.ref}, statistic="Sum", period=Duration.minutes(5),
        )
        nat_gateway_errors = [
            cw.Metric(namespace="AWS/NATGateway", metric_name=m, dimensions_map={"NatGatewayId": network.nat_gateway.ref}, statistic="Sum", period=Duration.minutes(5))
            for m in ("PacketsDropCount", "ErrorPortAllocation")
        ]
        inspection_nat_status = cw.Metric(
            namespace="AWS/EC2", metric_name="StatusCheckFailed",
            dimensions_map={"InstanceId": network.inspection_nat_instance.instance_id}, statistic="Maximum", period=Duration.minutes(5),
        )
        inspection_nat_cpu = cw.Metric(
            namespace="AWS/EC2", metric_name="CPUUtilization",
            dimensions_map={"InstanceId": network.inspection_nat_instance.instance_id}, statistic="Average", period=Duration.minutes(5),
        )

        dashboard.add_widgets(cw.TextWidget(markdown="# Network", width=24, height=1))
        dashboard.add_widgets(
            cw.GraphWidget(title="Site-to-Site VPN tunnel (0=DOWN, 1=UP)", left=[vpn_tunnel_state], width=8, height=6),
            cw.GraphWidget(title="Transit Gateway traffic", left=tgw_bytes, right=tgw_drops, width=8, height=6),
            cw.GraphWidget(title="App NAT Gateway (managed)", left=[nat_gateway_bytes], right=nat_gateway_errors, width=8, height=6),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Inspection NAT instance (self-managed EC2, not a managed NAT Gateway)",
                left=[inspection_nat_cpu], right=[inspection_nat_status], width=12, height=6,
            ),
        )

        # ------------------------------------------------------------------
        # PrivateLink -- provider NLB health + traffic. Same ARN-suffix
        # dimension extraction as GWLB/ALB above; confirmed via research
        # that AWS/NetworkELB uses the identical format.
        # ------------------------------------------------------------------
        nlb_arn_suffix = _elbv2_dimension_value(privatelink.nlb.load_balancer_arn)
        provider_tg_arn_suffix = _elbv2_dimension_value(privatelink.provider_target_group.target_group_arn)
        nlb_flow = [
            cw.Metric(namespace="AWS/NetworkELB", metric_name=m, dimensions_map={"LoadBalancer": nlb_arn_suffix}, statistic="Sum" if m == "NewFlowCount" else "Average", period=Duration.minutes(5))
            for m in ("ActiveFlowCount", "NewFlowCount")
        ]

        dashboard.add_widgets(cw.TextWidget(markdown="# PrivateLink", width=24, height=1))
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Provider NLB health",
                left=[privatelink.provider_target_group.metric_healthy_host_count(), privatelink.provider_target_group.metric_un_healthy_host_count()],
                width=12, height=6,
            ),
            cw.GraphWidget(title="Provider NLB traffic", left=nlb_flow, width=12, height=6),
        )

        # ------------------------------------------------------------------
        # Governance -- CloudTrail (Logs Insights query -- CloudTrail has no
        # native CW metric for event volume, confirmed via research), plus
        # governance_stack.py's own custom LatticeLab/Governance metrics
        # (Config/Security Hub/GuardDuty have no native "findings over
        # time" metric either -- see that stack's GovernanceMetricsPublisher
        # Lambda for why this is a custom namespace, not native AWS ones).
        # ------------------------------------------------------------------
        governance_custom_metrics = [
            cw.Metric(namespace="LatticeLab/Governance", metric_name=m, statistic="Maximum", period=Duration.minutes(15))
            for m in ("NonCompliantConfigRules", "ActiveFindingsCritical", "ActiveFindingsHigh", "ActiveFindingsMedium", "ActiveFindingsLow", "GuardDutyFindingsTotal")
        ]

        dashboard.add_widgets(cw.TextWidget(markdown="# Governance", width=24, height=1))
        dashboard.add_widgets(
            cw.GraphWidget(title="Config compliance + Security Hub + GuardDuty findings", left=governance_custom_metrics, width=12, height=6),
            cw.LogQueryWidget(
                title="CloudTrail event volume (management events)",
                log_group_names=[governance.trail_log_group.log_group_name],
                query_lines=[
                    "fields @timestamp, eventName",
                    "stats count(*) as events by bin(5m)",
                ],
                view=cw.LogQueryVisualizationType.LINE,
                width=12, height=6,
            ),
        )

        # ------------------------------------------------------------------
        # Drift Remediation -- the responsive half of governance.
        # ------------------------------------------------------------------
        dashboard.add_widgets(cw.TextWidget(markdown="# Drift Remediation", width=24, height=1))
        dashboard.add_widgets(
            cw.GraphWidget(
                title="drift-remediator Lambda",
                left=[drift_remediation.remediator_fn.metric_invocations()],
                right=[drift_remediation.remediator_fn.metric_errors()],
                width=12, height=6,
            ),
            cw.GraphWidget(
                title="governance-alerts SNS",
                left=[drift_remediation.alerts_topic.metric_number_of_messages_published()],
                width=12, height=6,
            ),
        )

        CfnOutput(self, "DashboardUrl", value=f"https://{self.region}.console.aws.amazon.com/cloudwatch/home?region={self.region}#dashboards:name=lattice-lab")
        CfnOutput(self, "SuricataLogGroupName", value=suricata_log_group.log_group_name)
        CfnOutput(self, "NftablesLogGroupName", value=nftables_log_group.log_group_name)
