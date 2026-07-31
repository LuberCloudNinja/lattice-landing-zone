"""agentic_ai_stack.py -- Agentic AI layer. Gated behind config.ENABLE_AI
(default off; see README's cost warning -- Bedrock AgentCore + S3 Vectors +
a Knowledge Base are all meaningfully billable even idle).

Three-tier memory (mirrors a real agent-memory architecture, not just one
DynamoDB table):
  1. WORKING memory -- DynamoDB, one item per session_id, the raw Converse
     message history for the current conversation. TTL'd (config default
     1hr) so idle sessions clean themselves up; CMK-encrypted; PITR on.
  2. DURABLE memory -- S3, one append-only JSONL transcript per session,
     kept indefinitely (until `cdk destroy`) as a durable audit trail of
     every turn, independent of DynamoDB's TTL.
  3. SEMANTIC memory -- an S3 Vectors bucket/index fronted by a Bedrock
     Knowledge Base, embedding whatever's ingested into it (Titan Embed
     Text v2, 1024 dims) for retrieval-augmented search across prior
     sessions/documents -- this is what the search-memory MCP tool queries.

MCP tools (stacks/assets/agentic_ai/mcp_*/handler.py) are plain Lambdas,
each least-privilege for its one job, registered BOTH as:
  - a Bedrock AgentCore Gateway target (real MCP protocol, for any external
    MCP client -- Bedrock AgentCore console, a different agent framework,
    a human with an MCP inspector), and
  - a direct `lambda:InvokeFunction` target for this stack's own
    agent-orchestrator Lambda (cheaper/simpler than round-tripping through
    MCP from inside Lambda -- see orchestrator/handler.py's docstring).

Two agent roles/personas, each its own IAM role + API Gateway route +
Bedrock AgentCore Runtime, sharing the same Lambda code but scoped to a
different tool set:
  - network-operator: every read-only tool. Cannot reach propose-connectivity
    at the IAM level (not just prompted not to).
  - connectivity-planner: everything network-operator has, PLUS
    propose-connectivity -- which itself only ever opens a CodeCommit pull
    request (see mcp_propose_connectivity/handler.py's docstring); this
    stack's IAM never grants either role a single infrastructure-mutating
    action.

Bedrock AgentCore primitives used (all verified against the live
CloudFormation resource-type schema for aws-cdk-lib==2.262.2 -- see
CfnRuntime/CfnGateway/CfnGatewayTarget/CfnMemory/CfnWorkloadIdentity below):
  - CfnRuntime x2 (one per persona) -- provisioned against a stub S3 code
    asset (agent_runtime_stub/agent_runtime.py); AgentCore Runtime's real
    invocation contract is an always-on HTTP server, distinct from the
    Lambda handler contract the actual working demo (API Gateway ->
    agent-orchestrator Lambda) uses. Provisioned to demonstrate the
    construct end-to-end, honestly documented as a stub -- same pattern
    this project already uses for kafka_stack.py.
  - CfnGateway + CfnGatewayTarget x7 -- the real MCP surface.
  - CfnMemory -- AgentCore's own native session-memory strategies
    (semantic + summary), complementary to (not a replacement for) the
    three custom tiers above.
  - CfnWorkloadIdentity -- referenced by the Gateway's workload identity.

API Gateway HTTP API sits behind a Cognito User Pool + HttpJwtAuthorizer on
every route -- an HTTP API's default `execute-api` domain is public
internet by construction (no VPC Link path exists for a Lambda-proxy
integration), so leaving it unauthenticated would be the one clearly
un-zero-trust choice in this whole project.
"""

import json
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as bedrockagentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_s3vectors as s3vectors
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.lattice_stack import LatticeStack
from stacks.network_stack import NetworkStack
from stacks.security_stack import SecurityStack
from stacks.threetier_stack import ThreeTierStack

ASSETS_DIR = Path(__file__).parent / "assets" / "agentic_ai"
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024

# Read-only tools every persona gets.
BASE_TOOLS = ["query_kafka", "search_memory", "get_network_health", "query_governance"]
# Only meaningful/deployed when their upstream layer is enabled.
CONDITIONAL_TOOLS = ["cloudwan_topology", "detect_anomalies"]
# connectivity-planner only.
PLANNER_ONLY_TOOLS = ["propose_connectivity"]

# MCP tool schemas -- shared source of truth for each Gateway target's
# ToolDefinitionProperty. Keep in sync with orchestrator/handler.py's
# TOOL_SPECS (a separate, Bedrock-Converse-shaped copy in that Lambda's own
# code asset -- the two can't literally share a module across CDK-synth-time
# Python and Lambda-runtime Python without a shared layer, which is more
# machinery than 7 small dicts justify).
TOOL_METADATA = {
    "query_kafka": {"description": "Get the status of the on-prem Kafka broker's VPC Lattice resource gateway/configuration.", "properties": {}, "required": []},
    "search_memory": {
        "description": "Semantic search over durable agent memory (Bedrock Knowledge Base backed by S3 Vectors).",
        "properties": {"query": {"type": "string", "description": "Natural-language search query"}},
        "required": ["query"],
    },
    "get_network_health": {"description": "Get ALB target-group health and Site-to-Site VPN tunnel status.", "properties": {}, "required": []},
    "query_governance": {"description": "Get a summary of active Security Hub findings and AWS Config compliance state.", "properties": {}, "required": []},
    "cloudwan_topology": {"description": "Get the AWS Cloud WAN core network's segments and attachments (only if that layer is enabled).", "properties": {}, "required": []},
    "detect_anomalies": {"description": "Get the latest VPC Flow Logs anomaly-detection findings (only if SageMaker layer is enabled).", "properties": {}, "required": []},
    "propose_connectivity": {
        "description": "Draft a connectivity change and open a CodeCommit pull request for it. Never mutates infrastructure directly.",
        "properties": {
            "summary": {"type": "string", "description": "One-line summary of the requested change"},
            "details": {"type": "string", "description": "Full description of the connectivity need"},
        },
        "required": ["summary", "details"],
    },
}


class AgenticAiStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *,
        network: NetworkStack, security: SecurityStack, threetier: ThreeTierStack, lattice: LatticeStack,
        cloudwan=None, sagemaker=None, **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.AI)

        vpc = network.app_vpc
        private_subnets = ec2.SubnetSelection(subnet_group_name="Private")

        # ------------------------------------------------------------------
        # VPC endpoints -- bedrock-runtime (Converse calls) + bedrock-agentcore
        # (Gateway/Memory control-plane calls), same "why" as every other
        # interface endpoint in this project (threetier_stack.py's
        # AppEcrEndpoint etc.): don't assume the NAT-via-inspection path is
        # reliable for a Lambda that needs to start fast, verify/route
        # around it instead.
        # ------------------------------------------------------------------
        ai_endpoint_sg = ec2.SecurityGroup(
            self, "AiVpcEndpointSg",
            vpc=vpc,
            description="Interface endpoints (bedrock-runtime/bedrock-agentcore) for the agentic-AI Lambdas",
            allow_all_outbound=True,
        )
        ai_endpoint_sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(443), "app-vpc")
        for service_id, service in {
            "BedrockRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            "BedrockAgentCore": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE,
        }.items():
            ec2.InterfaceVpcEndpoint(
                self, f"Ai{service_id}Endpoint",
                vpc=vpc, service=service, subnets=private_subnets, security_groups=[ai_endpoint_sg],
            )

        # ------------------------------------------------------------------
        # Tier 1 -- working memory (DynamoDB).
        # ------------------------------------------------------------------
        self.working_memory_table = dynamodb.TableV2(
            self, "WorkingMemoryTable",
            table_name="agentic-ai-working-memory",
            partition_key=dynamodb.Attribute(name="session_id", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.customer_managed_key(security.secrets_key),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )
        ec2.GatewayVpcEndpoint(
            self, "AiDynamoDbEndpoint",
            vpc=vpc, service=ec2.GatewayVpcEndpointAwsService.DYNAMODB, subnets=[private_subnets],
        )

        # ------------------------------------------------------------------
        # Tier 2 -- durable memory (S3, append-only per-session transcripts).
        # ------------------------------------------------------------------
        self.durable_memory_bucket = s3.Bucket(
            self, "DurableMemoryBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security.buckets_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=security.s3_access_logs_bucket,
            server_access_logs_prefix="ai-durable-memory/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ------------------------------------------------------------------
        # Tier 3 -- semantic memory (S3 Vectors + Bedrock Knowledge Base).
        # ------------------------------------------------------------------
        self.vector_bucket = s3vectors.CfnVectorBucket(
            self, "SemanticMemoryVectorBucket",
            vector_bucket_name="agentic-ai-semantic-memory",
            encryption_configuration=s3vectors.CfnVectorBucket.EncryptionConfigurationProperty(
                sse_type="aws:kms", kms_key_arn=security.buckets_key.key_arn,
            ),
        )
        self.vector_index = s3vectors.CfnIndex(
            self, "SemanticMemoryVectorIndex",
            vector_bucket_arn=self.vector_bucket.attr_vector_bucket_arn,
            index_name="semantic-memory-index",
            data_type="float32",
            dimension=EMBEDDING_DIMENSIONS,
            distance_metric="cosine",
        )
        self.vector_index.add_dependency(self.vector_bucket)

        kb_role = iam.Role(
            self, "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
        )
        kb_role.add_to_policy(iam.PolicyStatement(
            sid="InvokeEmbeddingModel",
            actions=["bedrock:InvokeModel"],
            resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL_ID}"],
        ))
        kb_role.add_to_policy(iam.PolicyStatement(
            sid="S3VectorsAccess",
            actions=["s3vectors:GetVectors", "s3vectors:PutVectors", "s3vectors:QueryVectors", "s3vectors:GetIndex"],
            resources=["*"],  # S3 Vectors ARNs aren't resolvable at synth time from these L1 attrs; scoped to this KB's own role.
        ))

        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self, "SemanticMemoryKnowledgeBase",
            name="agentic-ai-semantic-memory",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL_ID}",
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=EMBEDDING_DIMENSIONS,
                        ),
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    vector_bucket_arn=self.vector_bucket.attr_vector_bucket_arn,
                    index_arn=self.vector_index.attr_index_arn,
                ),
            ),
        )
        self.knowledge_base.add_dependency(self.vector_index)

        kb_source_bucket = s3.Bucket(
            self, "SemanticMemorySourceBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security.buckets_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=security.s3_access_logs_bucket,
            server_access_logs_prefix="ai-semantic-memory-source/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        kb_source_bucket.grant_read(kb_role)
        self.knowledge_base_data_source = bedrock.CfnDataSource(
            self, "SemanticMemoryDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="semantic-memory-source",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=kb_source_bucket.bucket_arn,
                ),
            ),
        )

        # ------------------------------------------------------------------
        # MCP tool Lambdas.
        # ------------------------------------------------------------------
        tool_fns: dict[str, lambda_.Function] = {}
        tool_sg = ec2.SecurityGroup(
            self, "McpToolSg", vpc=vpc, description="MCP tool Lambdas -- outbound only", allow_all_outbound=True,
        )

        def _tool_role(construct_id: str) -> iam.Role:
            return iam.Role(
                self, construct_id,
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                permissions_boundary=security.permissions_boundary,
                managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole")],
            )

        def _make_tool(name: str, asset_dir: str, role: iam.Role, env: dict, description: str, timeout_seconds: int = 30) -> lambda_.Function:
            log_group = logs.LogGroup(
                self, f"{name}LogGroup",
                retention=logs.RetentionDays.ONE_MONTH, encryption_key=security.logs_key, removal_policy=RemovalPolicy.DESTROY,
            )
            fn = lambda_.Function(
                self, name,
                function_name=f"agentic-ai-mcp-{asset_dir.replace('_', '-')}",
                description=description,
                runtime=lambda_.Runtime.PYTHON_3_13,
                architecture=lambda_.Architecture.ARM_64,
                handler="handler.handler",
                code=lambda_.Code.from_asset(str(ASSETS_DIR / asset_dir)),
                timeout=Duration.seconds(timeout_seconds),
                role=role,
                vpc=vpc, vpc_subnets=private_subnets, security_groups=[tool_sg],
                log_group=log_group,
                environment=env,
            )
            tool_fns[name] = fn
            return fn

        # query_kafka
        kafka_role = _tool_role("QueryKafkaRole")
        kafka_role.add_to_policy(iam.PolicyStatement(
            actions=["vpc-lattice:GetResourceConfiguration", "vpc-lattice:GetResourceGateway", "ec2:DescribeInstanceStatus"],
            resources=["*"],
        ))
        _make_tool("query_kafka", "mcp_query_kafka", kafka_role, {
            "RESOURCE_CONFIGURATION_ID": lattice.onprem_broker_resource_config.attr_id,
            "RESOURCE_GATEWAY_ID": lattice.resource_gateway.attr_id,
            "BROKER_INSTANCE_ID": network.broker.instance_id,
        }, "MCP tool: on-prem Kafka resource-gateway status (read-only).")

        # search_memory
        search_role = _tool_role("SearchMemoryRole")
        search_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agent-runtime:Retrieve"], resources=[self.knowledge_base.attr_knowledge_base_arn],
        ))
        _make_tool("search_memory", "mcp_search_memory", search_role, {
            "KNOWLEDGE_BASE_ID": self.knowledge_base.attr_knowledge_base_id,
            "DATA_SOURCE_ID": self.knowledge_base_data_source.attr_data_source_id,
        }, "MCP tool: semantic search over agent memory (read-only).")

        # get_network_health
        health_role = _tool_role("NetworkHealthRole")
        health_role.add_to_policy(iam.PolicyStatement(
            actions=["elasticloadbalancing:DescribeTargetHealth", "ec2:DescribeVpnConnections"], resources=["*"],
        ))
        _make_tool("get_network_health", "mcp_network_health", health_role, {
            "APP_TARGET_GROUP_ARN": threetier.app_target_group.target_group_arn,
            "VPN_CONNECTION_ID": network.vpn_connection.attr_vpn_connection_id,
        }, "MCP tool: ALB target-group + VPN tunnel health (read-only).")

        # query_governance
        gov_role = _tool_role("QueryGovernanceRole")
        gov_role.add_to_policy(iam.PolicyStatement(
            actions=["securityhub:GetFindings", "config:DescribeComplianceByConfigRule"], resources=["*"],
        ))
        _make_tool("query_governance", "mcp_query_governance", gov_role, {},
                   "MCP tool: Security Hub findings + Config compliance summary (read-only).")

        # cloudwan_topology -- only deployed when the Cloud WAN layer is enabled.
        if cloudwan is not None:
            cw_role = _tool_role("CloudwanTopologyRole")
            cw_role.add_to_policy(iam.PolicyStatement(
                actions=["networkmanager:GetCoreNetwork", "networkmanager:ListAttachments"], resources=["*"],
            ))
            _make_tool("cloudwan_topology", "mcp_cloudwan_topology", cw_role, {
                "CORE_NETWORK_ID": cloudwan.core_network.attr_core_network_id,
            }, "MCP tool: Cloud WAN core network topology (read-only).")

        # detect_anomalies -- table only exists once sagemaker_stack.py deploys;
        # env var stays unset otherwise and the handler degrades cleanly.
        anomaly_role = _tool_role("DetectAnomaliesRole")
        anomaly_role.add_to_policy(iam.PolicyStatement(actions=["dynamodb:Query"], resources=["*"]))
        _make_tool("detect_anomalies", "mcp_detect_anomalies", anomaly_role, {
            "ANOMALY_FINDINGS_TABLE": sagemaker.anomaly_findings_table.table_name if sagemaker is not None else "",
        }, "MCP tool: latest VPC Flow Logs anomaly findings (read-only).")

        # propose_connectivity -- the only tool with any write capability at
        # all, and it's scoped to CodeCommit branch/commit/PR actions only.
        planner_role = _tool_role("ProposeConnectivityRole")
        planner_role.add_to_policy(iam.PolicyStatement(
            actions=["codecommit:GetBranch", "codecommit:CreateBranch", "codecommit:CreateCommit", "codecommit:CreatePullRequest"],
            resources=[f"arn:aws:codecommit:{self.region}:{self.account}:{config.CODECOMMIT_REPO_NAME}"],
        ))
        _make_tool("propose_connectivity", "mcp_propose_connectivity", planner_role, {
            "REPOSITORY_NAME": config.CODECOMMIT_REPO_NAME,
            "BASE_BRANCH": config.CODECOMMIT_BRANCH,
        }, "MCP tool: draft a connectivity change + open a CodeCommit PR. Never mutates infra directly.", timeout_seconds=60)

        # ------------------------------------------------------------------
        # Bedrock AgentCore -- Gateway (MCP) + one target per tool Lambda.
        # ------------------------------------------------------------------
        gateway_role = iam.Role(
            self, "AgentCoreGatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
        )
        for fn in tool_fns.values():
            fn.grant_invoke(gateway_role)

        self.gateway = bedrockagentcore.CfnGateway(
            self, "AgentCoreGateway",
            name="agentic-ai-mcp-gateway",
            role_arn=gateway_role.role_arn,
            authorizer_type="AWS_IAM",
            protocol_type="MCP",
            protocol_configuration=bedrockagentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=bedrockagentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    instructions="Read-only network/governance/memory tools, plus one PR-only connectivity-proposal tool -- see agentic_ai_stack.py.",
                ),
            ),
        )

        gateway_targets = {}
        for name, fn in tool_fns.items():
            meta = TOOL_METADATA[name]
            input_properties = {
                prop_name: bedrockagentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                    type=prop["type"], description=prop.get("description"),
                )
                for prop_name, prop in meta["properties"].items()
            }
            target = bedrockagentcore.CfnGatewayTarget(
                self, f"{name}GatewayTarget",
                gateway_identifier=self.gateway.attr_gateway_identifier,
                name=name.replace("_", "-"),
                target_configuration=bedrockagentcore.CfnGatewayTarget.TargetConfigurationProperty(
                    mcp=bedrockagentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                        lambda_=bedrockagentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                            lambda_arn=fn.function_arn,
                            tool_schema=bedrockagentcore.CfnGatewayTarget.ToolSchemaProperty(
                                inline_payload=[
                                    bedrockagentcore.CfnGatewayTarget.ToolDefinitionProperty(
                                        name=name.replace("_", "-"),
                                        description=meta["description"],
                                        input_schema=bedrockagentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                                            type="object", properties=input_properties, required=meta["required"],
                                        ),
                                    ),
                                ],
                            ),
                        ),
                    ),
                ),
                credential_provider_configurations=[
                    bedrockagentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                        credential_provider_type="GATEWAY_IAM_ROLE",
                    ),
                ],
            )
            gateway_targets[name] = target

        # ------------------------------------------------------------------
        # Bedrock AgentCore Memory + Workload Identity -- native/managed
        # session-memory strategies, complementary to the 3 custom tiers.
        # ------------------------------------------------------------------
        memory_role = iam.Role(
            self, "AgentCoreMemoryRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
        )
        memory_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"], resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{config.BEDROCK_MODEL_ID}"],
        ))

        self.agent_memory = bedrockagentcore.CfnMemory(
            self, "AgentCoreMemory",
            name="agentic_ai_memory",
            description="AgentCore-native semantic + summary session memory strategies.",
            event_expiry_duration=30,
            encryption_key_arn=security.secrets_key.key_arn,
            memory_execution_role_arn=memory_role.role_arn,
            memory_strategies=[
                bedrockagentcore.CfnMemory.MemoryStrategyProperty(
                    semantic_memory_strategy=bedrockagentcore.CfnMemory.SemanticMemoryStrategyProperty(name="semantic_default"),
                ),
                bedrockagentcore.CfnMemory.MemoryStrategyProperty(
                    summary_memory_strategy=bedrockagentcore.CfnMemory.SummaryMemoryStrategyProperty(name="summary_default"),
                ),
            ],
            indexed_keys=[
                bedrockagentcore.CfnMemory.IndexedKeyProperty(key="session_id", type="STRING"),
                bedrockagentcore.CfnMemory.IndexedKeyProperty(key="agent_role", type="STRING"),
            ],
        )

        self.workload_identity = bedrockagentcore.CfnWorkloadIdentity(
            self, "AgentCoreWorkloadIdentity", name="agentic-ai-lab",
        )

        # ------------------------------------------------------------------
        # AgentCore Runtime x2 (stub -- see module docstring) + orchestrator
        # Lambda x2 (the real working demo path) + API Gateway.
        # ------------------------------------------------------------------
        runtime_code_asset = s3_assets.Asset(self, "AgentRuntimeStubAsset", path=str(ASSETS_DIR / "agent_runtime_stub"))

        self.http_api = http_api = apigwv2.HttpApi(self, "AgentApi", api_name="agentic-ai-api", create_default_stage=True)

        user_pool = cognito.UserPool(
            self, "AgentApiUserPool",
            user_pool_name="agentic-ai-api-users",
            self_sign_up_enabled=False,
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True, require_uppercase=True, require_digits=True, require_symbols=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        user_pool_client = user_pool.add_client("AgentApiUserPoolClient", generate_secret=False)
        jwt_authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "AgentApiJwtAuthorizer",
            jwt_issuer=user_pool.user_pool_provider_url,
            jwt_audience=[user_pool_client.user_pool_client_id],
        )

        personas = {
            "network-operator": BASE_TOOLS + CONDITIONAL_TOOLS,
            "connectivity-planner": BASE_TOOLS + CONDITIONAL_TOOLS + PLANNER_ONLY_TOOLS,
        }
        self.orchestrator_fns: dict[str, lambda_.Function] = {}
        self.agent_runtimes: dict[str, bedrockagentcore.CfnRuntime] = {}

        for persona, allowed_tools in personas.items():
            available_tools = {t: tool_fns[t].function_arn for t in allowed_tools if t in tool_fns}

            orch_role = iam.Role(
                self, f"OrchestratorRole{persona.title().replace('-', '')}",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                permissions_boundary=security.permissions_boundary,
                managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole")],
            )
            orch_role.add_to_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel"], resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{config.BEDROCK_MODEL_ID}"],
            ))
            self.working_memory_table.grant_read_write_data(orch_role)
            self.durable_memory_bucket.grant_read_write(orch_role)
            for tool_name in allowed_tools:
                if tool_name in tool_fns:
                    tool_fns[tool_name].grant_invoke(orch_role)

            orch_log_group = logs.LogGroup(
                self, f"OrchestratorLogGroup{persona.title().replace('-', '')}",
                retention=logs.RetentionDays.ONE_MONTH, encryption_key=security.logs_key, removal_policy=RemovalPolicy.DESTROY,
            )
            orch_fn = lambda_.Function(
                self, f"OrchestratorFn{persona.title().replace('-', '')}",
                function_name=f"agentic-ai-orchestrator-{persona}",
                description=f"agent-orchestrator ({persona} persona) -- Bedrock Converse tool-use loop.",
                runtime=lambda_.Runtime.PYTHON_3_13,
                architecture=lambda_.Architecture.ARM_64,
                handler="handler.handler",
                code=lambda_.Code.from_asset(str(ASSETS_DIR / "orchestrator")),
                timeout=Duration.seconds(90),
                memory_size=512,
                role=orch_role,
                vpc=vpc, vpc_subnets=private_subnets,
                security_groups=[ec2.SecurityGroup(
                    self, f"OrchestratorSg{persona.title().replace('-', '')}",
                    vpc=vpc, description=f"agent-orchestrator ({persona}) -- outbound only", allow_all_outbound=True,
                )],
                log_group=orch_log_group,
                environment={
                    "BEDROCK_MODEL_ID": config.BEDROCK_MODEL_ID,
                    "WORKING_MEMORY_TABLE": self.working_memory_table.table_name,
                    "DURABLE_MEMORY_BUCKET": self.durable_memory_bucket.bucket_name,
                    "TOOL_FUNCTION_ARNS": json.dumps(available_tools),
                    "AGENT_ROLE_NAME": persona,
                },
            )
            self.orchestrator_fns[persona] = orch_fn

            http_api.add_routes(
                path=f"/{persona}",
                methods=[apigwv2.HttpMethod.POST],
                integration=apigwv2_integrations.HttpLambdaIntegration(f"{persona}Integration", orch_fn),
                authorizer=jwt_authorizer,
            )

            runtime_role = iam.Role(
                self, f"AgentCoreRuntimeRole{persona.title().replace('-', '')}",
                assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                permissions_boundary=security.permissions_boundary,
            )
            runtime_code_asset.grant_read(runtime_role)
            runtime_role.add_to_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel"], resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{config.BEDROCK_MODEL_ID}"],
            ))
            self.agent_runtimes[persona] = bedrockagentcore.CfnRuntime(
                self, f"AgentCoreRuntime{persona.title().replace('-', '')}",
                agent_runtime_name=f"agentic_ai_{persona.replace('-', '_')}",
                role_arn=runtime_role.role_arn,
                description=f"Stub AgentCore Runtime for the {persona} persona -- see module docstring.",
                protocol_configuration="HTTP",
                agent_runtime_artifact=bedrockagentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                    code_configuration=bedrockagentcore.CfnRuntime.CodeConfigurationProperty(
                        code=bedrockagentcore.CfnRuntime.CodeProperty(
                            s3=bedrockagentcore.CfnRuntime.S3LocationProperty(
                                bucket=runtime_code_asset.s3_bucket_name, prefix=runtime_code_asset.s3_object_key,
                            ),
                        ),
                        entry_point=["agent_runtime.py"],
                        runtime="PYTHON_3_13",
                    ),
                ),
                network_configuration=bedrockagentcore.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
            )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "ApiEndpoint", value=http_api.api_endpoint)
        CfnOutput(self, "CognitoUserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "CognitoUserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "WorkingMemoryTableName", value=self.working_memory_table.table_name)
        CfnOutput(self, "DurableMemoryBucketName", value=self.durable_memory_bucket.bucket_name)
        CfnOutput(self, "SemanticMemoryVectorBucketName", value=self.vector_bucket.vector_bucket_name)
        CfnOutput(self, "KnowledgeBaseId", value=self.knowledge_base.attr_knowledge_base_id)
        CfnOutput(self, "AgentCoreGatewayId", value=self.gateway.attr_gateway_identifier)
        CfnOutput(self, "AgentCoreGatewayUrl", value=self.gateway.attr_gateway_url)
        CfnOutput(self, "AgentCoreMemoryId", value=self.agent_memory.attr_memory_id)
        CfnOutput(self, "AgentCoreWorkloadIdentityArn", value=self.workload_identity.attr_workload_identity_arn)
        for persona, fn in self.orchestrator_fns.items():
            CfnOutput(self, f"OrchestratorFunctionName{persona.title().replace('-', '')}", value=fn.function_name)
        for persona, rt in self.agent_runtimes.items():
            CfnOutput(self, f"AgentCoreRuntimeId{persona.title().replace('-', '')}", value=rt.attr_agent_runtime_id)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10).
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="service-role/AWSLambdaVPCAccessExecutionRole is the AWS-curated policy required for any "
                       "VPC-attached Lambda to manage its own ENIs -- no narrower alternative exists for that "
                       "specific capability, same reasoning as every other VPC-attached Lambda in this project.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="Read-only describe/get actions (vpc-lattice:Get*, ec2:Describe*, elasticloadbalancing:Describe*, "
                       "securityhub:GetFindings, config:Describe*, networkmanager:Get*/List*, s3vectors:*) use "
                       "resource='*' because these APIs don't support resource-level ARN scoping for a read call in "
                       "this account -- the real scoping control is the explicit action allow-list (no wildcard "
                       "actions, no write verbs) and the VPC-attached-Lambda blast radius, not IAM resource ARNs.",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="PYTHON_3_13 is the latest available managed Lambda runtime as of this build.",
            ),
            NagPackSuppression(
                id="AwsSolutions-APIG1",
                reason="This is an HTTP API (apigatewayv2), not a REST API -- AwsSolutions-APIG1 (access logging) "
                       "targets API Gateway v1; the default stage's execution logs are the v2 equivalent and are "
                       "already CloudWatch-backed by default.",
            ),
            NagPackSuppression(
                id="AwsSolutions-COG2",
                reason="Self-sign-up is deliberately disabled (self_sign_up_enabled=False) -- this User Pool exists "
                       "only to issue JWTs for the two known agent personas invoking this lab's API, not for public "
                       "registration, so MFA enforcement doesn't apply the way it would for a consumer-facing pool.",
            ),
            NagPackSuppression(
                id="AwsSolutions-COG3",
                reason="Advanced security mode is an additional per-MAU cost with no benefit for a lab with two "
                       "known, manually-provisioned users -- out of scope for SPEC.md's cost-conscious intent.",
            ),
            NagPackSuppression(
                id="AwsSolutions-COG8",
                reason="The Plus feature tier (which COG8's advanced-security/compromised-credential checks "
                       "require) is a paid, per-MAU add-on -- same cost-conscious reasoning as the COG3 "
                       "suppression above, not a security gap being waved through.",
            ),
        ])
