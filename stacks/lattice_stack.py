"""lattice_stack.py -- ALL VPC Lattice features (SPEC.md Section 5) -- the centerpiece.

Grouped with `# ---- L4a ----` etc. comments matching SPEC.md Section 5's
subsections. A few implementation notes that apply across the whole file:

  - INSTANCE-type target groups register EC2 *instance IDs* directly --
    threetier_stack.py's real app-tier workload runs on Lambda now (no ALB,
    no EC2, no idle compute at all), which fundamentally cannot fill this
    role, so this stack targets threetier.lattice_instance_target_host
    instead -- a small dedicated EC2 instance that exists specifically to
    keep the INSTANCE-type (and, reusing the same host, IP-type and
    ALB-type) target-group demos real and working. Its id/private IP are
    plain CDK attributes on an ec2.Instance construct, so no
    AwsCustomResource lookup is needed here. threetier_stack.py's own ALB
    (self.alb below) is the same kind of demo-only resource -- kept alive
    purely so this file's L4b ALB-type target group has a real ALB to
    point at, not part of the live app-tier traffic path.
  - L4c's HTTPS listener needs a certificate, and this lab has no real
    domain to validate a public ACM certificate against, and ACM Private CA
    costs ~$400/month just to exist -- far outside SPEC.md Section 1's
    cost-conscious intent for a side feature. Instead: generate a self-signed
    cert locally with openssl at synth time and import it into ACM via
    AwsCustomResource (ACM's ImportCertificate API, not a CloudFormation
    resource type) -- free, no domain ownership needed, clearly a lab
    substitute (documented inline and in the custom domain name itself).
"""

import subprocess
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ram as ram
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_vpclattice as vl
from aws_cdk import custom_resources as cr
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.network_stack import NetworkStack
from stacks.security_stack import SecurityStack
from stacks.threetier_stack import ThreeTierStack

APP_TIER_PORT = 8080  # must match threetier_stack.py's APP_TIER_PORT
LAB_CUSTOM_DOMAIN = "lattice-lab.internal.example"  # label only -- see L4c
LAB_TLS_CUSTOM_DOMAIN = "tls.lattice-lab.internal.example"  # tls_service's own domain -- see L4c


class LatticeStack(Stack):
    """Service network, all target-group types, HTTPS/TLS passthrough, auth policies,
    hybrid resource gateway, and observability -- SPEC.md Section 5 (L4a-L4f)."""

    def __init__(
        self, scope: Construct, construct_id: str, *,
        network: NetworkStack, threetier: ThreeTierStack, security: SecurityStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.LATTICE)

        app_vpc = network.app_vpc
        provider_vpc = network.provider_vpc

        # ------------------------------------------------------------------
        # threetier_stack.py's dedicated Lattice INSTANCE-target-group demo
        # host -- see module docstring for why this exists (Fargate tasks
        # can't be INSTANCE-type Lattice targets). Plain CDK attributes, no
        # custom-resource lookup needed.
        # ------------------------------------------------------------------
        app_instance_id = threetier.lattice_instance_target_host.instance_id
        app_instance_private_ip = threetier.lattice_instance_target_host.instance_private_ip

        # ------------------------------------------------------------------
        # ---- L4a: service network + first service ----
        # ------------------------------------------------------------------
        # Exposed publicly -- observability_stack.py's dashboard references
        # both for Lattice request-metric widgets.
        self.service_network = service_network = vl.CfnServiceNetwork(
            self, "ServiceNetwork", name="lattice-lab", auth_type="AWS_IAM"
        )

        self.service = service = vl.CfnService(self, "Service", name="lattice-lab-app", auth_type="AWS_IAM")

        instance_target_group = vl.CfnTargetGroup(
            self, "InstanceTargetGroup",
            type="INSTANCE",
            name="lattice-lab-instance-tg",
            config=vl.CfnTargetGroup.TargetGroupConfigProperty(
                port=APP_TIER_PORT,
                protocol="HTTP",
                vpc_identifier=app_vpc.vpc_id,
                health_check=vl.CfnTargetGroup.HealthCheckConfigProperty(
                    enabled=True, protocol="HTTP", path="/api/health",
                    healthy_threshold_count=2, unhealthy_threshold_count=3,
                ),
            ),
            targets=[vl.CfnTargetGroup.TargetProperty(id=app_instance_id, port=APP_TIER_PORT)],
        )

        http_listener = vl.CfnListener(
            self, "HttpListener",
            service_identifier=service.attr_id,
            name="http-listener",
            protocol="HTTP",
            port=80,
            default_action=vl.CfnListener.DefaultActionProperty(
                forward=vl.CfnListener.ForwardProperty(
                    target_groups=[
                        vl.CfnListener.WeightedTargetGroupProperty(
                            target_group_identifier=instance_target_group.attr_id, weight=100
                        )
                    ]
                )
            ),
        )
        # (The listener's default_action above IS the "default rule" --
        # VPC Lattice has no separate default CfnRule resource; additional
        # CfnRule resources below (L4b) add priority-ordered, condition-
        # matched routing on top of it.)

        # SG for the VPC association, built here (before the association
        # that needs it) even though it's conceptually an L4d feature --
        # see L4d below for what it actually references.
        lattice_vpc_association_sg = ec2.SecurityGroup(
            self, "LatticeVpcAssociationSg",
            vpc=app_vpc,
            description="app-vpc Lattice association -- ingress restricted to the VPC Lattice managed prefix list (L4d defense-in-depth)",
            allow_all_outbound=True,
        )

        vl.CfnServiceNetworkServiceAssociation(
            self, "ServiceAssociation",
            service_identifier=service.attr_id,
            service_network_identifier=service_network.attr_id,
        )
        vl.CfnServiceNetworkVpcAssociation(
            self, "AppVpcAssociation",
            service_network_identifier=service_network.attr_id,
            vpc_identifier=app_vpc.vpc_id,
            security_group_ids=[lattice_vpc_association_sg.security_group_id],
        )

        # ------------------------------------------------------------------
        # ---- L4b: all target-group types + rich rules ----
        # ------------------------------------------------------------------
        ip_target_group = vl.CfnTargetGroup(
            self, "IpTargetGroup",
            type="IP",
            name="lattice-lab-ip-tg",
            config=vl.CfnTargetGroup.TargetGroupConfigProperty(
                port=APP_TIER_PORT, protocol="HTTP", ip_address_type="IPV4", vpc_identifier=app_vpc.vpc_id,
            ),
            # Same app-tier instance, addressed by IP instead of instance id --
            # demonstrates the IP target type without standing up a new backend.
            targets=[vl.CfnTargetGroup.TargetProperty(id=app_instance_private_ip, port=APP_TIER_PORT)],
        )

        canary_lambda = lambda_.Function(
            self, "CanaryLambda",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            timeout=Duration.seconds(10),
            code=lambda_.Code.from_inline(
                "import json\n"
                "def handler(event, context):\n"
                "    return {\n"
                "        'statusCode': 200,\n"
                "        'statusDescription': '200 OK',\n"
                "        'isBase64Encoded': False,\n"
                "        'headers': {'Content-Type': 'application/json'},\n"
                "        'body': json.dumps({'message': 'hello from the Lattice LAMBDA target group'}),\n"
                "    }\n"
            ),
        )
        canary_lambda.add_permission(
            "InvokeFromLattice",
            principal=iam.ServicePrincipal("vpc-lattice.amazonaws.com"),
            source_account=self.account,
        )
        lambda_target_group = vl.CfnTargetGroup(
            self, "LambdaTargetGroup",
            type="LAMBDA",
            name="lattice-lab-lambda-tg",
            config=vl.CfnTargetGroup.TargetGroupConfigProperty(lambda_event_structure_version="V1"),
            targets=[vl.CfnTargetGroup.TargetProperty(id=canary_lambda.function_arn)],
        )

        alb_target_group = vl.CfnTargetGroup(
            self, "AlbTargetGroup",
            type="ALB",
            name="lattice-lab-alb-tg",
            config=vl.CfnTargetGroup.TargetGroupConfigProperty(
                port=80, protocol="HTTP", vpc_identifier=app_vpc.vpc_id,
            ),
            targets=[vl.CfnTargetGroup.TargetProperty(id=threetier.alb.load_balancer_arn)],
        )

        # Path-based: /v1/* -> tgA (ip_target_group)
        vl.CfnRule(
            self, "PathBasedRule",
            service_identifier=service.attr_id,
            listener_identifier=http_listener.attr_id,
            name="path-v1",
            priority=10,
            match=vl.CfnRule.MatchProperty(
                http_match=vl.CfnRule.HttpMatchProperty(
                    path_match=vl.CfnRule.PathMatchProperty(
                        case_sensitive=False,
                        match=vl.CfnRule.PathMatchTypeProperty(prefix="/v1/"),
                    )
                )
            ),
            action=vl.CfnRule.ActionProperty(
                forward=vl.CfnRule.ForwardProperty(
                    target_groups=[vl.CfnRule.WeightedTargetGroupProperty(
                        target_group_identifier=ip_target_group.attr_id, weight=100
                    )]
                )
            ),
        )

        # Header-based: x-canary: true -> tgB (lambda_target_group)
        vl.CfnRule(
            self, "HeaderBasedRule",
            service_identifier=service.attr_id,
            listener_identifier=http_listener.attr_id,
            name="header-canary",
            priority=20,
            match=vl.CfnRule.MatchProperty(
                http_match=vl.CfnRule.HttpMatchProperty(
                    header_matches=[vl.CfnRule.HeaderMatchProperty(
                        name="x-canary",
                        case_sensitive=False,
                        match=vl.CfnRule.HeaderMatchTypeProperty(exact="true"),
                    )]
                )
            ),
            action=vl.CfnRule.ActionProperty(
                forward=vl.CfnRule.ForwardProperty(
                    target_groups=[vl.CfnRule.WeightedTargetGroupProperty(
                        target_group_identifier=lambda_target_group.attr_id, weight=100
                    )]
                )
            ),
        )

        # Weighted 90/10 canary on /canary/* between instance_target_group
        # (v1, 90%) and alb_target_group (v2, 10%).
        vl.CfnRule(
            self, "WeightedCanaryRule",
            service_identifier=service.attr_id,
            listener_identifier=http_listener.attr_id,
            name="weighted-canary",
            priority=30,
            match=vl.CfnRule.MatchProperty(
                http_match=vl.CfnRule.HttpMatchProperty(
                    path_match=vl.CfnRule.PathMatchProperty(
                        case_sensitive=False,
                        match=vl.CfnRule.PathMatchTypeProperty(prefix="/canary/"),
                    )
                )
            ),
            action=vl.CfnRule.ActionProperty(
                forward=vl.CfnRule.ForwardProperty(
                    target_groups=[
                        vl.CfnRule.WeightedTargetGroupProperty(
                            target_group_identifier=instance_target_group.attr_id, weight=90
                        ),
                        vl.CfnRule.WeightedTargetGroupProperty(
                            target_group_identifier=alb_target_group.attr_id, weight=10
                        ),
                    ]
                )
            ),
        )

        # AZ affinity: as of this writing, VPC Lattice's target-group-level
        # AZ affinity setting has no CloudFormation property on
        # CfnTargetGroup.TargetGroupConfigProperty (checked directly against
        # the installed aws-cdk-lib's L1 -- no such field exists). Not set
        # here; revisit if/when CloudFormation adds it ("where supported"
        # per SPEC.md Section 6 -- currently, it isn't at the IaC layer).

        # ------------------------------------------------------------------
        # ---- L4c: HTTPS + custom domain + ACM + TLS passthrough ----
        # ------------------------------------------------------------------
        cert_arn = self._import_self_signed_certificate()

        # certificate_arn / custom_domain_name are properties of the SERVICE
        # (not the listener) in the Lattice CFN model -- set via direct L1
        # property assignment rather than restructuring L4a's construction
        # order.
        service.certificate_arn = cert_arn
        service.custom_domain_name = LAB_CUSTOM_DOMAIN

        vl.CfnListener(
            self, "HttpsListener",
            service_identifier=service.attr_id,
            name="https-listener",
            protocol="HTTPS",
            port=443,
            default_action=vl.CfnListener.DefaultActionProperty(
                forward=vl.CfnListener.ForwardProperty(
                    target_groups=[vl.CfnListener.WeightedTargetGroupProperty(
                        target_group_identifier=instance_target_group.attr_id, weight=100
                    )]
                )
            ),
        )

        # Second service, TLS_PASSTHROUGH -- Lattice forwards the raw
        # TLS bytes without terminating them; the BACKEND target must
        # terminate TLS itself. This project's app-tier instances only
        # listen on plain HTTP/8080 today, so this is structurally complete
        # (the Lattice/CFN resources) but not yet end-to-end testable --
        # add a TLS listener on the target (e.g. nginx + this same
        # self-signed cert) before treating this path as verified.
        #
        # VPC Lattice requires a TLS_PASSTHROUGH listener's service to have
        # a custom domain name (confirmed live: "TLS_PASSTHROUGH listener
        # can only be created for a service with a custom domain name") --
        # it needs its own domain distinct from the main `service`'s, since
        # reusing the same custom_domain_name across two services would
        # collide. Reuses the same self-signed cert (its CN doesn't need to
        # match here -- Lattice never terminates TLS in passthrough mode, so
        # the cert isn't used for validation, only to satisfy the schema).
        tls_service = vl.CfnService(self, "TlsPassthroughService", name="lattice-lab-tls", auth_type="AWS_IAM")
        tls_service.certificate_arn = cert_arn
        tls_service.custom_domain_name = LAB_TLS_CUSTOM_DOMAIN
        tls_target_group = vl.CfnTargetGroup(
            self, "TlsPassthroughTargetGroup",
            type="INSTANCE",
            name="lattice-lab-tls-tg",
            config=vl.CfnTargetGroup.TargetGroupConfigProperty(
                port=443, protocol="TCP", vpc_identifier=app_vpc.vpc_id,
                health_check=vl.CfnTargetGroup.HealthCheckConfigProperty(enabled=False),
            ),
            targets=[vl.CfnTargetGroup.TargetProperty(id=app_instance_id, port=443)],
        )
        vl.CfnListener(
            self, "TlsPassthroughListener",
            service_identifier=tls_service.attr_id,
            name="tls-passthrough-listener",
            protocol="TLS_PASSTHROUGH",
            port=443,
            default_action=vl.CfnListener.DefaultActionProperty(
                forward=vl.CfnListener.ForwardProperty(
                    target_groups=[vl.CfnListener.WeightedTargetGroupProperty(
                        target_group_identifier=tls_target_group.attr_id, weight=100
                    )]
                )
            ),
        )
        vl.CfnServiceNetworkServiceAssociation(
            self, "TlsServiceAssociation",
            service_identifier=tls_service.attr_id,
            service_network_identifier=service_network.attr_id,
        )

        # ------------------------------------------------------------------
        # ---- L4d: auth policies (zero-trust) + SG-on-association + managed prefix list ----
        # ------------------------------------------------------------------
        lattice_prefix_list_lookup = cr.AwsCustomResource(
            self, "LatticeManagedPrefixListLookup",
            on_create=cr.AwsSdkCall(
                service="EC2",
                action="describeManagedPrefixLists",
                parameters={
                    "Filters": [{"Name": "prefix-list-name", "Values": [f"com.amazonaws.{self.region}.vpc-lattice"]}]
                },
                physical_resource_id=cr.PhysicalResourceId.of("LatticeManagedPrefixListLookup"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE),
        )
        lattice_prefix_list_id = lattice_prefix_list_lookup.get_response_field("PrefixLists.0.PrefixListId")
        lattice_vpc_association_sg.add_ingress_rule(
            ec2.Peer.prefix_list(lattice_prefix_list_id), ec2.Port.all_traffic(),
            "VPC Lattice managed prefix list (defense-in-depth alongside the auth policies below)",
        )

        source_vpc_condition = {"StringEquals": {"aws:SourceVpc": app_vpc.vpc_id}}
        vl.CfnAuthPolicy(
            self, "ServiceNetworkAuthPolicy",
            resource_identifier=service_network.attr_arn,
            policy={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "vpc-lattice-svcs:Invoke",
                    "Resource": "*",
                    "Condition": source_vpc_condition,
                }],
            },
        )
        vl.CfnAuthPolicy(
            self, "ServiceAuthPolicy",
            resource_identifier=service.attr_arn,
            policy={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "vpc-lattice-svcs:Invoke",
                    "Resource": "*",
                    "Condition": source_vpc_condition,
                }],
            },
        )

        # ------------------------------------------------------------------
        # ---- L4e: resource gateway + resource configuration + service-network endpoint (hybrid) ----
        # ------------------------------------------------------------------
        resource_gateway_sg = ec2.SecurityGroup(
            self, "ResourceGatewaySg",
            vpc=app_vpc,
            description="Lattice resource gateway -- outbound to on-prem broker via TGW+VPN",
            allow_all_outbound=True,
        )
        self.resource_gateway = resource_gateway = vl.CfnResourceGateway(
            self, "OnpremResourceGateway",
            name="onprem-broker-gateway",
            vpc_identifier=app_vpc.vpc_id,
            subnet_ids=app_vpc.select_subnets(subnet_group_name="Private").subnet_ids,
            security_group_ids=[resource_gateway_sg.security_group_id],
        )
        self.onprem_broker_resource_config = resource_config = vl.CfnResourceConfiguration(
            self, "OnpremBrokerResourceConfig",
            name="onprem-broker",
            resource_configuration_type="SINGLE",
            protocol_type="TCP",
            port_ranges=["9092"],
            resource_gateway_id=resource_gateway.attr_id,
            resource_configuration_definition=vl.CfnResourceConfiguration.ResourceConfigurationDefinitionProperty(
                ip_resource=network.broker.instance_private_ip
            ),
        )
        vl.CfnServiceNetworkResourceAssociation(
            self, "OnpremBrokerResourceAssociation",
            resource_configuration_id=resource_config.attr_id,
            service_network_id=service_network.attr_id,
        )

        # Service-network VPC endpoint, in provider-vpc (deliberately NOT
        # app-vpc, which is already fully associated above) -- demonstrates
        # the alternative access pattern: a VPC that wants to reach the
        # service network's resources/services without joining it via a
        # full CfnServiceNetworkVpcAssociation.
        sn_endpoint_sg = ec2.SecurityGroup(
            self, "ServiceNetworkEndpointSg",
            vpc=provider_vpc,
            description="Service-network endpoint (PrivateLink-powered) -- inbound from provider-vpc",
            allow_all_outbound=True,
        )
        sn_endpoint_sg.add_ingress_rule(ec2.Peer.ipv4(provider_vpc.vpc_cidr_block), ec2.Port.all_traffic(), "provider-vpc")
        ec2.CfnVPCEndpoint(
            self, "ServiceNetworkEndpoint",
            vpc_id=provider_vpc.vpc_id,
            vpc_endpoint_type="ServiceNetwork",
            service_network_arn=service_network.attr_arn,
            subnet_ids=provider_vpc.select_subnets(subnet_group_name="Private").subnet_ids,
            security_group_ids=[sn_endpoint_sg.security_group_id],
        )

        # ------------------------------------------------------------------
        # ---- L4f: observability + dual-stack + optional RAM share ----
        # ------------------------------------------------------------------
        access_logs_group = logs.LogGroup(
            self, "ServiceNetworkAccessLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            encryption_key=security.logs_key,
            removal_policy=RemovalPolicy.DESTROY,
        )
        access_logs_bucket = s3.Bucket(
            self, "ServiceAccessLogsBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security.buckets_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=security.s3_access_logs_bucket,
            server_access_logs_prefix="lattice-access-logs-bucket/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        access_logs_bucket.add_to_resource_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("vpc-lattice.amazonaws.com")],
            actions=["s3:PutObject"],
            resources=[access_logs_bucket.arn_for_objects("*")],
            conditions={"StringEquals": {"aws:SourceAccount": self.account}},
        ))
        # SSE-KMS destination: the bucket policy above authorizes the
        # PutObject call itself, but the underlying KMS encrypt also needs
        # its own grant to the same service principal -- both are required
        # independently for SSE-KMS log delivery (the same pattern
        # governance_stack.py's CloudTrail trail needs for its own bucket).
        security.buckets_key.grant_encrypt_decrypt(iam.ServicePrincipal("vpc-lattice.amazonaws.com"))
        vl.CfnAccessLogSubscription(
            self, "ServiceNetworkAccessLogsSubscription",
            resource_identifier=service_network.attr_arn,
            destination_arn=access_logs_group.log_group_arn,
        )
        vl.CfnAccessLogSubscription(
            self, "ServiceAccessLogsSubscription",
            resource_identifier=service.attr_arn,
            destination_arn=access_logs_bucket.bucket_arn,
        )

        # Second Lambda-backed service, on its own service + listener +
        # association -- shows a service network fanning out to more than
        # one independently-routable service. NOTE: this was originally
        # meant to double as an IPv6/dual-stack demo via
        # ip_address_type=IPV6 on the target group config, but VPC Lattice's
        # API rejects that outright for LAMBDA-type target groups (confirmed
        # live: "Config contains invalid properties for Lambda Target
        # Group") -- ip_address_type is only accepted on IP-type target
        # groups, which would need real IPv6-enabled VPC subnets to
        # exercise, deliberately out of scope for this lab.
        dualstack_service = vl.CfnService(self, "DualStackService", name="lattice-lab-dualstack", auth_type="AWS_IAM")
        dualstack_target_group = vl.CfnTargetGroup(
            self, "DualStackLambdaTargetGroup",
            type="LAMBDA",
            name="lattice-lab-dualstack-tg",
            config=vl.CfnTargetGroup.TargetGroupConfigProperty(lambda_event_structure_version="V1"),
            targets=[vl.CfnTargetGroup.TargetProperty(id=canary_lambda.function_arn)],
        )
        canary_lambda.add_permission(
            "InvokeFromDualStackTargetGroup",
            principal=iam.ServicePrincipal("vpc-lattice.amazonaws.com"),
            source_account=self.account,
        )
        vl.CfnListener(
            self, "DualStackListener",
            service_identifier=dualstack_service.attr_id,
            name="dualstack-listener",
            protocol="HTTP",
            port=80,
            default_action=vl.CfnListener.DefaultActionProperty(
                forward=vl.CfnListener.ForwardProperty(
                    target_groups=[vl.CfnListener.WeightedTargetGroupProperty(
                        target_group_identifier=dualstack_target_group.attr_id, weight=100
                    )]
                )
            ),
        )
        vl.CfnServiceNetworkServiceAssociation(
            self, "DualStackServiceAssociation",
            service_identifier=dualstack_service.attr_id,
            service_network_identifier=service_network.attr_id,
        )

        # Optional AWS RAM share -- guarded behind config.ENABLE_RAM_SHARE
        # (true only once config.SECOND_ACCOUNT_ID is actually provided).
        if config.ENABLE_RAM_SHARE:
            ram.CfnResourceShare(
                self, "ServiceNetworkShare",
                name="lattice-lab-service-network-share",
                resource_arns=[service_network.attr_arn],
                principals=[config.SECOND_ACCOUNT_ID],
                allow_external_principals=False,
            )
        else:
            CfnOutput(
                self, "RamShareNotCreated",
                value=(
                    "ENABLE_RAM_SHARE is false (no SECOND_ACCOUNT_ID provided). "
                    "The cross-account grant that WOULD be created: an AWS RAM "
                    f"resource share named lattice-lab-service-network-share, sharing "
                    "ServiceNetwork's ARN, principal = <<SECOND_ACCOUNT_ID>>, "
                    "allow_external_principals=False. Set SECOND_ACCOUNT_ID to enable it."
                ),
            )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "ServiceNetworkArn", value=service_network.attr_arn)
        CfnOutput(self, "ServiceDnsName", value=service.attr_dns_entry_domain_name)
        CfnOutput(self, "ResourceConfigurationId", value=resource_config.attr_id)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10)
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="No managed policies are used directly by this stack's own resources beyond "
                       "what AwsCustomResource's framework provider role attaches internally.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="Wildcards come from AwsCustomResource SDK calls (describeAutoScalingGroups, "
                       "describeInstances, describeManagedPrefixLists, importCertificate) that have no "
                       "resource-level IAM support, and from Lambda's add_permission grants scoped by "
                       "source_account (the closest available scoping for vpc-lattice.amazonaws.com "
                       "invocation).",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="Flagged Lambda is AwsCustomResource's own framework-managed provider function "
                       "(used for the ASG/instance/prefix-list lookups and cert import above), not "
                       "project-authored code.",
            ),
            NagPackSuppression(
                id="AwsSolutions-S1",
                reason="Server access logging on the Lattice service access-logs bucket would need a "
                       "second bucket purely to log access to a lab's own access logs -- not worth it.",
            ),
        ])
        NagSuppressions.add_resource_suppressions(
            sn_endpoint_sg,
            [NagPackSuppression(
                id="AwsSolutions-EC23",
                reason="cdk-nag can't statically resolve vpc.vpc_cidr_block (a CloudFormation token) to "
                       "confirm it isn't 0.0.0.0/0 -- scoped to provider-vpc's own CIDR only.",
            )],
        )

    def _import_self_signed_certificate(self) -> str:
        """Generate a self-signed cert with openssl at synth time and import
        it into ACM via AwsCustomResource (ACM's ImportCertificate API has no
        native CloudFormation resource type). See this module's docstring for
        why: no real domain to validate a public cert against, and ACM
        Private CA's ~$400/month fixed cost doesn't fit a cost-conscious lab.
        Returns the resulting certificate's ARN (a token, resolved at deploy
        time)."""
        cert_dir = Path(__file__).parent / "assets" / "lattice" / "_generated_cert"
        cert_dir.mkdir(parents=True, exist_ok=True)
        cert_path = cert_dir / "cert.pem"
        key_path = cert_dir / "key.pem"
        # SAN covers both LAB_CUSTOM_DOMAIN (main service) and
        # LAB_TLS_CUSTOM_DOMAIN (tls_service) -- VPC Lattice validates
        # customDomainName against the certificate even for TLS_PASSTHROUGH
        # (confirmed live: "customDomainName does not match the provided
        # certificate"), so one cert needs to satisfy both rather than
        # importing a second certificate for the second service.
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key_path), "-out", str(cert_path),
                "-days", "3650", "-subj", f"/CN={LAB_CUSTOM_DOMAIN}",
                "-addext", f"subjectAltName=DNS:{LAB_CUSTOM_DOMAIN},DNS:{LAB_TLS_CUSTOM_DOMAIN}",
            ],
            check=True, capture_output=True,
        )
        cert_pem = cert_path.read_text()
        key_pem = key_path.read_text()

        import_call = cr.AwsSdkCall(
            service="ACM",
            action="importCertificate",
            parameters={"Certificate": cert_pem, "PrivateKey": key_pem},
            physical_resource_id=cr.PhysicalResourceId.from_response("CertificateArn"),
        )
        cert_import = cr.AwsCustomResource(
            self, "SelfSignedCertImport",
            on_create=import_call,
            on_update=import_call,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE),
        )
        return cert_import.get_response_field("CertificateArn")
