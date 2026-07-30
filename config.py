"""Environment, CIDR, feature-flag, and tagging constants for the lattice-landing-zone stacks.

Single source of truth per SPEC.md Section 3.5 / Section 4 -- stacks import this
module rather than hardcoding any of the values below.
"""

import os

# ---------------------------------------------------------------------------
# AWS environment
# ---------------------------------------------------------------------------
AWS_ACCOUNT_ID = os.environ.get("CDK_DEFAULT_ACCOUNT", "458798438816")
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

# ---------------------------------------------------------------------------
# VPC CIDR blocks (SPEC.md Section 4)
# ---------------------------------------------------------------------------
VPC_CIDRS = {
    "onprem": "10.100.0.0/16",  # simulated on-prem datacenter
    "inspection": "10.0.0.0/16",  # GWLB + firewall fleet
    "app": "10.1.0.0/16",  # spoke: three-tier app
    "provider": "10.2.0.0/16",  # PrivateLink provider
}

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
OWNER_TAG = os.environ.get("OWNER_TAG", "LuberCloudNinja")

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


ALL_LAYERS = [
    Layer.NETWORK,
    Layer.INSPECTION,
    Layer.THREETIER,
    Layer.PRIVATELINK,
    Layer.LATTICE,
    Layer.KAFKA,
    Layer.OBSERVABILITY,
    Layer.DIAGRAM,
]
