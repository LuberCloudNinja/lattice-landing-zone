"""threetier_stack.py -- web tier (CloudFront+S3) -> app tier (Lambda) -> data tier (DynamoDB).

See SPEC.md Section 6 ("threetier_stack.py (three-tier web app)"). SPEC.md's
original text describes an ALB + Auto Scaling app tier; this stack has since
moved twice -- first to ECS Fargate, now to a fully serverless app tier with
no load balancer and no always-on compute at all. Documented here rather
than in SPEC.md itself, the same way the earlier Fargate move was.

config.WEBAPP_SOURCE has not been set (still "REPLACE_ME_WEBAPP_SOURCE" per
SPEC.md Section 0) -- the app tier (app/backend/app.py) is still the
stdlib-only placeholder backend. The web tier is no longer a placeholder,
though: app/frontend-next is a real Next.js + Tailwind app (static export,
`output: 'export'` -- see its next.config.js), built by the pipeline's Synth
step (pipeline_stack.py) before `cdk synth` runs, so Source.asset() below
finds real files. Swap app/backend/ for the real app tier (keep its
Dockerfile, or replace it) once WEBAPP_SOURCE is provided; the web tier
already ships the author's own portfolio site + blog.

App tier compute: a single Lambda function running app/backend/'s container
image, fronted by API Gateway HTTP API, reached from CloudFront at /api/*.
No ALB, no ECS cluster, no Auto Scaling group, no idle compute cost -- the
function only runs (and is only billed) while handling a request. The
container image is unchanged from the ECS-Fargate version of this stack;
what changed is the Dockerfile, which now layers in the AWS Lambda Web
Adapter (a Lambda extension, not an app code change) so app.py keeps
running as a plain http.server process instead of being rewritten into a
handler(event, context) function. Same image, same app.py, different
compute target.

One consequence of dropping the ALB from the live traffic path: VPC
Lattice's ALB-type target group (lattice_stack.py L4b) needs a real ALB to
point at, and Lattice's INSTANCE-type target groups (L4a/L4c) need a real
EC2 instance id, neither of which a Lambda function can be. Both are kept
alive as small, clearly-separated demo-only resources below
(LatticeInstanceTargetHost + a minimal internal ALB in front of it) --
exactly the same "this exists only to keep one Lattice target-group type
demonstrable, it is not the real workload" pattern this file already used
for LatticeInstanceTargetHost before this change, just now extended to
cover the ALB type too.
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_targets as elbv2_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3_deploy
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.network_stack import NetworkStack
from stacks.security_stack import SecurityStack

APP_DIR = Path(__file__).parent.parent / "app"
APP_TIER_PORT = 8080  # must match app/backend/app.py's PORT env default


class ThreeTierStack(Stack):
    """Classic three-tier separation: web can't touch the DB, only the app tier can."""

    def __init__(self, scope: Construct, construct_id: str, *, network: NetworkStack, security: SecurityStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.THREETIER)

        vpc = network.app_vpc

        # ------------------------------------------------------------------
        # Lattice INSTANCE/IP-target-group demo host. Not part of the real
        # app-tier traffic path (that's the Lambda below) -- exists only
        # because VPC Lattice's INSTANCE-type target groups (lattice_stack.py
        # L4a/L4c) register real EC2 instance IDs, which a Lambda function
        # fundamentally can't be. Runs the exact same app/backend/app.py as
        # the real app tier, as a plain systemd service.
        # ------------------------------------------------------------------
        lattice_target_sg = ec2.SecurityGroup(
            self, "LatticeInstanceTargetSg",
            vpc=vpc,
            description="Dedicated Lattice target-group demo host -- inbound from app-vpc only",
            allow_all_outbound=True,
        )
        lattice_target_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(APP_TIER_PORT), "app-vpc (Lattice association traffic)"
        )
        lattice_target_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(443), "app-vpc (Lattice TLS_PASSTHROUGH demo)"
        )

        lattice_target_role = iam.Role(
            self, "LatticeInstanceTargetRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )
        lattice_target_user_data = ec2.UserData.for_linux()
        lattice_target_user_data.add_commands(
            "set -e",
            "mkdir -p /opt/app",
            f"cat > /opt/app/app.py << 'PYEOF'\n{(APP_DIR / 'backend' / 'app.py').read_text()}PYEOF",
            "cat > /etc/systemd/system/app-backend.service << 'EOF'\n"
            "[Unit]\n"
            "Description=Lattice target-group demo backend\n"
            "After=network.target\n"
            "[Service]\n"
            f"Environment=PORT={APP_TIER_PORT}\n"
            "ExecStart=/usr/bin/python3 /opt/app/app.py\n"
            "Restart=always\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
            "EOF",
            "systemctl daemon-reload",
            "systemctl enable --now app-backend",
        )
        # Exposed publicly -- lattice_stack.py references this instance's id
        # and private IP directly (no custom-resource lookup needed, unlike
        # the old ASG-backed approach, since a plain ec2.Instance already
        # exposes both as ordinary CDK attributes).
        self.lattice_instance_target_host = ec2.Instance(
            self, "LatticeInstanceTargetHost",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            instance_type=ec2.InstanceType(config.DEFAULT_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=lattice_target_sg,
            role=lattice_target_role,
            user_data=lattice_target_user_data,
            ssm_session_permissions=True,
            block_devices=[ec2.BlockDevice(
                device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(8, encrypted=True, kms_key=security.ebs_key)
            )],
        )

        # ------------------------------------------------------------------
        # Minimal internal ALB, demo-only, same reasoning as the instance
        # above: VPC Lattice's ALB-type target group (lattice_stack.py L4b)
        # needs a real ALB to point at. Forwards to the same demo host,
        # exactly like ip_target_group in lattice_stack.py already reaches
        # that same instance by a different address -- one small backend,
        # every Lattice target-group type demonstrated against it.
        # ------------------------------------------------------------------
        alb_sg = ec2.SecurityGroup(
            self, "AppAlbSg",
            vpc=vpc,
            description="Lattice ALB-target-group demo -- inbound only from inside app-vpc (Lattice association traffic, not the public internet)",
            allow_all_outbound=True,
        )
        alb_sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(80), "app-vpc (Lattice association traffic)")
        lattice_target_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(APP_TIER_PORT), "from AppAlbSg")

        self.alb = elbv2.ApplicationLoadBalancer(
            self, "AppAlb",
            vpc=vpc,
            internet_facing=False,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            security_group=alb_sg,
        )
        self.app_target_group = elbv2.ApplicationTargetGroup(
            self, "AppTargetGroup",
            vpc=vpc,
            port=APP_TIER_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.INSTANCE,
            targets=[elbv2_targets.InstanceIdTarget(self.lattice_instance_target_host.instance_id, port=APP_TIER_PORT)],
            health_check=elbv2.HealthCheck(path="/api/health", healthy_threshold_count=2, unhealthy_threshold_count=3),
        )
        # Exposed publicly (not local) -- lattice_stack.py's L4b ALB-type
        # target group registers this exact listener's ARN as its target.
        self.app_listener = self.alb.add_listener(
            "AppListener",
            port=80,
            default_action=elbv2.ListenerAction.forward([self.app_target_group]),
        )

        # ------------------------------------------------------------------
        # Data tier: DynamoDB -- on-demand billing (true scale-to-zero, no
        # idle cost, no capacity planning), reachable only via the app
        # Lambda's own IAM role (not a network-level control the way RDS's
        # security group was -- DynamoDB is an AWS-managed API endpoint,
        # not something with a listening port/SG of its own). Access is
        # scoped by IAM grant, not network reachability, which is the
        # correct control for a DynamoDB table regardless.
        # ------------------------------------------------------------------
        self.database = dynamodb.TableV2(
            self, "Database",
            table_name="latticelab",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.aws_managed_key(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # App tier compute: one Lambda function, the app/backend/ container
        # image (see that Dockerfile's AWS Lambda Web Adapter layer). Not
        # VPC-attached -- its only AWS dependency is DynamoDB, reached over
        # the same public, TLS-encrypted, IAM-authenticated API endpoint
        # every Lambda in this project already calls Bedrock/S3/DynamoDB
        # through (blog_assistant_stack.py, blog_analytics_stack.py), so
        # there's no VPC ENI cold-start cost and no interface endpoints to
        # provision just for this function to start.
        # ------------------------------------------------------------------
        app_role = iam.Role(
            self, "AppFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        self.database.grant_read_write_data(app_role)

        app_log_group = logs.LogGroup(
            self, "AppLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            encryption_key=security.logs_key,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.app_function = lambda_.DockerImageFunction(
            self, "AppFunction",
            function_name="threetier-app",
            description="App tier -- app/backend/'s container image, run on Lambda via the AWS Lambda Web Adapter.",
            code=lambda_.DockerImageCode.from_image_asset(str(APP_DIR / "backend")),
            architecture=lambda_.Architecture.X86_64,
            memory_size=512,
            # API Gateway HTTP API's own integration timeout caps at 29s --
            # no point configuring the function itself any higher.
            timeout=Duration.seconds(29),
            role=app_role,
            log_group=app_log_group,
            environment={"PORT": str(APP_TIER_PORT), "AWS_LWA_PORT": str(APP_TIER_PORT)},
        )

        # ------------------------------------------------------------------
        # API Gateway HTTP API -- same shape as blog_assistant_stack.py /
        # blog_analytics_stack.py's Lambda-behind-CloudFront routes, just
        # owned directly by this stack since it already owns the
        # distribution. Route path is the FULL path CloudFront forwards
        # (see the /api/* behavior below) -- CloudFront does not strip the
        # matched behavior's own prefix before forwarding to the origin.
        # ------------------------------------------------------------------
        self.http_api = apigwv2.HttpApi(
            self, "AppApi",
            api_name="threetier-app-api",
        )
        self.http_api.add_routes(
            path="/api/{proxy+}",
            methods=[apigwv2.HttpMethod.ANY],
            integration=apigwv2_integrations.HttpLambdaIntegration("AppIntegration", self.app_function),
        )

        # ------------------------------------------------------------------
        # Web tier: private S3 (OAC) for the static frontend, CloudFront in
        # front with two behaviors -- default -> S3, /api/* -> the API
        # Gateway HTTP API above (a plain HttpOrigin -- no VPC origin, no
        # ALB in the live traffic path at all now).
        # ------------------------------------------------------------------
        web_bucket = s3.Bucket(
            self, "WebBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security.buckets_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=security.s3_access_logs_bucket,
            server_access_logs_prefix="web-bucket/",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        s3_deploy.BucketDeployment(
            self, "WebDeployment",
            sources=[s3_deploy.Source.asset(str(APP_DIR / "frontend-next" / "out"))],
            destination_bucket=web_bucket,
        )

        api_domain = cdk.Fn.select(2, cdk.Fn.split("/", self.http_api.api_endpoint))
        app_api_origin = origins.HttpOrigin(
            api_domain,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )

        # Next.js static export writes one directory per route (e.g.
        # blog/hybrid-cloud-airport-story/index.html) with trailingSlash
        # enabled. default_root_object only rewrites the bare "/" -- a
        # private S3 origin behind OAC (not S3 website-hosting mode, which
        # would require public access) does NOT auto-append index.html to
        # subdirectory requests the way an S3 website endpoint would. This
        # CloudFront Function does that rewrite at the edge instead --
        # free-tier eligible, no Lambda@Edge needed.
        url_rewrite_fn = cloudfront.Function(
            self, "NextStaticUrlRewrite",
            code=cloudfront.FunctionCode.from_inline(
                "function handler(event) {\n"
                "  var request = event.request;\n"
                "  var uri = request.uri;\n"
                "  if (uri.endsWith('/')) {\n"
                "    request.uri += 'index.html';\n"
                "  } else if (!uri.includes('.')) {\n"
                "    request.uri += '/index.html';\n"
                "  }\n"
                "  return request;\n"
                "}"
            ),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
        )

        self.distribution = cloudfront.Distribution(
            self, "WebDistribution",
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(web_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                function_associations=[cloudfront.FunctionAssociation(
                    function=url_rewrite_fn, event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                )],
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=app_api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                ),
            },
        )

        CfnOutput(self, "WebDistributionUrl", value=f"https://{self.distribution.distribution_domain_name}")
        CfnOutput(self, "AppApiEndpoint", value=self.http_api.api_endpoint)
        CfnOutput(self, "AppAlbDnsName", value=self.alb.load_balancer_dns_name)
        CfnOutput(self, "DatabaseTableName", value=self.database.table_name)
        CfnOutput(self, "LatticeInstanceTargetHostId", value=self.lattice_instance_target_host.instance_id)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10)
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="AmazonSSMManagedInstanceCore (LatticeInstanceTargetHost's EC2 role) and "
                       "service-role/AWSLambdaBasicExecutionRole (the app tier Lambda's role) are both "
                       "AWS-curated, not hand-widened.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="Wildcards come from CDK's own grant_read_write_data()/BucketDeployment-generated "
                       "policies -- none hand-widened.",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="Flagged Lambda is BucketDeployment's own framework-managed provider function, "
                       "not project-authored code.",
            ),
            NagPackSuppression(
                id="AwsSolutions-S1",
                reason="Server access logging on the static-frontend bucket would need a second bucket "
                       "purely for a lab placeholder page -- not worth the added infrastructure.",
            ),
            NagPackSuppression(
                id="AwsSolutions-CFR1",
                reason="Geo restriction isn't relevant to an interview-lab demo page.",
            ),
            NagPackSuppression(
                id="AwsSolutions-CFR2",
                reason="WAF is explicitly optional per docs/inspection-architecture-reference.md Section "
                       "6.16 -- default is open behind the private-origin pattern, not WAF-restricted.",
            ),
            NagPackSuppression(
                id="AwsSolutions-CFR3",
                reason="CloudFront access logging needs a dedicated log bucket -- not worth the added "
                       "infrastructure for a lab demo distribution.",
            ),
            NagPackSuppression(
                id="AwsSolutions-CFR4",
                reason="minimum_protocol_version is set to TLS_V1_2_2021 in code; this distribution uses "
                       "the default *.cloudfront.net certificate (no custom domain, per Section 6.16's "
                       "own decision), for which AWS enforces TLSv1.2+ regardless -- cdk-nag's check "
                       "appears not to recognize that default-certificate case.",
            ),
            NagPackSuppression(
                id="AwsSolutions-ELB2",
                reason="ALB access logging needs a dedicated log bucket -- not worth the added "
                       "infrastructure for this lab's small, demo-only internal ALB (Lattice ALB-target-"
                       "group type demonstration, not the live app-tier traffic path).",
            ),
            NagPackSuppression(
                id="AwsSolutions-EC28",
                reason="Detailed monitoring is extra CloudWatch cost with no security value for a lab "
                       "meant to be built and torn down repeatedly (LatticeInstanceTargetHost).",
            ),
            NagPackSuppression(
                id="AwsSolutions-EC29",
                reason="Termination protection would directly conflict with SPEC.md's `cdk destroy "
                       "--all` teardown requirement (LatticeInstanceTargetHost).",
            ),
            NagPackSuppression(
                id="AwsSolutions-APIG1",
                reason="Access logging on this single-integration proxy API would duplicate what the "
                       "Lambda's own CloudWatch Logs already capture for a lab-scale app tier.",
            ),
            NagPackSuppression(
                id="AwsSolutions-APIG4",
                reason="This endpoint is only ever reached same-origin through CloudFront's /api/* "
                       "behavior -- there is no per-visitor identity to authorize against for a "
                       "placeholder app-tier backend.",
            ),
        ])
        NagSuppressions.add_resource_suppressions(
            alb_sg,
            [NagPackSuppression(
                id="AwsSolutions-EC23",
                reason="cdk-nag can't statically resolve vpc.vpc_cidr_block (a CloudFormation token) to "
                       "confirm it isn't 0.0.0.0/0 -- it's app-vpc's own CIDR, not open access; this ALB "
                       "is internal (internet_facing=False) in any case.",
            )],
        )
        NagSuppressions.add_resource_suppressions(
            lattice_target_sg,
            [NagPackSuppression(
                id="AwsSolutions-EC23",
                reason="cdk-nag can't statically resolve vpc.vpc_cidr_block (a CloudFormation token) to "
                       "confirm it isn't 0.0.0.0/0 -- scoped to app-vpc's own CIDR only.",
            )],
        )
