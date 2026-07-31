# org-governance -- management-account guardrails

A **separate** CDK app from the rest of this repo, deployed by hand into an
AWS Organizations **management account** -- not wired into `LandingZoneStage`
or the member-account CDK Pipeline. Organizations/Control Tower resources
only exist in a management account; the member app (everything else in this
repo) has no Organizations API access at all, and mixing the two would mean
the member pipeline's execution role would need Organizations-admin
permissions it should never hold.

This is the preventive/org-wide counterpart to the member app's
`security_stack.py` / `governance_stack.py` / `drift_remediation_stack.py`:
those detect and react to problems inside one account; this stops entire
categories of action from ever being possible, account-wide, via Service
Control Policies.

## Prerequisites (must exist before deploying)

1. **An AWS Organization**, with Control Tower's initial landing-zone setup
   already run once via the Control Tower console/API -- this is what
   actually creates the org root, plus the log-archive and audit accounts
   referenced below. `control_tower_stack.py`'s `CfnLandingZone` codifies
   that already-existing landing zone's configuration for repeatable
   updates/drift-detection; it does not bootstrap one from scratch.
2. The org root id (`aws organizations list-roots`), the log-archive
   account id, and the audit account id -- passed as environment variables
   (`ORG_ROOT_ID`, `LOG_ARCHIVE_ACCOUNT_ID`, `AUDIT_ACCOUNT_ID`; see
   `config.py`). Defaults are syntactically-valid placeholders so
   `cdk synth` works out of the box for review, but deploying against the
   defaults will fail (or worse, target the wrong thing) -- always supply
   real values.

## What this deploys

- **`org_stack.py`** -- four OUs, direct children of the org root: Security,
  Infrastructure, Workloads-Prod, Workloads-NonProd.
- **`scp_stack.py`** -- six SCPs. Five baseline guardrails (don't disable
  CloudTrail/Config/GuardDuty/SecurityHub, no root-user actions, stay in the
  approved regions, IMDSv2-only, no public S3) attach to all four OUs. The
  sixth, "pipeline-only," attaches only to the two Workloads OUs and denies
  mutating actions unless the caller is the CDK bootstrap's per-account
  `cdk-hnb659fds-cfn-exec-role-*` (the same principal pattern the member
  app's `drift_remediation_stack.py` excludes from its detective rule) or
  carries a `BreakGlass=true` principal tag -- the preventive twin of that
  stack's detective Lambda.
- **`control_tower_stack.py`** -- an illustrative `CfnLandingZone` manifest
  plus a representative set of `CfnEnabledControl`s on the Security OU.

## Delegated-admin pattern

Not created by any resource in this app -- delegation is an
Organizations-level trust relationship set up once via
`aws organizations register-delegated-administrator`, not a CloudFormation
resource type available in this account/region:

- Security Hub, Config, and GuardDuty are administered from the **audit**
  account -- it becomes each service's org-wide "administrator account,"
  seeing aggregated findings across every member account, while the
  management account stays out of day-to-day security operations.
- A single CloudTrail **organization trail** (created once, from the
  management account) ships every member account's events to a
  **centralized S3 bucket in the log-archive account** -- log integrity
  doesn't depend on any single member account (including a compromised one)
  being unable to tamper with or delete its own trail.

## Deploying

```bash
cd org-governance
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ORG_ROOT_ID=r-xxxx LOG_ARCHIVE_ACCOUNT_ID=111111111111 AUDIT_ACCOUNT_ID=222222222222 \
    cdk deploy --all --profile <management-account-profile>
```
