# install-gwlbtun.sh
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
# a silent failure would matter, matching the sibling install-suricata.sh.
#
# Companion file (same directory): install-suricata.sh installs the inline
# IPS engine that gwi-<X>-injected, PL-matched traffic gets queued to. This
# script only builds/installs/configures gwlbtun itself (reference doc
# Section 5.1) -- GENEVE decap/encap and the gwi-<X>/gwo-<X> tunnel
# interface lifecycle, nothing else.
#
# =============================================================================
# WHAT'S CONFIRMED FROM THE REAL UPSTREAM REPO VS. REASONED FROM THE REFERENCE
# DOC -- read this before editing anything below
# =============================================================================
# Confirmed directly from aws-samples/aws-gateway-load-balancer-tunnel-handler
# (README.md, CMakeLists.txt, main.cpp, TunInterface.cpp, GeneveHandler.cpp,
# example-scripts/*, as of the pinned commit below, 2026-07-29):
#   - It is C++ (C++17), NOT Go. Built with CMake (>=3.13) + make, linked
#     against pthreads and Boost >= 1.83 (headers only). There are no
#     GitHub Releases and no git tags -- no prebuilt binary is published
#     anywhere, and there is no versioned release scheme to pin against.
#   - Real CLI flags (from `gwlbtun -h`, reproduced verbatim in the
#     upstream README): -c FILE / -r FILE (create/destroy hook scripts),
#     -p PORT (TCP health-check listener, no default -- omit it and no
#     listener starts), -j (JSON health output), -s (simple status-code-only
#     health output), -t TIME (tunnel idle timeout, default 0 = never),
#     -d (debug logging), --udpthreads/--udpaffinity/--tunthreads/
#     --tunaffinity (thread count or explicit core-pinning for the UDP
#     receiver and per-tunnel processor threads, independently).
#   - gwi-<X>/gwo-<X> creation: on the first packet from a new GWLB
#     endpoint, GeneveHandlerENI's constructor (GeneveHandler.cpp) builds
#     TWO TunInterface objects (gwi-<X> ingress, gwo-<X> egress; <X> = the
#     ENI ID base-60-encoded to fit the 15-char Linux ifname limit), and
#     ONLY THEN invokes the -c create-hook script. TunInterface's
#     constructor (TunInterface.cpp) already marks the device IFF_UP and
#     sets its MTU via SIOCSIFMTU to a hardcoded `#define GWLB_MTU 8500`
#     (GeneveHandler.cpp) BEFORE the hook script ever runs -- there is no
#     runtime flag to change this default. Practical effect on this
#     script's hook below: `ip link set ... up` is a defensive no-op (the
#     daemon already did it); the MTU step is a deliberate OVERRIDE of the
#     daemon's built-in 8500 default, only meaningful if this environment's
#     real inner-packet MTU differs from 8500 (reference doc Section 5.4
#     says exactly this: confirm the real figure, don't hardcode).
#   - CAP_NET_ADMIN requirement: README states this explicitly ("the
#     application requires CAP_NET_ADMIN capability to create the tunnel
#     interfaces along with the example helper scripts").
#   - example-scripts/*.sh confirm the hook-invocation contract: a single
#     script is commonly reused for both -c and -r, dispatching on $1
#     (CREATE|DESTROY); $2/$3/$4 are the ingress iface, egress iface, and
#     ENI ID in hex. All of upstream's own examples set
#     `rp_filter=0` only on $2 (the ingress/gwi-<X> interface) -- matched
#     below.
#
# Reasoned / adapted (NOT stated by the upstream repo, which documents
# build/run only for Amazon Linux 2 / AL2023, never Ubuntu):
#   - Every apt package name, the CMakeLists.txt patch (see step 1c below),
#     the systemd unit (upstream ships none -- see step 2), running as an
#     unprivileged user at all (AWS's own demo CloudFormation template in
#     example-scripts/example-topology-one-arm.template runs gwlbtun as
#     root via a systemd unit with zero hardening and `-p 80`; this lab
#     deliberately does not follow that demo, per reference doc Section
#     5.1's explicit instruction: "grant via systemd
#     AmbientCapabilities=CAP_NET_ADMIN rather than running the whole
#     process as root").
#   - The exact health-check port (8080 -- see the constant below), the
#     inner tunnel MTU value, and the --udpaffinity/--tunaffinity core
#     ranges are this lab's own choices, grounded in but not dictated by
#     Section 5.4's instance-type guidance.
# =============================================================================

echo "=== install-gwlbtun.sh: begin ==="

# =============================================================================
# HEALTH-CHECK PORT -- FIXED CONSTANT, READ BY inspection_stack.py
# =============================================================================
# gwlbtun's built-in `-p PORT` health-check listener is bound to this port
# below (step 3). inspection_stack.py's CDK code MUST point the GWLB target
# group's health check at this exact port, protocol TCP (per the existing
# TODO comment already in inspection_stack.py: "Health check: TCP against
# gwlbtun's own health-check port, never GENEVE" -- a plain TCP-connect
# check is sufficient and correct; it does not need to parse gwlbtun's
# HTTP-formatted body). If GWLBTUN_HEALTHCHECK_PORT changes, update
# inspection_stack.py's target group in the same change.
#
# 8080 is not an upstream default -- the real -p flag has no default at
# all (see the confirmed-facts block above). 8080 is chosen here as a
# conventional unprivileged alt-HTTP port, which also happens to be a hard
# requirement: the systemd unit below (step 2) runs gwlbtun as an
# unprivileged user with no CAP_NET_BIND_SERVICE, so it cannot bind
# anything under 1024 regardless.
GWLBTUN_HEALTHCHECK_PORT=8080
# =============================================================================

# -----------------------------------------------------------------------------
# Other constants
# -----------------------------------------------------------------------------

# Upstream publishes no tags/releases (confirmed above) -- pin by commit SHA
# for a reproducible golden-AMI build. This is the HEAD of main as of
# 2026-07-29; bump deliberately (not silently) on a periodic AMI refresh,
# per reference doc Section 5.4's Packer/CI cadence.
GWLBTUN_GIT_REF="5e12604c85e99c8511d5b84604ac49647ffdc395"
GWLBTUN_REPO_URL="https://github.com/aws-samples/aws-gateway-load-balancer-tunnel-handler.git"
GWLBTUN_BUILD_DIR="/usr/local/src/gwlbtun"
GWLBTUN_INSTALL_DIR="/opt/gwlbtun"
GWLBTUN_BIN="/usr/local/bin/gwlbtun"
GWLBTUN_USER="gwlbtun"
GWLBTUN_GROUP="gwlbtun"

# Inner tunnel MTU applied to gwi-<X>/gwo-<X> in the create hook (step 4),
# overriding gwlbtun's own built-in 8500 default (confirmed above).
#
# Reference doc Section 5.4 is explicit that this value is
# environment-specific and must be CONFIRMED against the actual spoke VPCs
# and the Transit Gateway (hard 8500-byte MTU ceiling on TGW attachments
# regardless of what this VPC could otherwise carry), not hardcoded. 1500
# is set here as the conservative default -- the doc's own fallback ("if
# spokes have not enabled jumbo frames, use 1500 instead") -- specifically
# because defaulting to 8500 and being wrong fails SILENTLY and
# INTERMITTENTLY (GWLB/GENEVE drops oversized frames rather than
# fragmenting them), whereas defaulting to 1500 and being wrong only costs
# throughput, not correctness. Override this value once the real spoke MTU
# is confirmed; do not skip the doc's required end-to-end large-packet test
# even after doing so.
TUNNEL_MTU=1500

# Primary/GWLB-facing ENI jumbo-frame MTU (reference doc Section 5.4:
# "Nitro instances and this VPC support it, and it gives headroom to
# receive GENEVE-encapsulated frames without fragmentation").
PRIMARY_ENI_MTU=9001

# --udpaffinity/--tunaffinity core pinning (step 3), coordinated with the
# sibling install-suricata.sh, which pins Suricata's NFQUEUE worker threads
# to cores 0-3 and explicitly documents cores 4-7 as reserved for gwlbtun
# on the reference doc's baseline c6in.2xlarge (8 vCPU) instance type
# (Section 5.4). This is this lab's own choice (see reasoned/adapted list
# above), not an upstream default -- update both files together if the
# instance type or vCPU count ever changes, and once config.py grows a
# FIREWALL_INSTANCE_TYPE constant, confirm it still matches c6in.2xlarge.
GWLBTUN_UDP_AFFINITY="4,5"
GWLBTUN_TUN_AFFINITY="6,7"

# -----------------------------------------------------------------------------
# 1. Install build prerequisites and build/install gwlbtun from source
# -----------------------------------------------------------------------------

echo "install-gwlbtun.sh: [1/5] installing build prerequisites"
DEBIAN_FRONTEND=noninteractive apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential cmake git ca-certificates libboost-dev iproute2 procps

if ! command -v cmake >/dev/null 2>&1 || ! command -v g++ >/dev/null 2>&1; then
  echo "install-gwlbtun.sh: ERROR -- cmake or g++ missing after apt-get install, aborting rest of this fragment" >&2
  echo "=== install-gwlbtun.sh: end (FAILED) ==="
  return 1 2>/dev/null || true
fi

echo "install-gwlbtun.sh: [1/5] cloning gwlbtun @ ${GWLBTUN_GIT_REF}"
rm -rf "$GWLBTUN_BUILD_DIR"
git clone "$GWLBTUN_REPO_URL" "$GWLBTUN_BUILD_DIR"
( cd "$GWLBTUN_BUILD_DIR" && git checkout --quiet "$GWLBTUN_GIT_REF" )
if [ $? -ne 0 ]; then
  echo "install-gwlbtun.sh: ERROR -- git clone/checkout of gwlbtun failed, aborting rest of this fragment" >&2
  echo "=== install-gwlbtun.sh: end (FAILED) ==="
  return 1 2>/dev/null || true
fi

# 1c. Patch CMakeLists.txt: upstream unconditionally does
#   set(Boost_INCLUDE_DIR /home/ec2-user/boost)
#   find_package(Boost 1.83 REQUIRED)
# -- a path that only exists on the author's own AL2/AL2023 build host
# where Boost was hand-downloaded (see the README's Boost section, also
# confirmed above). Because that `set()` is a plain (non-CACHE) variable,
# it shadows any `-DBoost_INCLUDE_DIR=...` passed on the cmake command
# line, so passing a command-line override does NOT work against this
# CMakeLists.txt as written -- confirmed by reading it, not assumed. The
# only reliable fix is to remove that line so find_package(Boost) falls
# through to its normal system search path, which is sufficient here:
# Ubuntu 24.04's libboost-dev package resolves to boost-defaults 1.83,
# meeting upstream's ">= 1.83" requirement exactly, so no manual Boost
# download (as the AL2/AL2023 instructions require) is needed on this OS.
sed -i '/set(Boost_INCLUDE_DIR/d' "${GWLBTUN_BUILD_DIR}/CMakeLists.txt"

echo "install-gwlbtun.sh: [1/5] building gwlbtun (cmake + make)"
( cd "$GWLBTUN_BUILD_DIR" && cmake . && make -j"$(nproc)" )
if [ $? -ne 0 ] || [ ! -x "${GWLBTUN_BUILD_DIR}/gwlbtun" ]; then
  echo "install-gwlbtun.sh: ERROR -- gwlbtun build failed or binary missing, aborting rest of this fragment" >&2
  echo "=== install-gwlbtun.sh: end (FAILED) ==="
  return 1 2>/dev/null || true
fi

install -m 0755 -o root -g root "${GWLBTUN_BUILD_DIR}/gwlbtun" "$GWLBTUN_BIN"
mkdir -p "${GWLBTUN_INSTALL_DIR}/hooks"

# tun/tap kernel module: /dev/net/tun (which TunInterface.cpp opens to
# create gwi-<X>/gwo-<X>) needs it loaded. Usually already loaded on the
# EC2/Nitro kernel, but not documented as guaranteed -- load it explicitly
# and persist across reboots rather than assume.
modprobe tun 2>/dev/null || true
echo "tun" > /etc/modules-load.d/gwlbtun-tun.conf

# -----------------------------------------------------------------------------
# 2. Unprivileged service account
# -----------------------------------------------------------------------------

echo "install-gwlbtun.sh: [2/5] creating unprivileged ${GWLBTUN_USER} service account"
if ! id "$GWLBTUN_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$GWLBTUN_USER"
fi

# -----------------------------------------------------------------------------
# 3/4. Create/destroy hook script -- interface up (defensive), MTU override,
#      rp_filter=0 on the ingress tunnel interface
# -----------------------------------------------------------------------------
#
# One script serves both -c and -r (mirrors every example in upstream's
# example-scripts/, confirmed above), dispatching on $1. Arguments, in
# order, per the confirmed README contract: $1=CREATE|DESTROY,
# $2=ingress iface (gwi-<X>), $3=egress iface (gwo-<X>), $4=ENI ID (hex).
#
# This script runs as $GWLBTUN_USER (system() from inside gwlbtun, which
# itself runs unprivileged -- step 5), NOT as root. `ip`/`sysctl` here
# succeed only because of the AmbientCapabilities=CAP_NET_ADMIN grant on
# gwlbtun.service (step 5): ambient capabilities are specifically designed
# to survive fork/exec into unprivileged children like this hook script, as
# long as the child binary carries no setuid bit or file capabilities of
# its own (true for stock /bin/ip and /usr/sbin/sysctl on Ubuntu 24.04).

echo "install-gwlbtun.sh: [3/5] writing create/destroy hook script"
cat > "${GWLBTUN_INSTALL_DIR}/hooks/tunnel-hook.sh" <<HOOKEOF
#!/bin/bash
# Generated by install-gwlbtun.sh -- do not hand-edit; re-run the bootstrap
# fragment to regenerate. Invoked by gwlbtun via both -c and -r (dispatches
# on \$1). See install-gwlbtun.sh for the full grounding/comments.

MODE="\$1"
INGRESS_IF="\$2"
EGRESS_IF="\$3"
ENI_ID="\$4"
TUNNEL_MTU=${TUNNEL_MTU}

logger -t gwlbtun-hook "mode=\${MODE} ingress=\${INGRESS_IF} egress=\${EGRESS_IF} eni=\${ENI_ID}"

if [ "\$MODE" = "CREATE" ]; then
  # Defensive: gwlbtun's TunInterface constructor already brings gwi-<X>/
  # gwo-<X> up before this hook ever runs (confirmed from TunInterface.cpp
  # -- see the header comment block above). This is a harmless idempotent
  # safeguard, not a required step.
  ip link set dev "\$INGRESS_IF" up
  ip link set dev "\$EGRESS_IF" up

  # Override gwlbtun's built-in MTU=8500 default (GeneveHandler.cpp's
  # GWLB_MTU #define, confirmed above) to match this environment's actual
  # inner-packet MTU (reference doc Section 5.4). See the TUNNEL_MTU
  # comment in install-gwlbtun.sh for why 1500 is the safe default here.
  ip link set dev "\$INGRESS_IF" mtu "\$TUNNEL_MTU"
  ip link set dev "\$EGRESS_IF" mtu "\$TUNNEL_MTU"

  # Reverse-path-filter: disable on the ingress tunnel interface.
  #
  # WHY: gwi-<X> only ever sees GWLB's decapsulated traffic arriving from
  # one direction as far as the KERNEL's forwarding/routing view is
  # concerned -- there is no "return" traffic that would ever naturally
  # arrive back in via gwi-<X> the way strict rp_filter (mode 1) expects
  # for a normal routed interface. Post-inspection return traffic for the
  # SAME flow goes back out via the separate gwo-<X> interface instead
  # (that's the entire reason gwlbtun creates a paired interface rather
  # than reusing one -- reference doc Section 5.1). Strict rp_filter
  # evaluates each packet's source address against the local routing table
  # as if this were a conventional multi-homed router; on this tunnel
  # interface that check reads as asymmetric/spoofed-looking traffic even
  # though it is completely legitimate, and the kernel silently drops
  # (blackholes) it -- no log, no counter most operators check by default,
  # just packets that vanish. Disabling it here (rather than globally) is
  # the same fix upstream's own example-scripts/create-route.sh and
  # create-nat.sh use (confirmed above: \`sysctl
  # net.ipv4.conf.\$2.rp_filter=0\`), scoped to only the dynamically-created
  # tunnel interface.
  sysctl -w "net.ipv4.conf.\${INGRESS_IF}.rp_filter=0"
elif [ "\$MODE" = "DESTROY" ]; then
  # Interfaces are already being torn down by gwlbtun itself; nothing to
  # clean up here. Kept as an explicit branch (rather than falling through)
  # so a future addition to this hook doesn't accidentally run
  # CREATE-only logic on DESTROY.
  :
else
  logger -t gwlbtun-hook "unexpected mode '\${MODE}', ignoring"
fi
HOOKEOF
chmod 0755 "${GWLBTUN_INSTALL_DIR}/hooks/tunnel-hook.sh"
chown root:root "${GWLBTUN_INSTALL_DIR}/hooks/tunnel-hook.sh"

# -----------------------------------------------------------------------------
# 5. systemd service -- unprivileged user, AmbientCapabilities=CAP_NET_ADMIN,
#    health-check listener on GWLBTUN_HEALTHCHECK_PORT
# -----------------------------------------------------------------------------
#
# Upstream ships no systemd unit at all -- this is this lab's own file (see
# reasoned/adapted list above). Deliberately does NOT follow AWS's own demo
# CloudFormation template (example-topology-one-arm.template, confirmed
# above), which runs gwlbtun as root with no capability hardening; this
# unit follows reference doc Section 5.1's explicit instruction instead.

echo "install-gwlbtun.sh: [4/5] writing gwlbtun.service"
cat > /etc/systemd/system/gwlbtun.service <<UNITEOF
[Unit]
Description=AWS Gateway Load Balancer GENEVE tunnel handler (gwlbtun)
Documentation=https://github.com/aws-samples/aws-gateway-load-balancer-tunnel-handler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${GWLBTUN_USER}
Group=${GWLBTUN_GROUP}

# CAP_NET_ADMIN is required to create/configure the gwi-<X>/gwo-<X> tun
# interfaces (confirmed above: upstream README states this requirement
# explicitly). AmbientCapabilities grants it to this unprivileged user
# without running the process as root, and -- important for the hook
# script above -- ambient capabilities propagate through system()'s
# fork/exec into the -c/-r hook scripts too, which is what lets
# unprivileged \`ip\`/\`sysctl\` calls in tunnel-hook.sh succeed.
#
# CAP_NET_RAW is ALSO required, separately from CAP_NET_ADMIN: reading
# GeneveHandler.cpp directly (GeneveHandlerENI's constructor) shows it
# opens a raw socket -- \`socket(AF_INET, SOCK_RAW, IPPROTO_RAW)\` -- used
# by sendUdp() on every re-encapsulated return packet sent back to GWLB.
# The Linux kernel gates SOCK_RAW socket creation on CAP_NET_RAW
# specifically, not CAP_NET_ADMIN (see \`man 7 capabilities\`); with only
# CAP_NET_ADMIN granted, that socket() call fails, the GeneveHandlerENI
# constructor throws, and gwlbtun never successfully stands up a tunnel --
# it would still pass its own -p health check (that listener doesn't
# depend on this socket) while silently failing to pass any real traffic.
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
NoNewPrivileges=true

ExecStart=${GWLBTUN_BIN} \\
  -c ${GWLBTUN_INSTALL_DIR}/hooks/tunnel-hook.sh \\
  -r ${GWLBTUN_INSTALL_DIR}/hooks/tunnel-hook.sh \\
  -p ${GWLBTUN_HEALTHCHECK_PORT} \\
  --udpaffinity ${GWLBTUN_UDP_AFFINITY} \\
  --tunaffinity ${GWLBTUN_TUN_AFFINITY}

Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable gwlbtun
systemctl restart gwlbtun

sleep 2
if systemctl is-active --quiet gwlbtun; then
  echo "install-gwlbtun.sh: gwlbtun.service is active"
else
  echo "install-gwlbtun.sh: ERROR -- gwlbtun.service is NOT active after enable/restart; check 'systemctl status gwlbtun' and 'journalctl -u gwlbtun'" >&2
fi

# -----------------------------------------------------------------------------
# 6. IP forwarding, persisted via /etc/sysctl.d
# -----------------------------------------------------------------------------
#
# Decapsulated packets land on gwi-<X> and must be routed (through
# nftables/Suricata, reference doc Section 5.1) back out gwo-<X> by this
# host's own IP stack -- that only happens with forwarding enabled.
# net.ipv6.conf.all.forwarding included per reference doc Section 5.4
# ("...and net.ipv6.conf.all.forwarding=1 if dual-stack") -- harmless if
# this deployment is IPv4-only.

echo "install-gwlbtun.sh: [5/5] enabling IP forwarding"
cat > /etc/sysctl.d/99-gwlbtun-forwarding.conf <<SYSCTLEOF
# Managed by install-gwlbtun.sh -- reference doc Section 5.1/5.4.
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
SYSCTLEOF
sysctl -p /etc/sysctl.d/99-gwlbtun-forwarding.conf

# =============================================================================
# Primary-ENI MTU -- jumbo frames (reference doc Section 5.4)
# =============================================================================
#
# Separate, clearly-labeled block per the design spec: this is the
# GWLB-facing primary ENI's own MTU, NOT the gwi-<X>/gwo-<X> tunnel
# interface MTU set in the create hook above -- the two are deliberately
# different values (9001 here vs. TUNNEL_MTU=1500 above) and must not be
# confused. Set to 9001 (jumbo) so the primary ENI has headroom to receive
# GENEVE-encapsulated frames without fragmentation -- GWLB/GENEVE does NOT
# fragment oversized frames, it drops them, so an undersized primary-ENI
# MTU fails silently and intermittently rather than cleanly (reference doc
# Section 5.4, verbatim rationale).
#
# Primary interface name is detected the same way upstream's own
# example-scripts/create-nat.sh detects its egress interface (confirmed
# above: \`ip route show default | cut -f 5 -d ' '\`) rather than hardcoded,
# since predictable-network-interface naming (ensN vs eth0) varies by AMI
# kernel/driver version.
#
# `ip link set mtu` alone does not survive reboot on Ubuntu's
# netplan/cloud-init-managed networking -- not addressed by the reference
# doc, which only specifies the target MTU value, not the Ubuntu-specific
# persistence mechanism. A netplan drop-in is added here so the setting
# survives reboot/relaunch, matching this lab's Ubuntu 24.04 baseline.

echo "install-gwlbtun.sh: setting primary ENI to jumbo-frame MTU ${PRIMARY_ENI_MTU}"
PRIMARY_IF="$(ip route show default | cut -f 5 -d ' ')"

if [ -z "$PRIMARY_IF" ]; then
  echo "install-gwlbtun.sh: ERROR -- could not detect primary interface via 'ip route show default'; skipping jumbo-frame MTU step" >&2
else
  ip link set dev "$PRIMARY_IF" mtu "$PRIMARY_ENI_MTU"

  mkdir -p /etc/netplan
  cat > /etc/netplan/90-gwlbtun-primary-mtu.yaml <<NETPLANEOF
# Managed by install-gwlbtun.sh -- reference doc Section 5.4 (jumbo frames
# on the primary/GWLB-facing ENI). Persists the MTU set imperatively above
# across reboot; netplan's own generator/apply merges this with the
# cloud-init-authored network config rather than replacing it.
network:
  version: 2
  ethernets:
    ${PRIMARY_IF}:
      mtu: ${PRIMARY_ENI_MTU}
NETPLANEOF
  chmod 0600 /etc/netplan/90-gwlbtun-primary-mtu.yaml
  netplan apply 2>/dev/null || echo "install-gwlbtun.sh: WARNING -- 'netplan apply' failed; MTU is set for this boot via 'ip link set' above but the netplan drop-in may not take effect until next boot -- check 'netplan status'" >&2
fi

echo "=== install-gwlbtun.sh: end ==="
