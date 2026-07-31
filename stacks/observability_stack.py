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
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_logs as logs
from constructs import Construct

import config
from stacks.inspection_stack import InspectionStack
from stacks.lattice_stack import LatticeStack
from stacks.security_stack import SecurityStack
from stacks.threetier_stack import ThreeTierStack


def _elbv2_dimension_value(arn: str) -> str:
    """Extract the CloudWatch dimension value (e.g. "targetgroup/name/id" or
    "gwy/name/id") from an ELBv2 ARN -- the 6th colon-separated field."""
    return cdk.Fn.select(5, cdk.Fn.split(":", arn))


class ObservabilityStack(Stack):
    """Cross-cutting log groups and CloudWatch dashboards for the other stacks."""

    def __init__(
        self, scope: Construct, construct_id: str, *,
        inspection: InspectionStack, threetier: ThreeTierStack, lattice: LatticeStack,
        security: SecurityStack, **kwargs
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

        # ------------------------------------------------------------------
        # Dashboard -- one "is this all healthy" view across the four
        # layers that have meaningful runtime metrics.
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

        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name="lattice-lab")
        dashboard.add_widgets(
            cw.TextWidget(markdown="# lattice-lab -- interview demo health check", width=24, height=1),
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

        CfnOutput(self, "DashboardUrl", value=f"https://{self.region}.console.aws.amazon.com/cloudwatch/home?region={self.region}#dashboards:name=lattice-lab")
        CfnOutput(self, "SuricataLogGroupName", value=suricata_log_group.log_group_name)
        CfnOutput(self, "NftablesLogGroupName", value=nftables_log_group.log_group_name)
