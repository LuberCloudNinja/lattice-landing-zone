"""blog_analytics_stack.py -- read analytics for the airport-story blog post
(app/frontend-next/app/blog/hybrid-cloud-airport-story) and, generally, any
future page that uses the same AnalyticsBeacon client component.

Client -> CloudFront (/analytics/* behavior added onto ThreeTierStack's
existing distribution, same pattern as auto_heal_stack.py extending
observability_stack.py's dashboard from a different stack) -> API Gateway
HTTP API -> Lambda -> DynamoDB (raw, TTL'd events) + CloudWatch custom
metrics (permanent aggregates, survive the TTL). Widgets are added onto the
existing lattice-lab dashboard (observability_stack.py), not a second
dashboard -- same "one paved path" reasoning as every other optional layer
in this project.

Deploys unconditionally, like governance_stack.py -- API Gateway + a single
low-traffic Lambda + on-demand DynamoDB is lab-scale cents, not the kind of
cost that needs a config.py feature flag.

Privacy: this stack stores source IP address and CloudFront-derived country
per raw event, because the project owner explicitly asked for per-visitor
geographic/technical analytics on their own public-facing blog -- see
handler.py's own docstring and README.md's Blog Analytics section for the
retention (TTL) and disclosure notes. This is standard first-party web
analytics (comparable to what CloudFront access logs or Google Analytics
already capture by default), not something novel to this stack.
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.observability_stack import ObservabilityStack
from stacks.security_stack import SecurityStack
from stacks.threetier_stack import ThreeTierStack

ASSETS_DIR = Path(__file__).parent / "assets" / "blog_analytics"
METRICS_NAMESPACE = "LatticeLab/BlogAnalytics"
TTL_DAYS = 180


class BlogAnalyticsStack(Stack):
    """API Gateway + Lambda + DynamoDB read-analytics for the blog, wired
    into ThreeTierStack's existing CloudFront distribution and
    ObservabilityStack's existing dashboard."""

    def __init__(
        self, scope: Construct, construct_id: str, *,
        security: SecurityStack, threetier: ThreeTierStack, observability: ObservabilityStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.BLOG_ANALYTICS)

        # ------------------------------------------------------------------
        # DynamoDB -- raw events, one item per (sessionId, eventTimestamp).
        # TTL'd (see module docstring) so raw per-visitor rows -- including
        # source IP -- don't accumulate forever; CloudWatch metrics below
        # are the permanent aggregate record.
        # ------------------------------------------------------------------
        self.table = dynamodb.TableV2(
            self, "BlogAnalyticsEvents",
            table_name="blog-analytics-events",
            partition_key=dynamodb.Attribute(name="sessionId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="eventTimestamp", type=dynamodb.AttributeType.NUMBER),
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.customer_managed_key(security.buckets_key),
            time_to_live_attribute="expiresAt",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
            global_secondary_indexes=[
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name="EventTypeByTime",
                    partition_key=dynamodb.Attribute(name="eventType", type=dynamodb.AttributeType.STRING),
                    sort_key=dynamodb.Attribute(name="eventTimestamp", type=dynamodb.AttributeType.NUMBER),
                ),
            ],
        )

        # ------------------------------------------------------------------
        # Ingest Lambda -- one function behind one route, POST /events.
        # ------------------------------------------------------------------
        ingest_role = iam.Role(
            self, "BlogAnalyticsIngestRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        self.table.grant_write_data(ingest_role)
        security.buckets_key.grant_encrypt_decrypt(ingest_role)
        ingest_role.add_to_policy(iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],  # metric namespaces are not ARN resources -- see governance_stack.py's identical suppression
        ))

        ingest_log_group = logs.LogGroup(
            self, "BlogAnalyticsIngestLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            encryption_key=security.logs_key,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.ingest_fn = lambda_.Function(
            self, "BlogAnalyticsIngest",
            function_name="blog-analytics-ingest",
            description="POST /events from the blog's AnalyticsBeacon -- writes DynamoDB, publishes CloudWatch metrics.",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(ASSETS_DIR)),
            timeout=Duration.seconds(10),
            memory_size=256,
            role=ingest_role,
            log_group=ingest_log_group,
            environment={
                "TABLE_NAME": self.table.table_name,
                "METRICS_NAMESPACE": METRICS_NAMESPACE,
                "TTL_DAYS": str(TTL_DAYS),
            },
        )

        # ------------------------------------------------------------------
        # API Gateway HTTP API -- single route, CORS open (write-only
        # telemetry beacon, not sensitive read data; also reached same-origin
        # through CloudFront in production so CORS rarely even applies).
        # ------------------------------------------------------------------
        self.http_api = apigwv2.HttpApi(
            self, "BlogAnalyticsApi",
            api_name="blog-analytics-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.OPTIONS],
                allow_headers=["Content-Type"],
            ),
        )
        # CloudFront forwards the viewer's full request path to the origin
        # unchanged -- a "/analytics/*" cache behavior does NOT strip that
        # prefix before forwarding, so the route registered here has to be
        # the full path the browser actually calls (/analytics/events),
        # not just the part after the behavior's own wildcard.
        self.http_api.add_routes(
            path="/analytics/events",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration("IngestIntegration", self.ingest_fn),
        )

        # ------------------------------------------------------------------
        # Wire /analytics/* into ThreeTierStack's EXISTING CloudFront
        # distribution -- same cross-stack construct-mutation pattern
        # auto_heal_stack.py already uses on observability_stack.py's
        # dashboard. Keeps the blog same-origin (no CORS in practice) and
        # avoids standing up a second distribution.
        # ------------------------------------------------------------------
        api_domain = cdk.Fn.select(2, cdk.Fn.split("/", self.http_api.api_endpoint))
        analytics_origin = origins.HttpOrigin(
            api_domain,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )
        threetier.distribution.add_behavior(
            "/analytics/*",
            analytics_origin,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
        )

        # ------------------------------------------------------------------
        # Dashboard widgets -- appended to the existing lattice-lab
        # dashboard, not a new one.
        # ------------------------------------------------------------------
        observability.dashboard.add_widgets(
            cw.TextWidget(markdown="# Blog Analytics", width=24, height=1),
            cw.GraphWidget(
                title="Reads and drop-off",
                left=[
                    cw.Metric(namespace=METRICS_NAMESPACE, metric_name="PageViews", statistic="Sum", label="Page views"),
                    cw.Metric(namespace=METRICS_NAMESPACE, metric_name="ReadCompletions", statistic="Sum", label="Read to end"),
                    cw.Metric(namespace=METRICS_NAMESPACE, metric_name="GithubClicks", statistic="Sum", label="GitHub clicks"),
                ],
                width=12, height=6,
            ),
            cw.GraphWidget(
                title="Engagement depth",
                left=[cw.Metric(namespace=METRICS_NAMESPACE, metric_name="TimeOnPageSeconds", statistic="Average", label="Avg time on page (s)")],
                right=[cw.Metric(namespace=METRICS_NAMESPACE, metric_name="MaxScrollPercent", statistic="Average", label="Avg max scroll %")],
                width=12, height=6,
            ),
        )

        CfnOutput(self, "BlogAnalyticsApiEndpoint", value=self.http_api.api_endpoint)
        CfnOutput(self, "BlogAnalyticsTableName", value=self.table.table_name)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10).
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="service-role/AWSLambdaBasicExecutionRole is the standard CloudWatch Logs write "
                       "policy every Lambda in this project uses (see governance_stack.py, "
                       "drift_remediation_stack.py) -- no narrower AWS-managed alternative exists.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="table.grant_write_data()/buckets_key.grant_encrypt_decrypt() are CDK-generated "
                       "grants scoped to this table/key's own ARN. cloudwatch:PutMetricData is inherently "
                       "unscoped (metric namespaces are not ARN resources) -- identical, already-accepted "
                       "pattern to governance_stack.py's GovernanceMetricsRole.",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="PYTHON_3_13 is the latest available managed Lambda runtime as of this build.",
            ),
            NagPackSuppression(
                id="AwsSolutions-APIG1",
                reason="Access logging on this single-route ingest API would duplicate what the Lambda's "
                       "own CloudWatch Logs already capture for a lab-scale blog analytics beacon.",
            ),
            NagPackSuppression(
                id="AwsSolutions-APIG4",
                reason="This endpoint is an intentionally-open, write-only analytics beacon reached "
                       "same-origin through CloudFront -- there is no per-visitor identity to authorize "
                       "against; the payload itself is the only input, validated in the handler.",
            ),
            NagPackSuppression(
                id="AwsSolutions-DDB3",
                reason="point_in_time_recovery_specification is explicitly enabled above.",
            ),
        ])
