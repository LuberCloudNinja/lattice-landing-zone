# install-suricata.sh
#
# ASSUMPTION: target OS is Ubuntu 24.04 LTS ("noble") -- per
# docs/inspection-architecture-reference.md Section 5.4, the AMI choice for
# this firewall stack. This file is a *bash fragment*, not a standalone
# script: no shebang, no `set -e`. It is meant to be concatenated into a
# larger root cloud-init / user-data script (or a Packer golden-AMI
# provisioning script, per Section 5.4's Packer-vs-cloud-init split) that
# owns its own shell options. Do not add `set -e`/`set -u` here -- that
# would change error-handling semantics for every fragment concatenated
# after this one. Steps below check their own exit status explicitly where
# a silent failure would matter.
#
# Companion files (same directory): pl-sync.sh / pl-sync.service /
# pl-sync.timer render and apply the actual nftables zone policy ("the PL")
# that feeds Suricata via NFQUEUE -- this script only installs and
# configures the Suricata engine itself.

echo "=== install-suricata.sh: begin ==="

# ---------------------------------------------------------------------------
# 0. Package source: OISF's own PPA (ppa:oisf/suricata-stable)
#
# Reference doc Section 5.4 picks Ubuntu 24.04 specifically because "official
# apt package + OISF's own repo track Ubuntu primarily" -- i.e. both paths
# exist on this OS. This script takes OISF's PPA over plain Ubuntu
# universe: Suricata is the inline IPS engine sitting directly in the data
# path here, and engine/rule-parser CVEs land in OISF's own repo well ahead
# of Ubuntu's SRU cadence for a universe package. Set USE_OISF_PPA=false to
# fall back to plain Ubuntu apt (e.g. if this VPC's egress ever blocks
# Launchpad/PPA hosts and only the Ubuntu archive mirror is reachable).
# Uses a default-if-unset assignment (not a plain `=true`) so a value
# exported earlier in the concatenated cloud-init/Packer script -- or
# passed in from the environment -- isn't silently clobbered here.
USE_OISF_PPA="${USE_OISF_PPA:-true}"

DEBIAN_FRONTEND=noninteractive apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common ca-certificates

if [ "$USE_OISF_PPA" = "true" ]; then
  add-apt-repository -y ppa:oisf/suricata-stable
  if [ $? -ne 0 ]; then
    echo "install-suricata.sh: WARNING -- add-apt-repository for OISF PPA failed, falling back to Ubuntu universe suricata package" >&2
  fi
  DEBIAN_FRONTEND=noninteractive apt-get update -y
fi

# python3-yaml: used below to patch suricata.yaml programmatically (safer
# than sed against a ~1000-line YAML file). nftables: dependency of the
# sibling pl-sync.sh/generate_nft.py pipeline (this script does not write
# nft rules itself, just makes sure the binary/config dir this instance's
# PL depends on is present).
DEBIAN_FRONTEND=noninteractive apt-get install -y suricata python3-yaml nftables jq

if ! command -v suricata >/dev/null 2>&1; then
  echo "install-suricata.sh: ERROR -- suricata binary not found after apt-get install, aborting rest of this fragment" >&2
  echo "=== install-suricata.sh: end (FAILED) ==="
  return 1 2>/dev/null || true
fi

# The Debian/Ubuntu suricata package's postinst enables+starts the service
# against its packaged default (af-packet) config. Stop it now; we
# reconfigure below and re-enable/start at the end of this fragment.
systemctl stop suricata 2>/dev/null || true

SURICATA_YAML="/etc/suricata/suricata.yaml"

# ---------------------------------------------------------------------------
# 1. HOME_NET -- union of this project's VPC CIDRs (config.py VPC_CIDRS)
#
# Reference doc Section 5.3: "HOME_NET = the union of all inspected VPC
# CIDRs -- drive it from the same zone-definitions.yaml used by the nftables
# generator (single source of truth)." zone-definitions.yaml is a sibling
# task's deliverable (pulled by pl-sync.sh from S3 at runtime) and isn't
# necessarily present yet when this AMI-bake-time fragment runs, so for now
# HOME_NET is hardcoded here directly from config.py's VPC_CIDRS -- the same
# four CIDRs, so this is consistent with that eventual source of truth, not
# a competing one. KEEP IN SYNC with config.py if those CIDRs ever change;
# ideally a future revision of this fragment (or a boot-time step in
# pl-sync.sh) regenerates this block from zone-definitions.yaml directly
# instead of duplicating the values.
HOME_NET_CIDRS="10.0.0.0/16,10.1.0.0/16,10.2.0.0/16,10.100.0.0/16"
# ^ config.py VPC_CIDRS: inspection, app, provider, onprem (in that order)

# ---------------------------------------------------------------------------
# 2. Patch suricata.yaml: HOME_NET, runmode, rule paths, nfq:, cpu-affinity
#
# Done via a small python3/PyYAML rewrite rather than sed: safer for a
# multi-key edit that includes nested structures (nfq:, threading.
# cpu-affinity), and idempotent if this fragment ever re-runs. Trade-off:
# PyYAML's safe_dump re-serializes the *entire* file, so upstream inline
# comments in the packaged suricata.yaml are lost. Acceptable here -- this
# script (and this repo) is the source of truth for the golden AMI's
# Suricata config, not hand-edits to the on-disk yaml.
# ---------------------------------------------------------------------------
HOME_NET_CIDRS="$HOME_NET_CIDRS" SURICATA_YAML="$SURICATA_YAML" python3 - <<'PYEOF'
import os
import sys

import yaml

path = os.environ["SURICATA_YAML"]
home_net = os.environ["HOME_NET_CIDRS"]

with open(path) as f:
    cfg = yaml.safe_load(f)

# --- HOME_NET: this project's four VPC CIDRs (see comment block above) ---
cfg.setdefault("vars", {}).setdefault("address-groups", {})
cfg["vars"]["address-groups"]["HOME_NET"] = "[{}]".format(home_net)

# --- runmode: 'workers' is the recommended runmode for NFQUEUE capture ---
cfg["runmode"] = "workers"

# --- rule paths: explicit, matching what `suricata-update` (Section 3
#     below) writes, rather than relying on the package default matching
#     across OISF-PPA vs Ubuntu-universe builds. ---
cfg["default-rule-path"] = "/var/lib/suricata/rules"
cfg["rule-files"] = ["suricata.rules"]

# --- nfq: block -- fail-open / batchcount / route-queue, per reference doc
#     Section 5.3. This is a SECOND, independent fail-open control at the
#     libnetfilter_queue layer (kernel-queue-overflow/backpressure),
#     distinct from nftables' own `bypass` verdict (set in the PL's
#     `queue num 0-3 options fanout,bypass` clause, rendered by the sibling
#     generate_nft.py). Both default to fail-open, consistent with this
#     project's documented availability-vs-security trade-off (Section
#     5.3): "default to bypass on, paired with a hard CloudWatch alarm on
#     suricata.service health so uninspected windows are seconds, not
#     silent and indefinite." ---
cfg["nfq"] = {
    # mode: accept (default) issues one direct NF_ACCEPT/NF_DROP verdict per
    # packet -- matches this design, since the PL's fanout clause queues
    # each inspected packet exactly once and expects a single verdict back,
    # not a mark-and-repeat requeue loop.
    "mode": "accept",
    # route-queue only takes effect under mode: repeat (mark-and-requeue to
    # a second queue for a second pass). Not exercised by this design's
    # single-queue-set fanout, but the reference doc calls it out
    # explicitly as one of the three nfq: keys to pin deliberately rather
    # than leave at Suricata's own default -- set to the first fanout queue
    # as a safe no-op value.
    "route-queue": 0,
    # libnetfilter_queue read batch size: packets pulled off the queue per
    # syscall before Suricata processes them. 20 is Suricata's own
    # documented default -- pinned explicitly here rather than left
    # implicit, per the reference doc.
    "batchcount": 20,
    # fail-open: true -- on queue backpressure/overflow at the
    # libnetfilter_queue layer, the kernel ACCEPTs the packet instead of
    # dropping it (NFQA_CFG_F_FAIL_OPEN). See comment block above.
    "fail-open": True,
}

# --- threading.cpu-affinity: pin Suricata's worker threads to the same
#     cores nftables' `fanout` hashes across (queues 0-3 here), and
#     explicitly NOT the cores gwlbtun pins via --tunaffinity/--udpaffinity
#     (reference doc Section 5.1/5.3). ---
cfg["threading"] = cfg.get("threading") or {}
cfg["threading"]["cpu-affinity"] = [
    {"management-cpu-set": {"cpu": [0]}},
    {"receive-cpu-set": {"cpu": [0]}},
    {
        "worker-cpu-set": {
            # NEEDS-COORDINATION (gwlbtun cores): gwlbtun's
            # --tunaffinity/--udpaffinity pins are owned by a sibling task
            # (the systemd unit / launch script that invokes gwlbtun, not
            # this file) and are not visible here. The 4 cores below
            # (0-3) match nftables' `queue num 0-3` fanout so Suricata's
            # worker threads line up 1:1 with the queues; confirm at build
            # time that gwlbtun is pinned to a DISJOINT core range (e.g.
            # 4-7 on the reference doc's baseline c6in.2xlarge / 8 vCPU
            # instance type, Section 5.4) so the two data-plane processes
            # never contend for the same physical cores. Update this list
            # (and the sibling gwlbtun pin) together if the instance type
            # or core count ever changes.
            "cpu": [0, 1, 2, 3],
            "mode": "exclusive",
            "prio": {"default": "high"},
        }
    },
]

with open(path, "w") as f:
    yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)

print("install-suricata.sh: suricata.yaml patched (HOME_NET, runmode, rule paths, nfq:, threading.cpu-affinity)")
PYEOF

if [ $? -ne 0 ]; then
  echo "install-suricata.sh: ERROR -- suricata.yaml patch step failed, config may be left in a partial state -- investigate before relying on this instance" >&2
fi

# ---------------------------------------------------------------------------
# 3. Emerging Threats Open ruleset via suricata-update
# ---------------------------------------------------------------------------
suricata-update update-sources
suricata-update enable-source et/open
if ! suricata-update; then
  echo "install-suricata.sh: WARNING -- suricata-update failed to pull ET Open rules; Suricata will start with whatever rules (if any) are already on disk under /var/lib/suricata/rules" >&2
fi

# ---------------------------------------------------------------------------
# 4. NFQUEUE mode across queue numbers 0-3
#
# Matches this project's nftables PL clause `queue num 0-3 options
# fanout,bypass` (reference doc Section 5.2/5.3, rendered by the sibling
# generate_nft.py into /etc/nftables/pl.nft and applied by pl-sync.sh).
# `fanout` distributes packets across queues by CPU id, so Suricata must be
# listening on all four queue numbers via repeated `-q` flags -- there is no
# single "queue range" CLI flag, each queue number needs its own `-q N`.
#
# The packaged suricata.service unit defaults to af-packet capture
# (ExecStart ... --af-packet). Override it with a drop-in rather than
# editing the packaged unit file directly, so `apt upgrade` of the suricata
# package doesn't silently revert this.
# ---------------------------------------------------------------------------
mkdir -p /etc/systemd/system/suricata.service.d
cat > /etc/systemd/system/suricata.service.d/override.conf <<EOF
[Service]
# Clear the packaged unit's ExecStart (af-packet, forking/-D + pidfile) and
# replace with an explicit NFQUEUE invocation across queue numbers 0-3.
# Type=simple + foreground (no -D) avoids depending on the packaged unit's
# fork/pidfile assumptions, which vary across the OISF-PPA vs
# Ubuntu-universe package builds this fragment might run against.
Type=simple
ExecStart=
ExecStart=/usr/bin/suricata -c ${SURICATA_YAML} -q 0 -q 1 -q 2 -q 3
Restart=on-failure
RestartSec=5s
EOF

systemctl daemon-reload

# Config self-test before enabling the service for real (`-T` = test config
# and exit, does not start capture). Non-fatal here by design (this
# fragment must not `exit` and kill the rest of a concatenated cloud-init
# script) -- but loud, since a failed test config means the service below
# will crash-loop.
if ! suricata -T -c "$SURICATA_YAML" -v; then
  echo "install-suricata.sh: ERROR -- 'suricata -T' config test FAILED; suricata.service will be enabled anyway but is expected to crash-loop until this is fixed. Check $SURICATA_YAML." >&2
fi

# ---------------------------------------------------------------------------
# 5. Enable as a systemd service
# ---------------------------------------------------------------------------
systemctl enable suricata
systemctl restart suricata

sleep 2
if systemctl is-active --quiet suricata; then
  echo "install-suricata.sh: suricata.service is active"
else
  echo "install-suricata.sh: ERROR -- suricata.service is NOT active after enable/restart; check 'systemctl status suricata' and 'journalctl -u suricata'" >&2
fi

echo "=== install-suricata.sh: end ==="
