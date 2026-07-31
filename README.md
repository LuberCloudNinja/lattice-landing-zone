# lattice-landing-zone

A hands-on lab mirroring an enterprise "Lattice-first" hybrid landing zone, built
entirely in AWS CDK (Python) -- see [`SPEC.md`](SPEC.md) for the full build spec
and [`docs/inspection-architecture-reference.md`](docs/inspection-architecture-reference.md)
for the detailed GWLB/firewall/diagram design this project implements.

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
cdk bootstrap aws://458798438816/us-east-1 --profile deloitte
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
`NetworkStack -> InspectionStack -> ThreeTierStack -> PrivateLinkStack ->
LatticeStack -> ObservabilityStack -> ResourceGroupsStack -> DiagramStack`
(`KafkaStack` stays off; see Feature flags below).

`cdk deploy` will show a full plan and prompt for approval before touching
AWS -- review resource counts and any `IAM Statement Changes` there before
approving, same review you'd want even though this README exists.

## Feature flags (`config.py`)

| Flag | Default | Effect |
|---|---|---|
| `MULTI_AZ` | `false` | Flips non-HA tiers (onprem-vpc/provider-vpc AZ count, ThreeTierStack/PrivateLinkStack's Fargate desired task count) from single-AZ/single-task to 2. Does **not** affect app-vpc, which is fixed at >=2 AZs regardless (originally an RDS DBSubnetGroup requirement, kept after that stack's DynamoDB swap -- see network_stack.py), or the inspection VPC, which is always 2 AZs (`config.INSPECTION_AZ_COUNT`) with 2 firewall appliances per AZ (`config.FIREWALL_APPLIANCES_PER_AZ`) regardless -- that's a fixed HA requirement, not a cost/HA trade-off. |
| `ENABLE_KAFKA` | `false` | `KafkaStack` isn't instantiated at all until this is `true` -- deploy everything else first, per SPEC.md's "deploy LAST" instruction. |
| `ENABLE_RAM_SHARE` / `SECOND_ACCOUNT_ID` | off | Set `SECOND_ACCOUNT_ID` to actually create the AWS RAM cross-account share of the Lattice service network; otherwise `LatticeStack` prints what *would* be created (`RamShareNotCreated` output). |

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
