"""blog_assistant_stack.py -- the chat assistant embedded in the blog
(app/frontend-next/components/ChatConsole.tsx, mounted on the home page
Assistant Console section).

Three AWS pieces, same shape as agentic_ai_stack.py's semantic memory tier
but standing on its own, deployed unconditionally rather than gated behind
ENABLE_AI, since the whole point is that a visitor to the always-on blog
can use it without any feature flag being on:

  Knowledge base: a Bedrock Knowledge Base backed by an S3 Vectors index
  (aws_s3vectors.CfnVectorBucket / CfnIndex), ingesting this project's own
  blog content (stacks/assets/blog_assistant/kb_source) as its data
  source. Retrieval Augmented Generation, not a bare model with no
  grounding.

  Generation: Amazon Bedrock, Anthropic Claude (config.BEDROCK_MODEL_ID),
  called through the Converse API.

  Memory: a plain S3 bucket, one JSON object per conversation id, read at
  the start of a request and written back at the end. Explicit and
  inspectable rather than relying on any hidden session cache.

Client -> CloudFront (/assistant/* behavior added onto ThreeTierStack's
existing distribution, the same cross-stack construct-mutation pattern
blog_analytics_stack.py already uses) -> API Gateway HTTP API -> one Lambda
that does retrieval, generation, and memory read/write in a single request.
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3_deploy
from aws_cdk import aws_s3vectors as s3vectors
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.security_stack import SecurityStack
from stacks.threetier_stack import ThreeTierStack

ASSETS_DIR = Path(__file__).parent / "assets" / "blog_assistant"
KB_SOURCE_DIR = ASSETS_DIR / "kb_source"
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024


class BlogAssistantStack(Stack):
    """API Gateway + Lambda + Bedrock Knowledge Base (S3 Vectors) + S3
    memory, wired into ThreeTierStack's existing CloudFront distribution."""

    def __init__(
        self, scope: Construct, construct_id: str, *,
        security: SecurityStack, threetier: ThreeTierStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.BLOG_ASSISTANT)

        # ------------------------------------------------------------------
        # RAG: S3 Vectors index + Bedrock Knowledge Base. Same construct
        # pattern, same two previously-hit-and-fixed bugs guarded against,
        # as agentic_ai_stack.py's semantic memory tier:
        #  - S3 Vectors' own async indexing needs an explicit KMS grant to
        #    its own service principal, separate from any generic grant.
        #  - The KnowledgeBase resource races its own role's IAM policy
        #    unless it holds an explicit dependency on the Policy resource
        #    (a role_arn reference alone only depends on the Role itself).
        # ------------------------------------------------------------------
        self.vector_bucket = s3vectors.CfnVectorBucket(
            self, "BlogAssistantVectorBucket",
            vector_bucket_name="blog-assistant-knowledge",
            encryption_configuration=s3vectors.CfnVectorBucket.EncryptionConfigurationProperty(
                sse_type="aws:kms", kms_key_arn=security.buckets_key.key_arn,
            ),
        )
        security.buckets_key.grant_encrypt_decrypt(iam.ServicePrincipal("indexing.s3vectors.amazonaws.com"))
        self.vector_index = s3vectors.CfnIndex(
            self, "BlogAssistantVectorIndex",
            vector_bucket_arn=self.vector_bucket.attr_vector_bucket_arn,
            index_name="blog-assistant-index",
            data_type="float32",
            dimension=EMBEDDING_DIMENSIONS,
            distance_metric="cosine",
        )
        self.vector_index.add_dependency(self.vector_bucket)

        kb_role = iam.Role(
            self, "BlogAssistantKnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
        )
        kb_policy = iam.Policy(
            self, "BlogAssistantKnowledgeBaseRolePolicy",
            roles=[kb_role],
            statements=[
                iam.PolicyStatement(
                    sid="InvokeEmbeddingModel",
                    actions=["bedrock:InvokeModel"],
                    resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL_ID}"],
                ),
                iam.PolicyStatement(
                    sid="S3VectorsAccess",
                    actions=["s3vectors:GetVectors", "s3vectors:PutVectors", "s3vectors:QueryVectors", "s3vectors:GetIndex"],
                    resources=["*"],  # S3 Vectors ARNs aren't resolvable at synth time from these L1 attrs -- scoped to this KB's own role.
                ),
            ],
        )

        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self, "BlogAssistantKnowledgeBase",
            name="blog-assistant-knowledge",
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
        self.knowledge_base.node.add_dependency(kb_policy)
        self.knowledge_base.add_dependency(self.vector_index)

        kb_source_bucket = s3.Bucket(
            self, "BlogAssistantKnowledgeSourceBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security.buckets_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=security.s3_access_logs_bucket,
            server_access_logs_prefix="blog-assistant-kb-source/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        s3_deploy.BucketDeployment(
            self, "BlogAssistantKnowledgeSourceDeployment",
            sources=[s3_deploy.Source.asset(str(KB_SOURCE_DIR))],
            destination_bucket=kb_source_bucket,
        )
        kb_source_read_grant = kb_source_bucket.grant_read(kb_role)
        self.knowledge_base_data_source = bedrock.CfnDataSource(
            self, "BlogAssistantKnowledgeDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="blog-assistant-source",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=kb_source_bucket.bucket_arn,
                ),
            ),
        )
        kb_source_read_grant.apply_before(self.knowledge_base_data_source)

        # ------------------------------------------------------------------
        # Memory -- one JSON object per conversation id. Short TTL-style
        # lifecycle rule since a portfolio chat session has no reason to be
        # retained indefinitely; unlike blog_analytics_stack.py's raw
        # events this holds actual conversation text, so it gets a shorter
        # retention window (30 days) and its own bucket, never shared with
        # the knowledge base source content.
        # ------------------------------------------------------------------
        self.memory_bucket = s3.Bucket(
            self, "BlogAssistantMemoryBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security.buckets_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=security.s3_access_logs_bucket,
            server_access_logs_prefix="blog-assistant-memory/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
        )

        # ------------------------------------------------------------------
        # Ask Lambda -- retrieval, generation, and memory read/write in one
        # request.
        # ------------------------------------------------------------------
        ask_role = iam.Role(
            self, "BlogAssistantAskRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        self.memory_bucket.grant_read_write(ask_role)
        security.buckets_key.grant_encrypt_decrypt(ask_role)
        ask_role.add_to_policy(iam.PolicyStatement(
            # The Bedrock Agent Runtime Retrieve operation is IAM-namespaced
            # under bedrock-agent-runtime, not bedrock, despite the boto3
            # client method also being named "retrieve" -- confirmed live,
            # same distinction the permissions boundary already draws for
            # agentic_ai_stack.py's identical call.
            actions=["bedrock-agent-runtime:Retrieve"],
            resources=[self.knowledge_base.attr_knowledge_base_arn],
        ))
        ask_role.add_to_policy(iam.PolicyStatement(
            # config.BEDROCK_MODEL_ID is a cross-region inference profile id
            # (every current Anthropic model on Bedrock is INFERENCE_TYPE
            # profile-only -- confirmed live, the old direct foundation-model
            # id this pointed at had reached end of life). Converse against
            # an inference profile needs permission on the profile ARN AND
            # the underlying foundation model ARN it fans out to -- the
            # profile-only grant 403s with a ResourceNotFoundException-style
            # failure otherwise.
            actions=["bedrock:InvokeModel", "bedrock:Converse"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{config.BEDROCK_MODEL_ID}",
                f"arn:aws:bedrock:*::foundation-model/{config.BEDROCK_BASE_MODEL_ID}",
            ],
        ))

        ask_log_group = logs.LogGroup(
            self, "BlogAssistantAskLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            encryption_key=security.logs_key,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.ask_fn = lambda_.Function(
            self, "BlogAssistantAsk",
            function_name="blog-assistant-ask",
            description="POST /assistant/ask -- Bedrock Knowledge Base retrieval, Claude generation, S3 memory.",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(ASSETS_DIR), exclude=["kb_source"]),
            timeout=Duration.seconds(30),
            memory_size=512,
            role=ask_role,
            log_group=ask_log_group,
            environment={
                "KNOWLEDGE_BASE_ID": self.knowledge_base.attr_knowledge_base_id,
                "MODEL_ID": config.BEDROCK_MODEL_ID,
                "MEMORY_BUCKET": self.memory_bucket.bucket_name,
            },
        )
        self.ask_fn.node.add_dependency(self.knowledge_base_data_source)

        # ------------------------------------------------------------------
        # API Gateway HTTP API. Route path is the FULL path CloudFront
        # forwards (see threetier_stack.py's /api/* behavior and
        # blog_analytics_stack.py's identical note) -- CloudFront does not
        # strip the matched behavior's own prefix before forwarding.
        # ------------------------------------------------------------------
        self.http_api = apigwv2.HttpApi(
            self, "BlogAssistantApi",
            api_name="blog-assistant-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.OPTIONS],
                allow_headers=["Content-Type"],
            ),
        )
        self.http_api.add_routes(
            path="/assistant/ask",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration("AskIntegration", self.ask_fn),
        )

        # ------------------------------------------------------------------
        # Wire /assistant/* into ThreeTierStack's EXISTING CloudFront
        # distribution -- same cross-stack construct-mutation pattern
        # blog_analytics_stack.py already uses for /analytics/*.
        # ------------------------------------------------------------------
        api_domain = cdk.Fn.select(2, cdk.Fn.split("/", self.http_api.api_endpoint))
        assistant_origin = origins.HttpOrigin(
            api_domain,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )
        threetier.distribution.add_behavior(
            "/assistant/*",
            assistant_origin,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
        )

        CfnOutput(self, "BlogAssistantApiEndpoint", value=self.http_api.api_endpoint)
        CfnOutput(self, "BlogAssistantKnowledgeBaseId", value=self.knowledge_base.attr_knowledge_base_id)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10).
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="service-role/AWSLambdaBasicExecutionRole is the standard CloudWatch Logs write "
                       "policy every Lambda in this project uses.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="S3VectorsAccess and the two foundation-model InvokeModel/Converse statements are "
                       "scoped to this knowledge base's own role and to the two specific model ARNs this "
                       "project uses -- S3 Vectors resources have no ARN CDK can resolve at synth time, "
                       "same accepted gap as agentic_ai_stack.py's identical KnowledgeBaseRolePolicy. "
                       "memory_bucket.grant_read_write()/buckets_key.grant_encrypt_decrypt() are "
                       "CDK-generated grants scoped to this bucket/key's own ARN.",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="PYTHON_3_13 is the latest available managed Lambda runtime as of this build.",
            ),
            NagPackSuppression(
                id="AwsSolutions-APIG1",
                reason="Access logging on this single-route ask API would duplicate what the Lambda's own "
                       "CloudWatch Logs already capture for a lab-scale portfolio chat assistant.",
            ),
            NagPackSuppression(
                id="AwsSolutions-APIG4",
                reason="This endpoint is an intentionally-open portfolio chat assistant reached "
                       "same-origin through CloudFront -- there is no per-visitor identity to authorize "
                       "against, the same reasoning as blog_analytics_stack.py's identical suppression.",
            ),
        ])
