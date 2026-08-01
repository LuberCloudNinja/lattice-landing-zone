"""Environment, CIDR, feature-flag, and tagging constants for the lattice-landing-zone stacks.

Single source of truth per SPEC.md Section 3.5 / Section 4 -- stacks import this
module rather than hardcoding any of the values below.
"""

import os

# ---------------------------------------------------------------------------
# AWS environment
# ---------------------------------------------------------------------------
# No hardcoded fallback -- if CDK_DEFAULT_ACCOUNT isn't set, this stays None,
# which cdk.Environment() treats as "environment-agnostic" and resolves from
# whatever AWS credentials/profile the CDK CLI is currently using.
AWS_ACCOUNT_ID = os.environ.get("CDK_DEFAULT_ACCOUNT")
AWS_REGION = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

# Local AWS CLI / CDK commands should be run with `--profile deloitte` (or
# AWS_PROFILE=deloitte) -- the dedicated admin IAM user provisioned for this
# build. Not consumed by app code (profile selection is a credential-resolution
# concern, not an app-code one) -- documented here for README/operator reference.
AWS_PROFILE = "deloitte"

# ---------------------------------------------------------------------------
# CodeCommit / CDK Pipeline (SPEC.md Section 7) -- pipeline source.
# ---------------------------------------------------------------------------
# GitHub (github.com/LuberCloudNinja/lattice-landing-zone) still exists as a
# public mirror but is NOT the pipeline's source -- CodeConnections' GitHub
# App required a separate, easy-to-miss "install the app on repos" step
# beyond OAuth authorization, which kept the Source stage failing with
# "No Branch [main] found" even once the connection itself showed AVAILABLE.
# CodeCommit needs no third-party OAuth/App handshake at all -- IAM
# credentials (a service-specific git credential on deloitte-admin) are
# sufficient, so it's the more reliable single source of truth for the
# pipeline itself.
CODECOMMIT_REPO_NAME = os.environ.get("CODECOMMIT_REPO_NAME", "lattice-landing-zone")
CODECOMMIT_BRANCH = os.environ.get("CODECOMMIT_BRANCH", "main")

# ---------------------------------------------------------------------------
# Web app source (threetier_stack.py) -- fill in before building that stack.
# ---------------------------------------------------------------------------
WEBAPP_SOURCE = os.environ.get("WEBAPP_SOURCE", "REPLACE_ME_WEBAPP_SOURCE")

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
# Flips non-HA tiers (e.g. RDS) from single-AZ to 2-AZ. Default off for lab cost
# (SPEC.md Section 1: "single-AZ for non-HA tiers").
MULTI_AZ = os.environ.get("MULTI_AZ", "false").lower() == "true"

# The inspection VPC's AZ count is NOT governed by MULTI_AZ above -- SPEC.md
# Section 1 and Section 6 fix it at 2 AZs with two firewall appliances per AZ
# (4 total) as a hard HA requirement, independent of the cost flag used
# elsewhere in the stack.
INSPECTION_AZ_COUNT = 2
FIREWALL_APPLIANCES_PER_AZ = 2

# Kafka stack is deployed last and stays off until explicitly enabled
# (SPEC.md Section 6: "deploy LAST, flag ENABLE_KAFKA=False at first").
ENABLE_KAFKA = os.environ.get("ENABLE_KAFKA", "false").lower() == "true"

# Optional AWS RAM cross-account share for VPC Lattice L4f -- only meaningful
# once a second account id is supplied (SPEC.md Section 0 / Section 5 L4f).
SECOND_ACCOUNT_ID = os.environ.get("SECOND_ACCOUNT_ID")  # None if not provided
ENABLE_RAM_SHARE = bool(SECOND_ACCOUNT_ID)

# Multi-region AWS Cloud WAN layer (cloudwan_stack.py / region2_stack.py).
# Default off -- Cloud WAN's core network + attachments are materially more
# expensive than the TGW already in use (per-attachment + per-GB data
# processing charges in every attached edge location), so this is meant to
# be flipped on for a demo, toured, then torn down, not left running.
ENABLE_CLOUDWAN = os.environ.get("ENABLE_CLOUDWAN", "false").lower() == "true"
SECOND_REGION = os.environ.get("SECOND_REGION", "us-east-2")

# Governance / drift-remediation (governance_stack.py / drift_remediation_stack.py).
REMEDIATION_EMAIL = os.environ.get("REMEDIATION_EMAIL", "you@example.com")
# True = the drift-remediator Lambda only alerts (SNS), never deletes.
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Agentic AI layer (agentic_ai_stack.py) -- API Gateway + orchestrator Lambda +
# Bedrock AgentCore Runtime/Gateway/Memory/Identity + MCP tool Lambdas. Off by
# default: Bedrock AgentCore + S3 Vectors + a Knowledge Base are all
# meaningfully billable even idle, so this is a deploy-tour-teardown demo
# layer like ENABLE_CLOUDWAN, not something to leave running.
ENABLE_AI = os.environ.get("ENABLE_AI", "false").lower() == "true"
# Current Anthropic models on Bedrock are INFERENCE_PROFILE only (confirmed
# live via `aws bedrock list-foundation-models` -- claude-3-5-sonnet-* is
# LEGACY/EOL). Converse must be called with the cross-region inference
# profile id, not a bare foundation-model id -- callers deriving an IAM
# resource ARN from this need both the inference-profile ARN and the
# underlying foundation-model ARN (see BEDROCK_BASE_MODEL_ID below).
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5")
BEDROCK_BASE_MODEL_ID = BEDROCK_MODEL_ID.split(".", 1)[1] if "." in BEDROCK_MODEL_ID else BEDROCK_MODEL_ID

# SageMaker anomaly-detection layer (sagemaker_stack.py) -- Async Inference
# endpoint + RCF training on VPC Flow Logs. Independently gated from
# ENABLE_AI since it's a separate, also-billable-even-idle demo layer (an
# Async Inference endpoint bills for its underlying instance while it
# exists, scale-to-zero notwithstanding for the instance itself).
ENABLE_SAGEMAKER = os.environ.get("ENABLE_SAGEMAKER", "false").lower() == "true"

# ---------------------------------------------------------------------------
# VPC CIDR blocks (SPEC.md Section 4)
# ---------------------------------------------------------------------------
VPC_CIDRS = {
    "onprem": "10.100.0.0/16",  # simulated on-prem datacenter
    "inspection": "10.0.0.0/16",  # GWLB + firewall fleet
    "app": "10.1.0.0/16",  # spoke: three-tier app
    "provider": "10.2.0.0/16",  # PrivateLink provider
}

# Second-region (SECOND_REGION) CIDRs -- deliberately non-overlapping with
# VPC_CIDRS above so the two regions could, in principle, be connected
# without a conflict (Cloud WAN doesn't NAT between segments).
REGION2_VPC_CIDRS = {
    "region2_prod": "10.20.0.0/16",
    "region2_shared": "10.21.0.0/16",
}

# ---------------------------------------------------------------------------
# AWS Cloud WAN segment names -- matching the customer's own vocabulary, used
# verbatim in cloudwan_stack.py's core network policy document and as the
# `segment` tag value attachments key off of (attachment-policies).
# ---------------------------------------------------------------------------
class CloudWanSegment:
    FASTTRACK = "FastTrack"  # prod
    SKYPATH = "SkyPath"  # proxy
    SKYTRANSIT = "SkyTransit"  # hybrid (on-prem/TGW reachability)
    WORKLOAD = "Workload"

# ---------------------------------------------------------------------------
# Instance sizing (lab-cost defaults per SPEC.md Section 1)
# ---------------------------------------------------------------------------
DEFAULT_INSTANCE_TYPE = "t3.micro"
APP_TIER_INSTANCE_TYPE = "t3.small"
NAT_GATEWAY_COUNT = 1  # single, centralized -- put internet-needing things in app-vpc

# ---------------------------------------------------------------------------
# Tagging (SPEC.md Section 3.5) -- single source of truth, also drives the
# per-layer AWS Resource Groups built in resource_groups_stack.py.
# ---------------------------------------------------------------------------
PROJECT_TAG = "lattice-lab"
OWNER_TAG = os.environ.get("OWNER_TAG", "your-org")

STANDARD_TAGS = {
    "Project": PROJECT_TAG,
    "Environment": "lab",
    "Owner": OWNER_TAG,
    "CostCenter": "interview-lab",
    "ManagedBy": "cdk",
}


class Layer:
    """One value per stacks/*.py -- applied as that stack's `Layer` tag."""

    NETWORK = "network"
    INSPECTION = "inspection"
    THREETIER = "threetier"
    PRIVATELINK = "privatelink"
    LATTICE = "lattice"
    KAFKA = "kafka"
    OBSERVABILITY = "observability"
    DIAGRAM = "diagram"
    SECURITY = "security"
    GOVERNANCE = "governance"
    REMEDIATION = "remediation"
    ORG = "org"
    CLOUDWAN = "cloudwan"
    REGION2 = "region2"
    AI = "ai"
    SAGEMAKER = "sagemaker"
    BLOG_ANALYTICS = "blog-analytics"
    BLOG_ASSISTANT = "blog-assistant"


ALL_LAYERS = [
    Layer.NETWORK,
    Layer.INSPECTION,
    Layer.THREETIER,
    Layer.PRIVATELINK,
    Layer.LATTICE,
    Layer.KAFKA,
    Layer.OBSERVABILITY,
    Layer.DIAGRAM,
    Layer.SECURITY,
    Layer.GOVERNANCE,
    Layer.REMEDIATION,
    Layer.ORG,
    Layer.CLOUDWAN,
    Layer.REGION2,
    Layer.AI,
    Layer.SAGEMAKER,
    Layer.BLOG_ANALYTICS,
    Layer.BLOG_ASSISTANT,
]
