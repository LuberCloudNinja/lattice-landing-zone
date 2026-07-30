#!/usr/bin/env python3
"""CDK app entry point.

Two independent top-level things are instantiated, per SPEC.md Sections 7-8:

  - LandingZoneStage("LandingZone-Dev"), deployed directly with
    `cdk deploy --all` -- fast local iteration, no pipeline involved.
  - PipelineStack, deployed once by hand (`cdk deploy PipelineStack`) and
    self-mutating from there -- the real, git-driven path. It wraps its own
    separate LandingZoneStage("LandingZone") instance (a Stage can only be
    parented once, so the pipeline can't reuse the Dev one above).

PipelineStack sources from CodeCommit (config.CODECOMMIT_REPO_NAME /
CODECOMMIT_BRANCH) -- no OAuth/App handshake required, just IAM
credentials, so it's deployable as-is once the CodeCommit repo exists.
"""

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

import config
from pipeline_stack import PipelineStack
from stage import LandingZoneStage

app = cdk.App()

env = cdk.Environment(account=config.AWS_ACCOUNT_ID, region=config.AWS_REGION)

landing_zone_dev = LandingZoneStage(app, "LandingZone-Dev", env=env)
PipelineStack(app, "PipelineStack", env=env)

# cdk-nag (SPEC.md Section 10) -- applied to the Dev stage only, not
# PipelineStack's own internal CodePipeline/CodeBuild plumbing or its
# nested copy of this same stage (identical resource shapes; checking twice
# would just double the findings to review, not add coverage).
cdk.Aspects.of(landing_zone_dev).add(AwsSolutionsChecks(verbose=True))

for key, value in config.STANDARD_TAGS.items():
    cdk.Tags.of(app).add(key, value)

app.synth()
