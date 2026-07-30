#!/usr/bin/env bash
#
# pl-sync.sh -- policy-as-code sync agent for the nftables zone-based policy
# ("the PL"). See docs/inspection-architecture-reference.md Section 5.5 for
# the full design; this is the on-instance half of the S3 (payload) + SSM
# Parameter Store (version pointer) policy pipeline described there.
#
# Run by pl-sync.service, triggered by pl-sync.timer (on boot + every 5
# minutes thereafter) -- see the sibling pl-sync.service/pl-sync.timer files
# in this directory.
#
# Hard requirement (reference doc Section 5.5, point 4 / "never apply a
# broken PL"): nft -c -f (syntax-check only, no apply) must succeed before
# the real nft -f apply runs. On ANY failure at ANY step below, the
# last-known-good ruleset already loaded in the kernel is left completely
# untouched -- this script only ever replaces the running ruleset after a
# clean validation pass, and only ever advances the local version marker
# after a successful apply.
#
# Prerequisite baked into the golden AMI (Packer layer, Section 5.4) --
# NOT installed by this script: the AWS CLI (v2) must already be on PATH,
# and the instance profile must carry read-only IAM permissions scoped to
# exactly the SSM parameter path and S3 prefix below (least privilege, per
# Section 5.5's IAM note; CloudTrail then captures every fetch).

set -u
set -o pipefail

# ---------------------------------------------------------------------------
# Configuration -- CDK-wired values
#
# inspection_stack.py must inject the real values for the two variables
# below at instance launch time (e.g. templated into cloud-init/user-data,
# or written to an EnvironmentFile such as /etc/default/pl-sync that
# pl-sync.service loads -- see that unit's commented-out EnvironmentFile=
# line). Do NOT ship the placeholder defaults below to an actual deploy;
# they exist only so this script is syntactically complete and greppable
# for what inspection_stack.py still needs to wire up.
# ---------------------------------------------------------------------------

# SSM Parameter Store parameter holding the current PL version pointer (an
# S3 object version-id or semantic tag -- reference doc Section 5.5, step 4:
# "Write the new S3 object version-id (or a semantic tag) into an SSM
# Parameter Store parameter -- the cheap, pollable pointer, not the
# payload.")
# NEEDS-CDK-WIRING: replace with the real parameter name inspection_stack.py
# creates (e.g. via ssm.StringParameter in CDK).
PL_SSM_PARAM="${PL_SSM_PARAM:-/NEEDS-CDK-WIRING/lattice-lab/inspection/pl-version}"

# S3 bucket + prefix holding the versioned policy.yaml / zone-definitions.yaml
# artifacts (reference doc Section 5.5, step 3: "Publish raw YAML + rendered
# .nft to a versioned S3 bucket").
# NEEDS-CDK-WIRING: replace with the real bucket name / prefix
# inspection_stack.py provisions.
S3_BUCKET="${S3_BUCKET:-NEEDS-CDK-WIRING-pl-artifacts-bucket}"
S3_PREFIX="${S3_PREFIX:-NEEDS-CDK-WIRING/policy}"

# ---------------------------------------------------------------------------
# Fixed local paths / dependencies
# ---------------------------------------------------------------------------
VERSION_FILE="/var/lib/pl-sync/version"
WORK_DIR="/var/lib/pl-sync/staging"
POLICY_YAML="$WORK_DIR/policy.yaml"
ZONE_DEFS_YAML="$WORK_DIR/zone-definitions.yaml"
RENDERED_NFT="$WORK_DIR/pl.nft"
LIVE_NFT="/etc/nftables/pl.nft"

# generate_nft.py is a sibling task's deliverable, baked into the golden AMI
# at this fixed path -- reference doc Section 5.2's policy-as-code pipeline
# and Section 5.5's pl-sync.service logic both name this exact path.
GENERATE_NFT="/opt/inspection/generate_nft.py"

LOG_TAG="pl-sync"

mkdir -p "$WORK_DIR" "$(dirname "$VERSION_FILE")" "$(dirname "$LIVE_NFT")"

log() {
  # $1 = tag (e.g. PL-APPLIED, PL-SYNC-ERROR), rest = free-text message.
  # journald/syslog picks this up; CloudWatch Agent ships it onward
  # (reference doc Section 5.3 "Logging" -- same pattern as the nftables
  # `log prefix "PL-..."` lines the PL itself emits, so PL-APPLIED /
  # PL-SYNC-ERROR sit in the same greppable log-prefix family).
  local tag="$1"
  shift
  logger -t "$LOG_TAG" "${tag}: $*"
}

fail() {
  # The single exit path for "do not apply" -- last-known-good ruleset and
  # local version marker are both left exactly as they were.
  log "PL-SYNC-ERROR" "$*"
  exit 1
}

# ---------------------------------------------------------------------------
# 1. Compare local version marker against the SSM parameter
# ---------------------------------------------------------------------------
ssm_err_file="$(mktemp)"
remote_version="$(aws ssm get-parameter \
  --name "$PL_SSM_PARAM" \
  --query 'Parameter.Value' \
  --output text 2>"$ssm_err_file")"
ssm_rc=$?

if [ "$ssm_rc" -ne 0 ] || [ -z "$remote_version" ] || [ "$remote_version" = "None" ]; then
  err="$(cat "$ssm_err_file" 2>/dev/null)"
  rm -f "$ssm_err_file"
  fail "could not read SSM parameter '$PL_SSM_PARAM' (rc=$ssm_rc): $err"
fi
rm -f "$ssm_err_file"

local_version=""
if [ -f "$VERSION_FILE" ]; then
  local_version="$(cat "$VERSION_FILE")"
fi

if [ "$remote_version" = "$local_version" ]; then
  # No change since last successful sync -- nothing to do. Not an error.
  exit 0
fi

log "PL-SYNC-START" "version change detected: local='${local_version:-<none>}' remote='$remote_version'"

# ---------------------------------------------------------------------------
# 2. Pull policy.yaml + zone-definitions.yaml from S3
# ---------------------------------------------------------------------------
s3_err_file="$(mktemp)"

if ! aws s3 cp "s3://${S3_BUCKET}/${S3_PREFIX}/policy.yaml" "$POLICY_YAML" >"$s3_err_file" 2>&1; then
  err="$(cat "$s3_err_file")"; rm -f "$s3_err_file"
  fail "failed to pull policy.yaml from s3://${S3_BUCKET}/${S3_PREFIX}/ for version=$remote_version: $err"
fi

if ! aws s3 cp "s3://${S3_BUCKET}/${S3_PREFIX}/zone-definitions.yaml" "$ZONE_DEFS_YAML" >"$s3_err_file" 2>&1; then
  err="$(cat "$s3_err_file")"; rm -f "$s3_err_file"
  fail "failed to pull zone-definitions.yaml from s3://${S3_BUCKET}/${S3_PREFIX}/ for version=$remote_version: $err"
fi

rm -f "$s3_err_file"

# ---------------------------------------------------------------------------
# 3. Render the nftables ruleset via generate_nft.py (sibling task)
#
# NOTE: the exact CLI flags below (--policy/--zone-definitions/--output) are
# this script's assumption about generate_nft.py's interface, based on the
# policy.yaml + zone-definitions.yaml -> pl.nft pipeline described in
# reference doc Section 5.2. The fixed path (/opt/inspection/generate_nft.py)
# is the one contract point the reference doc actually specifies; confirm
# these flag names against the sibling task's real argparse signature once
# that file exists and adjust this invocation if it differs.
# ---------------------------------------------------------------------------
if [ ! -f "$GENERATE_NFT" ]; then
  fail "generate_nft.py not found at $GENERATE_NFT -- cannot render PL for version=$remote_version"
fi

render_err_file="$(mktemp)"
if ! python3 "$GENERATE_NFT" \
      --policy "$POLICY_YAML" \
      --zone-definitions "$ZONE_DEFS_YAML" \
      --output "$RENDERED_NFT" >"$render_err_file" 2>&1; then
  err="$(cat "$render_err_file")"; rm -f "$render_err_file"
  fail "generate_nft.py failed to render $RENDERED_NFT for version=$remote_version: $err"
fi
rm -f "$render_err_file"

if [ ! -s "$RENDERED_NFT" ]; then
  fail "generate_nft.py reported success but produced an empty $RENDERED_NFT for version=$remote_version"
fi

# ---------------------------------------------------------------------------
# 4. Validate: nft -c -f (syntax check only -- does NOT touch the running
#    ruleset). This is the gate that must never be skipped.
# ---------------------------------------------------------------------------
validate_err_file="$(mktemp)"
if ! nft -c -f "$RENDERED_NFT" >"$validate_err_file" 2>&1; then
  err="$(cat "$validate_err_file")"; rm -f "$validate_err_file"
  fail "nft -c -f validation FAILED for version=$remote_version -- last-known-good ruleset left running UNTOUCHED: $err"
fi
rm -f "$validate_err_file"

# ---------------------------------------------------------------------------
# 5. Apply atomically -- only ever reached after a clean validation pass.
#
# nft -f is itself a single netlink transaction (reference doc Section 5.2:
# "atomic ruleset replace (nft -f, one netlink transaction) -- no
# flush-then-reload race window"), so there is no partial-apply state to
# worry about.
#
# IMPORTANT ordering: apply against the STAGED file ($RENDERED_NFT, under
# $WORK_DIR) first, and only copy it into $LIVE_NFT (/etc/nftables/pl.nft)
# AFTER a confirmed-successful apply. An earlier draft of this script copied
# the new ruleset into $LIVE_NFT before calling `nft -f`, which meant that if
# `nft -f` failed for any reason after a clean `nft -c -f`, the on-disk
# $LIVE_NFT would already reflect the new (never-actually-applied) ruleset
# while the kernel kept running the old one -- a disk/kernel mismatch that
# would silently ship the unapplied ruleset on the next reboot. Applying
# from the staged file first means $LIVE_NFT on disk always matches what's
# actually loaded in the kernel, satisfying "last-known-good ruleset left
# completely untouched" for both the running ruleset AND its on-disk copy.
# ---------------------------------------------------------------------------
apply_err_file="$(mktemp)"
if ! nft -f "$RENDERED_NFT" >"$apply_err_file" 2>&1; then
  err="$(cat "$apply_err_file")"; rm -f "$apply_err_file"
  # Should be unreachable given step 4 already validated this exact file
  # content, but never assume. Treat it exactly like a validation failure:
  # do NOT touch $LIVE_NFT and do NOT update the version marker. The version
  # marker only advances on a confirmed-applied ruleset, so the next timer
  # tick will retry from scratch rather than silently drifting.
  fail "nft -f apply FAILED for version=$remote_version despite passing nft -c -f -- investigate immediately (kernel ruleset and $LIVE_NFT left untouched): $err"
fi
rm -f "$apply_err_file"

# Persist the now-confirmed-applied ruleset to its live path (atomic rename
# within the same filesystem) so it survives reboot / re-read by
# nftables.service. If this staging step itself fails, the kernel is already
# running the new ruleset but $LIVE_NFT and the version marker are not
# updated -- safe and retry-idempotent: the next tick sees local_version !=
# remote_version and redoes the full pull/render/validate/apply/persist
# cycle rather than drifting silently.
cp "$RENDERED_NFT" "${LIVE_NFT}.new" || fail "applied version=$remote_version to the kernel but failed to stage $RENDERED_NFT to ${LIVE_NFT}.new -- version marker NOT updated, will retry next tick"
mv -f "${LIVE_NFT}.new" "$LIVE_NFT" || fail "applied version=$remote_version to the kernel but failed to move ${LIVE_NFT}.new into place -- version marker NOT updated, will retry next tick"

echo "$remote_version" > "${VERSION_FILE}.new"
mv -f "${VERSION_FILE}.new" "$VERSION_FILE"

log "PL-APPLIED" "version=$remote_version"
exit 0
