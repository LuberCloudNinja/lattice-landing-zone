# lattice-landing-zone

A hands-on lab mirroring an enterprise "Lattice-first" hybrid landing zone, built
entirely in AWS CDK (Python) -- see [`SPEC.md`](SPEC.md) for the full build spec
and [`docs/inspection-architecture-reference.md`](docs/inspection-architecture-reference.md)
for the detailed GWLB/firewall/diagram design this project implements.

> **Disclaimer:** this is a personal reference/interview-prep lab, not a
> production landing zone. "On-prem" is simulated with an EC2-based Libreswan
> gateway + Site-to-Site VPN (no real Direct Connect); everything lives in a
> single AWS account (with an optional second region for the Cloud WAN demo);
> and the whole point is deploy-tour-and-teardown via the CDK pipeline, not a
> long-running environment. Treat every architectural choice here as
> lab-appropriate, not a recommendation for how to run this in a real org.

**Nothing in this repo has been deployed.** Every stack below has only ever been
`cdk synth`'d. Read the "Before you deploy" section before running `cdk deploy`
anywhere.

## Prerequisites

Homebrew, `awscli`, `node`, `python@3.12`, `git`, `npm i -g aws-cdk` (all already
confirmed installed for this build -- CDK CLI 2.1133.0, Python 3.12.13, Node
24.16). Credentials: a dedicated IAM admin user (`deloitte-admin`, group
`deloitte`, `AdministratorAccess`) already exists in this account, configured
locally as CLI profile `deloitte` (`~/.aws/credentials`). Use
`--profile deloitte` / `AWS_PROFILE=deloitte` for every command below.

```bash
cd lattice-landing-zone
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Before you deploy

`PipelineStack` sources from AWS CodeCommit (`config.CODECOMMIT_REPO_NAME` /
`CODECOMMIT_BRANCH`, default `lattice-landing-zone` / `main`) -- create the
repo once (`aws codecommit create-repository --repository-name
lattice-landing-zone --profile deloitte`) and push this repo's `main` branch
to it. No OAuth/App handshake required (unlike the GitHub CodeConnections
path this project used originally -- see `pipeline_stack.py`'s module
docstring for why that was dropped): a CodeCommit HTTPS git credential on
the `deloitte-admin` IAM user (`aws iam create-service-specific-credential
--user-name deloitte-admin --service-name codecommit.amazonaws.com`) is
enough to `git push`.

| Placeholder | Needed for |
|---|---|
| `WEBAPP_SOURCE` | Not yet used -- `threetier_stack.py` currently deploys the placeholder app in `app/` (clearly labeled as such) instead. Swap `app/` for the real app and redeploy `ThreeTierStack` whenever this is provided |

`LandingZoneStage("LandingZone-Dev")` (direct `cdk deploy --all` path) needs
none of the above and is deployable as-is.

**Every stack sets `RemovalPolicy.DESTROY` + `auto_delete_objects`/
`deletion_protection=False` throughout specifically so `cdk destroy --all`
leaves nothing behind** -- this is a deliberate, verified design choice (see
the cdk-nag suppression for `AwsSolutions-EC29` in each stack with EC2
instances), not an oversight to fix before a "real" deployment.

## Bootstrap (once)

```bash
cdk bootstrap aws://<YOUR_ACCOUNT_ID>/us-east-1 --profile deloitte
```

Only if you plan to turn on `ENABLE_CLOUDWAN` (off by default -- see
"Multi-Region AWS Cloud WAN" below): also bootstrap the second region,
*before* deploying, since `region2_stack.py` deploys into it directly:

```bash
cdk bootstrap aws://<YOUR_ACCOUNT_ID>/us-east-2 --profile deloitte
```

## Deploy

**Fast local iteration (no pipeline, recommended for building/testing one
stack at a time):**

```bash
cdk deploy --all --profile deloitte
# or a single stack while iterating:
cdk deploy LandingZone-Dev/NetworkStack --profile deloitte
```

**Git-driven path (self-mutating CDK Pipeline, once the placeholders above are
filled in):**

```bash
cdk deploy PipelineStack --profile deloitte   # one-time; every push after this deploys automatically
```

Either path deploys stacks in dependency order:
`SecurityStack -> NetworkStack -> InspectionStack -> ThreeTierStack ->
PrivateLinkStack -> LatticeStack -> ObservabilityStack -> ResourceGroupsStack
-> DiagramStack -> GovernanceStack -> DriftRemediationStack`
(`KafkaStack` and `CloudWanStack`/`Region2Stack` stay off; see Feature flags
below). `SecurityStack` deploys FIRST -- every other stack imports its CMKs
and permissions boundary as constructor props.

**Scope B (`org-governance/`) is a separate app, deployed separately, into a
different (management) account -- see `org-governance/README.md`.** It is
never part of the `cdk deploy --all` or pipeline path above.

`cdk deploy` will show a full plan and prompt for approval before touching
AWS -- review resource counts and any `IAM Statement Changes` there before
approving, same review you'd want even though this README exists.

## Feature flags (`config.py`)

| Flag | Default | Effect |
|---|---|---|
| `MULTI_AZ` | `false` | Flips non-HA tiers (onprem-vpc/provider-vpc AZ count, ThreeTierStack/PrivateLinkStack's Fargate desired task count) from single-AZ/single-task to 2. Does **not** affect app-vpc, which is fixed at >=2 AZs regardless (originally an RDS DBSubnetGroup requirement, kept after that stack's DynamoDB swap -- see network_stack.py), or the inspection VPC, which is always 2 AZs (`config.INSPECTION_AZ_COUNT`) with 2 firewall appliances per AZ (`config.FIREWALL_APPLIANCES_PER_AZ`) regardless -- that's a fixed HA requirement, not a cost/HA trade-off. |
| `ENABLE_KAFKA` | `false` | `KafkaStack` isn't instantiated at all until this is `true` -- deploy everything else first, per SPEC.md's "deploy LAST" instruction. |
| `ENABLE_RAM_SHARE` / `SECOND_ACCOUNT_ID` | off | Set `SECOND_ACCOUNT_ID` to actually create the AWS RAM cross-account share of the Lattice service network; otherwise `LatticeStack` prints what *would* be created (`RamShareNotCreated` output). |
| `REMEDIATION_EMAIL` | `you@example.com` | Email subscribed to `DriftRemediationStack`'s `governance-alerts` SNS topic -- set this to your own address before deploying, then confirm the subscription (check your inbox for a "AWS Notification - Subscription Confirmation" email) right after first deploy, or you won't receive anything. |
| `DRY_RUN` | `false` | `true` makes the drift-remediator Lambda alert-only for every manual change it detects -- never deletes anything, regardless of tags. Useful for a first pass to see what it *would* have done before trusting it to actually delete. |
| `ENABLE_CLOUDWAN` | `false` | `CloudWanStack`/`Region2Stack` aren't instantiated at all until this is `true`. **Materially more expensive than the TGW already in use** -- see "Multi-Region AWS Cloud WAN" below before turning this on. |
| `SECOND_REGION` | `us-east-2` | Where `Region2Stack` deploys when `ENABLE_CLOUDWAN=true`. Requires bootstrapping that region first (see Bootstrap above). |
| `ENABLE_AI` | `false` | `AgenticAiStack` isn't instantiated at all until this is `true`. Bedrock AgentCore + S3 Vectors + a Knowledge Base are all meaningfully billable even idle -- see "Agentic AI + SageMaker" below before turning this on. |
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-5-sonnet-20240620-v1:0` | Model the agent-orchestrator Lambdas call via Bedrock Converse. |
| `ENABLE_SAGEMAKER` | `false` | `SageMakerStack` isn't instantiated at all until this is `true`. An Async Inference endpoint bills for its underlying instance while it exists, scale-to-zero notwithstanding for idle time -- see "Agentic AI + SageMaker" below. Independent flag from `ENABLE_AI`; the `detect_anomalies` MCP tool degrades cleanly if this is off while AI is on. |

## Security + Governance

Deployed by default (not gated behind a flag -- CloudTrail/Config/Security
Hub/GuardDuty/a single Lambda cost cents at this scale, not dollars):

- **`SecurityStack`** -- five customer-managed KMS keys (logs, buckets, ebs,
  secrets, sns; rotation on), a permissions boundary applied to every IAM
  role this project's own code authors (see that stack's module docstring
  for why it's *not* a blanket Aspect over the whole app -- it would also
  catch CDK's own internal custom-resource roles, whose IAM needs aren't
  something `cdk synth` alone can verify), IAM Access Analyzer, and the
  shared S3 server-access-log destination bucket every other bucket in this
  project points to.
- **`GovernanceStack`** -- CloudTrail (multi-region, log file validation,
  CMK-encrypted + versioned S3 + CloudWatch Logs), AWS Config (all resource
  types, 8 managed rules: required-tags, S3 versioning/encryption, EBS
  encryption, CloudTrail enabled, VPC Flow Logs, restricted SSH, no
  admin-access IAM policies), Security Hub (AWS Foundational Security Best
  Practices + CIS standards), GuardDuty.
- **`DriftRemediationStack`** -- "only the pipeline may change infra," made
  responsive: an EventBridge rule watches CloudTrail for mutating API calls
  NOT made by this project's own CDK bootstrap execution role, and triggers
  a Lambda that alerts via SNS (`governance-alerts`, emailed to
  `REMEDIATION_EMAIL`) and -- only for a small, explicit allow-list of
  resource types, and only if the resource lacks this project's own
  `ManagedBy=cdk` tag, and only if the caller isn't tagged
  `BreakGlass=true` -- deletes the one specific resource the event touched.
  Read that stack's module docstring before changing anything here; the
  scoping is the entire safety story for a Lambda with delete permissions.
  Honors `DRY_RUN` (alert-only, never deletes).

**Scope B** (org-wide OUs, SCPs, Control Tower -- the preventive,
management-account-only counterpart to the detective controls above) lives
in the separate `org-governance/` app; see its own README.md.

## Multi-Region AWS Cloud WAN

Off by default (`ENABLE_CLOUDWAN=false`). **Turn this on to demo it, tour
it, then turn it back off and `cdk destroy`** -- Cloud WAN's core network
and attachments bill per-attachment and per-GB processed in every attached
edge location, on top of (not instead of) the Transit Gateway this project
already runs; it is not a cheap thing to leave running.

- **Segments** (`cloudwan_stack.py`'s policy-as-code, matching the
  customer's own vocabulary): `FastTrack` (prod), `SkyPath` (proxy),
  `SkyTransit` (hybrid/on-prem reachability), `Workload` (isolated --
  `isolate-attachments=true`, nothing in it reaches anything else in it
  without an explicit share). `SkyTransit` is explicitly shared into
  `FastTrack` (`segment-actions`) -- the isolation-vs-sharing demo asked
  for in L7 of the original build request: attach something to `Workload`
  and confirm it can't reach `FastTrack`; attach something to `SkyTransit`
  and confirm it can.
- **VPC attachments**: `app-vpc` (`Workload`) and `provider-vpc`
  (`FastTrack`, this project's closest analog to a shared-services VPC) in
  us-east-1; a second small Workload VPC (`region2_stack.py`, a
  NAT/IGW-free VPC reachable only via SSM) in us-east-2.
- **TGW-peering migration path**: the existing Transit Gateway is
  registered into the same Global Network and peered with the core network
  -- confirmed working, both reach `AVAILABLE` reliably -- an incremental,
  additive path onto Cloud WAN, not a rip-and-replace of the TGW this
  project already depends on. **Known gap, stated plainly**: attaching one
  of the TGW's route tables into a segment (the piece that would make
  routes actually exchange between the TGW and Cloud WAN) is not included.
  `AWS::NetworkManager::TransitGatewayRouteTableAttachment` consistently
  failed with an opaque, detail-free `InvalidRequest` across 6+ direct API
  attempts covering every plausible cause (ASN collision -- real, found
  and fixed; a live vs. dedicated route table; registration/peering
  propagation timing; the peering's own EC2-side attachment state) with no
  further explanation available from the API. See `cloudwan_stack.py`'s
  module docstring for the full troubleshooting record.
- Attachments file themselves into the right segment automatically via a
  `segment` tag + `attachment-policies` (tag-based association) -- nothing
  wires a VPC into a segment by hand.

## Agentic AI + SageMaker

Both off by default (`ENABLE_AI=false`, `ENABLE_SAGEMAKER=false`). **Turn
these on to demo, tour, then turn back off and `cdk destroy`** -- same
posture as Cloud WAN above: Bedrock AgentCore, S3 Vectors, a Knowledge
Base, and a SageMaker Async Inference endpoint are all meaningfully
billable even sitting idle.

**`agentic_ai_stack.py`** -- a Cognito-authenticated API Gateway (HTTP API)
fronts two agent-orchestrator Lambda personas that share one Bedrock
Converse tool-use loop but have distinct IAM roles and tool access:
- `network-operator` -- every read-only MCP tool (query_kafka,
  search_memory, get_network_health, query_governance, and, when those
  layers are enabled, cloudwan_topology / detect_anomalies).
- `connectivity-planner` -- everything above, plus `propose_connectivity`,
  the one tool with any write capability at all -- and even that only ever
  opens a CodeCommit pull request describing the requested change; it
  never calls a network/IAM/compute mutation API directly. A human (or the
  pipeline, on merge) still has to act on it.

Three-tier memory: DynamoDB (working, TTL + PITR + CMK), S3 (durable,
append-only per-session JSONL transcripts), and an S3 Vectors index fronted
by a Bedrock Knowledge Base (semantic search, Titan Embed Text v2). The
same 7 MCP tool Lambdas are *also* registered as real Bedrock AgentCore
Gateway (MCP protocol) targets -- so any external MCP client, not just this
project's own orchestrator, can reach them -- alongside an AgentCore
Memory (native semantic + summary session strategies) and Workload
Identity resource.

**`sagemaker_stack.py`** -- VPC Flow Logs on app-vpc export to S3; a daily
Lambda extracts numeric features into CSV; a weekly Lambda kicks off a
SageMaker Random Cut Forest training job; a promoter Lambda (triggered by
the training job's completion event) creates each new model generation and
either creates or updates a scale-to-zero Async Inference endpoint, then
deletes the previous generation. A separate 6-hourly batch-transform job
bulk-scores accumulated data with whichever model is currently promoted; a
findings processor turns qualifying anomaly scores into DynamoDB + S3 +
SNS output, which the `detect_anomalies` MCP tool reads. Async Inference
(not Serverless) is deliberate -- Serverless can't attach to a VPC at all,
and scoring live Flow Log data from outside app-vpc's boundary would be
this project's one break from its zero-trust posture. Because there's no
`AWS::SageMaker::TrainingJob`/`...::TransformJob` CloudFormation resource
type (they're inherently one-shot API calls), the endpoint/model/config
lifecycle is entirely Lambda-managed, not CDK-managed -- a dedicated
teardown custom resource sweeps every runtime-created generation on
`cdk destroy` so nothing this pipeline creates survives a teardown.

**`auto_heal_stack.py`** -- deployed unconditionally (deterministic
remediation costs nothing when idle), last in the stack order so it can
optionally reference whichever of the above happen to be deployed. A Step
Functions state machine (Notify -> Remediate -> Notify, both notifications
to the same `governance-alerts` SNS topic `DriftRemediationStack` already
owns) runs against exactly one named resource per known failure mode: a
VPN tunnel down (SSM-restarts `ipsec` on the Libreswan instance), the
Lattice INSTANCE-type target host failing its EC2 status check (reboots
it), or the agent-orchestrator Lambdas erroring repeatedly (alert-only --
a Bedrock throttling quota increase needs a support ticket, not something
infrastructure can fix). Also extends the `lattice-lab` CloudWatch
dashboard with AI/SageMaker/API Gateway/DynamoDB widgets.

**Known gaps, stated plainly:**
- The Bedrock AgentCore `Runtime` resources (one per persona) point at a
  stub S3 code asset -- AgentCore Runtime's real invocation contract is an
  always-on HTTP server, distinct from the Lambda handler contract the
  actual working demo path (API Gateway -> agent-orchestrator Lambda)
  uses. Provisioned to demonstrate the construct end-to-end, not as a
  second working entry point.
- The RCF batch-transform output's exact JSON-lines shape
  (`{"score": <float>}` per record) is this project's best-verified-from-
  AWS-docs understanding, not yet confirmed against a live transform job's
  actual output -- `rcf_findings_processor/handler.py` documents this.
- The SageMaker endpoint's first-ever deploy has no trained model to serve
  until the first weekly training run completes and gets promoted -- a
  bootstrapping gap inherent to any from-scratch MLOps pipeline, not
  something CDK can route around.
- **No VPC interface endpoints for bedrock-runtime/bedrock-agentcore.**
  Both consistently failed with "private-dns-enabled cannot be set because
  there is already a conflicting DNS domain" -- but only when CloudFormation
  created them as part of `AgenticAiStack`; 6 consecutive identical
  failures across clean-state retries (no leftover VPC endpoints or
  Route53 private hosted zones between attempts), while isolated direct
  `aws ec2 create-vpc-endpoint` calls for the exact same service/VPC
  succeeded every time. Same "reproducible, opaque, no further AWS-side
  diagnostic detail available" shape as the Cloud WAN TGW-attachment gap
  above. The agent-orchestrator/MCP-tool Lambdas that call Bedrock reach it
  via the existing NAT-via-inspection path instead -- still TLS-encrypted,
  just not fully VPC-private for these 2 specific services.

## Observability

One CloudWatch dashboard (`lattice-lab`, `ObservabilityStack`'s `DashboardUrl`
output), one section per architecture layer that has anything meaningful to
graph -- 33 widgets total when every optional layer (`ENABLE_AI`/
`ENABLE_SAGEMAKER`/`ENABLE_CLOUDWAN`) is on. `ObservabilityStack` owns the
sections for unconditionally-deployed layers (Inspection, Lattice, ThreeTier,
Network, PrivateLink, Governance, Drift Remediation) and deploys *after*
`GovernanceStack`/`DriftRemediationStack` now (it used to deploy right after
`LatticeStack`) so it can graph both directly. The optional layers'
sections (Agentic AI, SageMaker, Cloud WAN) are added by `auto_heal_stack.py`
instead, via the same `dashboard.add_widgets(...)` cross-stack construct
mutation already established for the AI/SageMaker sections -- `auto_heal_
stack.py` is deployed last, after every optional layer that might or might
not exist by the time it runs.

Every metric namespace/dimension below was verified (not guessed) before
being wired up -- either already confirmed live earlier in this project
(`AWS/VPN` `TunnelState`+`VpnId`) or via a dedicated research pass for this
expansion:

- **Network**: VPN tunnel state; Transit Gateway traffic + packet drops
  (`AWS/TransitGateway`); the managed `AppNatGateway`'s real `AWS/NATGateway`
  metrics; the *self-managed EC2* inspection NAT instance's `AWS/EC2`
  metrics instead (confirmed: `AWS/NATGateway` does NOT apply to a
  self-managed EC2 NAT instance, only the managed NAT Gateway resource type).
- **PrivateLink**: provider NLB health + traffic (`AWS/NetworkELB` -- same
  ARN-suffix dimension extraction as GWLB/ALB, confirmed identical).
- **Governance**: CloudTrail has no native "event volume over time" metric
  (a `LogQueryWidget` against the trail's log group instead); Config/
  Security Hub/GuardDuty have no native "findings/non-compliant-resources
  over time" metric at all (confirmed via research) -- `governance_stack.py`
  now runs a `governance-metrics-publisher` Lambda every 15 minutes that
  polls all three and `PutMetricData`s into a custom `LatticeLab/Governance`
  namespace, which the dashboard graphs like any other metric.
- **Drift Remediation**: `drift-remediator` Lambda invocations/errors +
  `governance-alerts` SNS publish volume.
- **Agentic AI**: agent-orchestrator + MCP tool Lambda invocations/errors,
  working-memory DynamoDB, API Gateway request/4xx/5xx, Cognito sign-in
  activity, and a link to AgentCore's own GenAI Observability console
  (its per-resource CloudWatch dimensions aren't pinned down in public docs
  yet, so this links out rather than guessing a `dimensions_map` that could
  silently render an empty graph).
- **SageMaker**: pipeline Lambda invocations, anomaly-findings DynamoDB
  writes, and the RCF Async Inference endpoint's real queue-depth/latency/
  error metrics (confirmed via research which `AWS/SageMaker` metrics
  actually publish for *Async* endpoints specifically -- `Invocations`/
  `InvocationsPerInstance` are explicitly NOT published for async, unlike
  real-time endpoints).
- **Cloud WAN**: Core Network traffic + packet drops per edge location
  (`AWS/NetworkManager`, dimension key is `CoreNetwork` not `CoreNetworkId`).

## Verify (per layer)

**Network / VPN** -- from an SSM session on `ThreeTierStack`'s
`LatticeInstanceTargetHost` (app-vpc's one remaining EC2 instance; the real
app tier runs on Fargate, see that stack's module docstring), confirm the
VPN path to the simulated on-prem broker:
```bash
aws ssm start-session --target <LatticeInstanceTargetHostId> --profile deloitte
nc -vz <BrokerPrivateIp> 9092
```
Give the libreswan tunnel a few minutes to come up first; check
`sudo ipsec status` / `sudo journalctl -u ipsec -f` on `LibreswanInstanceId` if
it doesn't connect immediately -- hand-rolled IPsec config correctness can only
be confirmed after a real deploy (see Known gaps).

**Inspection (GWLB + firewall fleet)** -- check target health in the
`lattice-lab` CloudWatch dashboard (`ObservabilityStack`'s `DashboardUrl`
output), or:
```bash
aws elbv2 describe-target-health --target-group-arn <FirewallTargetGroupArn> --profile deloitte
```
Expect 2 healthy targets per AZ (4 total) once boot (gwlbtun build + first
`pl-sync` run) completes -- allow the full 10-minute ASG health-check grace
period before expecting healthy status.

**Three-tier app** -- `curl <WebDistributionUrl>` for the static placeholder
frontend; `curl <WebDistributionUrl>/api/health` for the app tier via the
CloudFront VPC origin.

**PrivateLink** -- from an SSM session inside app-vpc:
```bash
curl http://<InterfaceEndpointDnsName>:8080/
```
No route to provider-vpc's CIDR exists anywhere in app-vpc -- that's what
makes this one-way and overlapping-CIDR-safe (see `privatelink_stack.py`'s
module docstring).

**VPC Lattice (zero-trust auth)**:
```bash
python3 stacks/assets/lattice/test-sigv4.py <ServiceDnsName>
```
Must run from inside app-vpc (e.g. via SSM) -- the auth policies condition on
`aws:SourceVpc`, so even a correctly-signed request from outside app-vpc is
denied by design. Expect 403 without SigV4, 200 with it.

**Architecture diagram** -- `open <DiagramUrl>` (`DiagramStack` output). To
update the diagram after changing the architecture: edit
`diagram-site/generate_diagram.py`, re-run it, then redeploy `DiagramStack`.
The page now has five diagrams: the original inspection-flow one, a
governance/drift-remediation flow (including the auto-heal loop), the
multi-region Cloud WAN layer, the Agentic AI layer, and the SageMaker
anomaly-detection layer -- the last four are all present regardless of
which feature flags are on (the diagram documents the whole architecture,
not just what's currently deployed).

**Security + Governance** -- confirm the recording plane is actually
recording:
```bash
aws cloudtrail get-trail-status --name lattice-lab-trail --profile deloitte
aws configservice describe-configuration-recorder-status --profile deloitte
aws securityhub get-enabled-standards --profile deloitte
```
To test the drift-remediation flow end to end, manually create something
throwaway outside the pipeline (e.g. `aws ec2 create-security-group
--group-name manual-test --description test --vpc-id <AppVpcId> --profile
deloitte`) and confirm two emails arrive at `REMEDIATION_EMAIL` (detection,
then remediation result) and the security group is gone (or, with
`DRY_RUN=true`, that it's still there and the email says so).

**Multi-Region Cloud WAN** (only if `ENABLE_CLOUDWAN=true`) -- from
`Region2Stack`'s `TestInstanceId` (SSM session, no NAT needed):
```bash
aws ssm start-session --target <TestInstanceId> --region us-east-2 --profile deloitte
# reachable -- both attachments are in Workload, shared with nothing:
nc -vz <AppVpcPrivateIp> 8080   # should FAIL -- Workload is isolated
# now attach a resource into SkyTransit/FastTrack instead and re-test to see it succeed
```

**Agentic AI** (only if `ENABLE_AI=true`) -- create a user in the Cognito
User Pool (`CognitoUserPoolId` output), obtain a JWT, then:
```bash
curl -X POST https://<ApiEndpoint>/network-operator \
  -H "Authorization: Bearer <jwt>" -H "Content-Type: application/json" \
  -d '{"prompt": "what is the current network health?"}'
```

**SageMaker anomaly detection** (only if `ENABLE_SAGEMAKER=true`) -- the
pipeline is fully scheduled (daily preprocess, weekly train, 6-hourly
batch-score), so there's nothing to invoke by hand for a first look; check
progress via:
```bash
aws sagemaker list-training-jobs --name-contains rcf-flowlog-training --profile deloitte
aws sagemaker list-endpoints --name-contains rcf-flowlog --profile deloitte
```
To force an end-to-end run without waiting for the schedule, invoke
`rcf-flowlog-flow-log-preprocessor` then `rcf-flowlog-rcf-trainer` directly
(`aws lambda invoke --function-name ... --profile deloitte`) and watch for
`rcf-flowlog-rcf-model-promoter`'s CloudWatch Logs once training completes.

## Teardown

```bash
cdk destroy --all --profile deloitte
```

This must leave **zero** billable resources -- every bucket, log group, and
resource is `RemovalPolicy.DESTROY` (buckets also `auto_delete_objects=True`).
After it completes:

- If `PipelineStack` was ever deployed, also delete its CodePipeline artifact
  S3 bucket by hand (CDK Pipelines doesn't auto-delete it, by design, to avoid
  losing build history) and any CodeBuild log groups it created.
- Eyeball **Billing -> Bills** for: EC2 (instances + EBS), NAT Gateway
  (charges per-hour even mid-teardown), Elastic IPs (charged if unattached --
  confirm the libreswan/NAT-instance EIPs actually released), Secrets Manager
  (30-day pending-deletion window still bills), CloudWatch Logs storage.
- If `ENABLE_CLOUDWAN` was ever `true`: this is the layer most worth
  destroying promptly rather than leaving running (see its cost warning
  above). `cdk destroy --all` tears down `Region2Stack` (us-east-2) and
  `CloudWanStack` (us-east-1) along with everything else; if a destroy ever
  fails partway through on the TGW peering/attachment resources
  specifically, delete the `CfnTransitGatewayRouteTableAttachment` first,
  then the peering, then retry -- Cloud WAN won't delete a peering that
  still has an active route table attachment.
- `SecurityStack`'s 5 CMKs go to `PendingDeletion` (7-day AWS-enforced
  minimum), not deleted immediately -- expected, not a stuck resource.
- If `ENABLE_AI` or `ENABLE_SAGEMAKER` was ever `true`: same "worth
  destroying promptly" advice as Cloud WAN. `SageMakerStack`'s SageMaker
  Async Inference endpoint/model/endpoint-config are Lambda-managed, not
  CDK-managed (see "Agentic AI + SageMaker" above) -- `cdk destroy` still
  cleans them up via that stack's teardown custom resource, but confirm
  with `aws sagemaker list-endpoints --name-contains rcf-flowlog --profile
  deloitte` afterward that nothing named `rcf-flowlog-*` is left running.

## Known gaps (stated plainly, not fixed here)

- **No golden AMI.** The firewall fleet builds `gwlbtun` from source and
  installs Suricata at every boot (`apt-get`-based), not from a Packer-baked
  image -- slow first boot (the 10-minute ASG grace period reflects this), not
  incorrect. `docs/inspection-architecture-reference.md` Section 5.4 documents
  the golden-AMI approach as the day-2 optimization this project doesn't build.
- **CloudWatch Agent isn't installed on the firewall fleet.** `ObservabilityStack`
  creates the `/inspection-vpc/suricata-eve` and `/inspection-vpc/nft-pl` log
  groups with the right names/retention, but nothing ships logs into them yet
  -- `inspection_stack.py`'s bootstrap only grants the IAM permission
  (`CloudWatchAgentServerPolicy`), it doesn't configure the agent.
- **TLS_PASSTHROUGH's backend doesn't terminate TLS.** `LatticeStack`'s
  `TlsPassthroughService` is structurally complete, but the app-tier instances
  it targets only listen on plain HTTP/8080 -- add a TLS listener on the
  target (e.g. nginx + the same self-signed cert `lattice_stack.py` already
  generates) before treating this path as end-to-end verified.
- **Self-signed cert / label-only custom domain.** `LatticeStack`'s HTTPS
  listener uses a locally-generated self-signed certificate imported via
  `acm:ImportCertificate` (no real domain to validate a public ACM cert
  against, and ACM Private CA costs ~$400/month just to exist -- see the
  file's module docstring). `custom_domain_name` is a label
  (`lattice-lab.internal.example`) that won't resolve unless you point real
  DNS at it.
- **MTU and live IPsec/GENEVE behavior** can only be confirmed after an actual
  deploy -- `cdk synth` proves the CloudFormation is structurally correct, not
  that hand-rolled IPsec cipher/DH-group negotiation succeeds on the first try.

## VPC Lattice feature checklist (SPEC.md Section 5)

All in `stacks/lattice_stack.py`, grouped with `# ---- L4x ----` comments
matching the headings below.

**L4a -- service network + first service**
- `CfnServiceNetwork` (`auth_type=AWS_IAM`) -- line 77
- `CfnService` (HTTP) -- line 81
- `CfnTargetGroup` type `INSTANCE`, health-checked -- line 83 (targets `ThreeTierStack`'s `LatticeInstanceTargetHost` -- see that stack's module docstring for why the real app tier, on Fargate, can't fill this role itself)
- `CfnListener` (HTTP/80, default forward action = the "default rule") -- line 99
- `CfnServiceNetworkServiceAssociation` -- line 130
- `CfnServiceNetworkVpcAssociation` (app-vpc, with the SG below) -- line 135

**L4b -- all target-group types + rich rules**
- `CfnTargetGroup` type `IP` -- line 145 (same dedicated host as L4a, addressed by IP)
- `CfnTargetGroup` type `LAMBDA` (+ inline `lambda_.Function`) -- line 179
- `CfnTargetGroup` type `ALB` (fronts `ThreeTierStack`'s ALB listener, backed by its Fargate service) -- line 187
- Path-based rule (`/v1/*`) -- line 198
- Header-based rule (`x-canary: true`) -- line 222
- Weighted 90/10 canary rule -- line 248
- AZ affinity: **not implemented** -- no such CloudFormation property exists on `CfnTargetGroup` as of this build (checked directly against the installed CDK L1), not an oversight

**L4c -- HTTPS + custom domain + ACM + TLS passthrough**
- HTTPS listener (443) + self-signed cert import + `custom_domain_name` -- line 295 (cert import helper at the bottom of the file)
- Second service, `TLS_PASSTHROUGH` listener, service association -- lines 317/328/342

**L4d -- auth policies (zero-trust) + SG-on-association + managed prefix list**
- Service-network `CfnAuthPolicy` -- line 370
- Service-level `CfnAuthPolicy` -- line 384
- SG on the VPC association (created line 123, associated line 135) referencing the `com.amazonaws.<region>.vpc-lattice` managed prefix list, looked up via `AwsCustomResource` -- line 352
- SigV4 test script -- `stacks/assets/lattice/test-sigv4.py`

**L4e -- resource gateway + resource configuration + service-network endpoint (hybrid)**
- `CfnResourceGateway` -- line 408
- `CfnResourceConfiguration` type `SINGLE`, pointed at the on-prem broker -- line 415
- `CfnServiceNetworkResourceAssociation` -- line 426
- Service-network VPC endpoint (`ec2.CfnVPCEndpoint`, type `ServiceNetwork`), in provider-vpc -- line 444

**L4f -- observability + second Lambda-backed service + optional RAM share**
- Access logs: service-network -> CloudWatch Logs -- line 476
- Access logs: service -> S3 -- line 481
- Second LAMBDA target group + service + listener + association, demonstrating a service network fanning out to multiple independent services -- lines 492/493/507/521. Originally intended to also demo `ip_address_type=IPV6`, but VPC Lattice's API rejects that property outright on LAMBDA-type target groups (confirmed live); it's only valid on IP-type target groups, which would need real IPv6-enabled VPC subnets, out of scope for this lab.
- AWS RAM share, guarded behind `config.ENABLE_RAM_SHARE` -- line 530 (else-branch prints the grant that would be created)
