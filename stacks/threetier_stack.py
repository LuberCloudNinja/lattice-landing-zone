"""threetier_stack.py -- web tier (CloudFront+S3) -> app tier (ALB+Fargate) -> data tier (RDS).

See SPEC.md Section 6 ("threetier_stack.py (three-tier web app)").

config.WEBAPP_SOURCE has not been set yet (still "REPLACE_ME_WEBAPP_SOURCE"
per SPEC.md Section 0), so this stack deploys the placeholder app in app/
(app/frontend/index.html, app/backend/app.py) instead -- clearly labeled as
such in both. Swap app/backend/ for the real app (keep its Dockerfile, or
replace it) and re-deploy this stack once WEBAPP_SOURCE is provided.

App tier runs on ECS Fargate (smallest task size, 0.25 vCPU/0.5GB) rather
than an EC2 ASG -- Fargate draws from its own separate vCPU quota pool
(distinct from the EC2 "Standard" instance-family quota the rest of this
project's EC2 fleet shares), and needs no host patching. One consequence:
VPC Lattice's INSTANCE-type target groups (lattice_stack.py L4a/L4c)
register real EC2 instance IDs, which Fargate tasks fundamentally can't be
-- see LatticeInstanceTargetHost below for how that's preserved without
putting the real app-tier workload back on EC2.
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3_deploy
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.network_stack import NetworkStack

APP_DIR = Path(__file__).parent.parent / "app"
APP_TIER_PORT = 8080  # must match app/backend/app.py's PORT env default


class ThreeTierStack(Stack):
    """Classic three-tier separation: web can't touch the DB, only the app tier can."""

    def __init__(self, scope: Construct, construct_id: str, *, network: NetworkStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.THREETIER)

        vpc = network.app_vpc

        # ------------------------------------------------------------------
        # App tier: internal ALB -> ASG. Built before the web tier below
        # since CloudFront's VPC origin needs a real ALB to point at.
        # Deliberately public (self.alb / self.app_target_group), not local
        # -- LatticeStack (L4b) reuses this exact ALB as a Lattice target
        # group per SPEC.md Section 6's own note.
        # ------------------------------------------------------------------
        alb_sg = ec2.SecurityGroup(
            self, "AppAlbSg",
            vpc=vpc,
            description="Internal app-tier ALB -- inbound only from inside app-vpc (CloudFront VPC origin traffic enters via the VPC, not the public internet)",
            allow_all_outbound=True,
        )
        alb_sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(APP_TIER_PORT), "app-vpc")

        app_sg = ec2.SecurityGroup(
            self, "AppInstanceSg",
            vpc=vpc,
            description="App-tier ASG instances -- inbound from the ALB only",
            allow_all_outbound=True,
        )
        app_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(APP_TIER_PORT), "from AppAlbSg")

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
            # IP, not INSTANCE -- Fargate's awsvpc networking mode requires
            # IP-type ALB target groups (there's no host instance to
            # register by id).
            target_type=elbv2.TargetType.IP,
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
        # VPC endpoints Fargate needs to even START in app-vpc. app-vpc's
        # Private subnets DO have a real internet path (NAT Gateway, routed
        # through inspection first) unlike provider-vpc's -- so this looked
        # unnecessary at first. It wasn't: confirmed the hard way (same
        # failure as privatelink_stack.py's provider task) that the
        # inspection-routed NAT path isn't reliable/fast enough for the ECR
        # auth handshake -- "unable to pull registry auth ... dial tcp
        # ...:443: i/o timeout" on every attempt. Same fix, same reasoning
        # as privatelink_stack.py's ProviderEcrEndpoint etc.
        # ------------------------------------------------------------------
        app_vpc_endpoint_sg = ec2.SecurityGroup(
            self, "AppVpcEndpointSg",
            vpc=vpc,
            description="Interface endpoints (ECR/Logs) for the Fargate app tier -- inbound from app-vpc only",
            allow_all_outbound=True,
        )
        app_vpc_endpoint_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(443), "app-vpc"
        )
        for service_id, service in {
            "Ecr": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        }.items():
            ec2.InterfaceVpcEndpoint(
                self, f"App{service_id}Endpoint",
                vpc=vpc,
                service=service,
                subnets=ec2.SubnetSelection(subnet_group_name="Private"),
                security_groups=[app_vpc_endpoint_sg],
            )
        ec2.GatewayVpcEndpoint(
            self, "AppS3Endpoint",
            vpc=vpc,
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[ec2.SubnetSelection(subnet_group_name="Private")],
        )

        # ------------------------------------------------------------------
        # App tier compute: ECS Fargate, smallest task size. from_asset()
        # builds app/backend/'s Dockerfile and pushes it to a CDK-managed
        # ECR asset repo at deploy time -- CDK Pipelines automatically runs
        # that publish step in a privileged (Docker-capable) CodeBuild
        # project, no manual configuration needed.
        # ------------------------------------------------------------------
        app_cluster = ecs.Cluster(
            self, "AppCluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.DISABLED
        )

        app_task_definition = ecs.FargateTaskDefinition(
            self, "AppTaskDefinition",
            cpu=256,
            memory_limit_mib=512,
        )
        app_task_definition.add_container(
            "AppContainer",
            image=ecs.ContainerImage.from_asset(str(APP_DIR / "backend")),
            port_mappings=[ecs.PortMapping(container_port=APP_TIER_PORT)],
            environment={"PORT": str(APP_TIER_PORT)},
            logging=ecs.LogDrivers.aws_logs(stream_prefix="app-tier", log_retention=logs.RetentionDays.ONE_WEEK),
        )

        self.app_service = ecs.FargateService(
            self, "AppService",
            cluster=app_cluster,
            task_definition=app_task_definition,
            desired_count=2 if config.MULTI_AZ else 1,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            security_groups=[app_sg],
            # ECS Exec -- interactive debug access, the Fargate equivalent
            # of ssm_session_permissions=True on the EC2 instances elsewhere
            # in this project.
            enable_execute_command=True,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            # Fine for a lab at desired_count=1 to go briefly to 0 during a
            # deploy; explicit rather than relying on the (identical) default
            # so the CLI doesn't flag it as unconfigured.
            min_healthy_percent=0,
            max_healthy_percent=200,
        )
        self.app_service.attach_to_application_target_group(self.app_target_group)

        # ------------------------------------------------------------------
        # Dedicated small EC2 instance -- exists ONLY because VPC Lattice's
        # INSTANCE-type target groups (lattice_stack.py L4a/L4c) register
        # real EC2 instance IDs, and Fargate tasks cannot be INSTANCE-type
        # targets (no such registration path exists in the Lattice API).
        # NOT part of the ALB/CloudFront traffic path -- the app tier above
        # is the real workload; this just keeps that one Lattice feature
        # demonstrable now that the real workload no longer runs on EC2.
        # ------------------------------------------------------------------
        lattice_target_sg = ec2.SecurityGroup(
            self, "LatticeInstanceTargetSg",
            vpc=vpc,
            description="Dedicated Lattice INSTANCE-target-group demo host -- inbound from app-vpc only",
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
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )
        lattice_target_user_data = ec2.UserData.for_linux()
        lattice_target_user_data.add_commands(
            "set -e",
            "mkdir -p /opt/app",
            f"cat > /opt/app/app.py << 'PYEOF'\n{(APP_DIR / 'backend' / 'app.py').read_text()}PYEOF",
            "cat > /etc/systemd/system/app-backend.service << 'EOF'\n"
            "[Unit]\n"
            "Description=Lattice INSTANCE-target-group demo backend\n"
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
                device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(8, encrypted=True)
            )],
        )

        # ------------------------------------------------------------------
        # Data tier: RDS, reachable only from the app tier's own SG (not a
        # separate subnet tier -- SPEC.md Section 6 scopes isolation to the
        # security group, and app-vpc's existing Private subnet already
        # routes everything through inspection first, same as the app tier).
        # ------------------------------------------------------------------
        db_sg = ec2.SecurityGroup(
            self, "DbSg",
            vpc=vpc,
            description="RDS -- inbound from the app tier only",
            allow_all_outbound=False,
        )
        # Non-default port (cdk-nag AwsSolutions-RDS11) -- trivial, no
        # functional cost since only the app tier's own SG can reach it
        # anyway; avoids relying on "everyone knows 5432" as a control.
        db_port = 5433
        db_sg.add_ingress_rule(app_sg, ec2.Port.tcp(db_port), "from AppInstanceSg")

        self.database = rds.DatabaseInstance(
            self, "Database",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16_3),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            security_groups=[db_sg],
            port=db_port,
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO),
            multi_az=config.MULTI_AZ,
            allocated_storage=20,
            storage_encrypted=True,
            database_name="latticelab",
            credentials=rds.Credentials.from_generated_secret("latticelab_admin"),
            removal_policy=cdk.RemovalPolicy.DESTROY,
            deletion_protection=False,
        )

        # ------------------------------------------------------------------
        # Web tier: private S3 (OAC) for the static frontend, CloudFront in
        # front with two behaviors -- default -> S3, /api/* -> the internal
        # ALB above via a CloudFront VPC origin (no public ALB needed; this
        # is the trade-off SPEC.md Section 6 asked to document -- see this
        # file's module docstring / README.md for the public-ALB fallback
        # this project deliberately did NOT take).
        # ------------------------------------------------------------------
        web_bucket = s3.Bucket(
            self, "WebBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        s3_deploy.BucketDeployment(
            self, "WebDeployment",
            sources=[s3_deploy.Source.asset(str(APP_DIR / "frontend"))],
            destination_bucket=web_bucket,
        )

        alb_vpc_origin = origins.VpcOrigin.with_application_load_balancer(self.alb, http_port=80)

        self.distribution = cloudfront.Distribution(
            self, "WebDistribution",
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(web_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=alb_vpc_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                ),
            },
        )

        CfnOutput(self, "WebDistributionUrl", value=f"https://{self.distribution.distribution_domain_name}")
        CfnOutput(self, "AppAlbDnsName", value=self.alb.load_balancer_dns_name)
        CfnOutput(self, "DatabaseSecretArn", value=self.database.secret.secret_arn)
        CfnOutput(self, "LatticeInstanceTargetHostId", value=self.lattice_instance_target_host.instance_id)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10)
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="AmazonSSMManagedInstanceCore (LatticeInstanceTargetHost's EC2 role) and "
                       "AmazonECSTaskExecutionRolePolicy (the Fargate app tier's execution role, CDK's "
                       "own standard image-pull/log-write policy) are both AWS-curated, not "
                       "hand-widened.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="Wildcards come from CDK's own grant_read()/BucketDeployment-generated policies, "
                       "the RDS-generated-secret attachment policy, the Fargate task definition's "
                       "framework-generated execution role policy, and ECS Exec's SSM channel "
                       "permissions -- none hand-widened.",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="Flagged Lambda is BucketDeployment's own framework-managed provider function, "
                       "not project-authored code.",
            ),
            NagPackSuppression(
                id="AwsSolutions-ECS4",
                reason="Container Insights is extra CloudWatch cost with no security value for a lab "
                       "meant to be built and torn down repeatedly.",
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
                id="AwsSolutions-RDS3",
                reason="Single-AZ by design and by cost, per config.MULTI_AZ (default false) -- "
                       "SPEC.md Section 1's explicit cost-conscious intent for non-HA tiers.",
            ),
            NagPackSuppression(
                id="AwsSolutions-RDS10",
                reason="Deletion protection would directly conflict with SPEC.md's `cdk destroy --all` "
                       "teardown requirement.",
            ),
            NagPackSuppression(
                id="AwsSolutions-SMG4",
                reason="Automatic secret rotation needs a rotation Lambda + scheduling -- extra "
                       "infrastructure not core to this lab; the secret itself is still "
                       "auto-generated and stored securely in Secrets Manager.",
            ),
            NagPackSuppression(
                id="AwsSolutions-ELB2",
                reason="ALB access logging needs a dedicated log bucket -- not worth the added "
                       "infrastructure for a lab's internal ALB.",
            ),
            NagPackSuppression(
                id="AwsSolutions-ECS2",
                reason="The only 'environment variable' is PORT=8080, a fixed, non-sensitive listen "
                       "port -- not the kind of value AwsSolutions-ECS2 exists to catch (secrets/config "
                       "that belong in Secrets Manager or SSM Parameter Store instead).",
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
