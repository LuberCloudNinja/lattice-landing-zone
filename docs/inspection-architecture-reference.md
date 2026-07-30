# Inspection Architecture Reference — GWLB, TGW Symmetry, Linux Firewall Stack, Diagram Spec

This is supporting reference material for `inspection_stack.py`, `network_stack.py`, and `diagram_stack.py`
(see `SPEC.md`). It was researched and cross-verified independently (network design drafted, firewall
software stack drafted, diagram/hosting spec drafted, then all three checked against each other for
consistency and against AWS's published GWLB centralized-inspection reference architecture) because this is
the part of the build most prone to subtle, silent failures — asymmetric routing, missing Appliance Mode in
one of its two required places, MTU mismatches, etc.

**IaC note:** this was researched against underlying AWS resource/attribute names (e.g. Transit Gateway
attachment `appliance_mode_support`, GWLB target-group Appliance Mode) for precision, independent of the IaC
tool. This project builds in **AWS CDK (Python)**, not Terraform — map each setting below to its equivalent
CDK L1 (`Cfn*`) or L2 construct property (e.g. `CfnTransitGatewayVpcAttachment`'s `options.applianceModeSupport`,
or the GWLB target group's `targetGroupAttributes`). The AWS-level concepts, route table entries, and
correctness requirements below are identical regardless of IaC tool — only the construct API differs.

This project's own network topology (four VPCs — `onprem-vpc`, `inspection-vpc`, `app-vpc`, `provider-vpc`,
plus the Site-to-Site VPN standing in for on-prem — per SPEC.md §4) differs in shape from the simpler
spokes-only topology this reference was drafted against, but every routing/symmetry/health-check/MTU
correctness point below applies unchanged: they're properties of GWLB + TGW + Appliance Mode themselves, not
of how many spoke VPCs exist. Treat the CIDR ranges below as illustrative — use this project's actual CIDRs
(`10.0.0.0/16` inspection-vpc, `10.1.0.0/16` app-vpc, `10.2.0.0/16` provider-vpc, `10.100.0.0/16` onprem-vpc)
and its two firewall appliances per AZ (this reference assumes one per AZ in its illustrative examples — for
this project, register **both** per-AZ appliances as GWLB targets in the same per-AZ target group / ASG).

---

## 4. Network Architecture Specification

### 4.0 GWLB vs. NLB — resolved, do not re-litigate this

**Gateway Load Balancer (GWLB) is the only inspection data-path mechanism in this design.** All north-south and east-west traffic is transparently redirected through it via GWLB Endpoints (GWLBe) using GENEVE encapsulation (UDP/6081) with Appliance Mode enabled for flow symmetry.

A plain **Network Load Balancer (NLB) is not part of the inspection data path and must never appear in any route table that carries inspected traffic.** The only legitimate use for an NLB in this design is optional and out-of-band: fronting the firewall fleet's **management plane** (a config-push/orchestration endpoint, health dashboard, or log-forwarding collector) on a separate ENI/subnet never touched by inspected packets. If you build this optional management NLB, place it in a dedicated management subnet, label it clearly as management-plane-only in code comments and any diagram, and do not wire it into the TGW-attach, GWLB Endpoint, or firewall data-plane subnet route tables under any circumstances.

### 4.1 VPC Layout

**AZ count: default to 3 AZs**, expose as a config flag (`config.py`: `AZ_COUNT`, default `3`, allowed values 2 or 3, alongside the existing `MULTI_AZ` flag) so it can be overridden. 3 AZs limits blast radius on a single-AZ failure to ~33% capacity loss (vs. 50% at 2 AZs) at roughly 1.5x the cost of firewall instances/GWLB endpoints/NAT Gateways. Use 3 unless a cost constraint is stated.

**Inspection VPC CIDR:** `10.99.0.0/23` — a dedicated, non-overlapping block hosting infrastructure ENIs only, no application workloads. This is 512 total addresses; once subdivided into per-AZ /27 subnets (below), plan capacity **per subnet** (27 usable IPs per /27 after AWS's 5 reserved addresses), not against the flat /23 usable-host count — the two are not the same figure.

**Four subnet tiers per AZ:**

| Tier | Purpose | Contains |
|---|---|---|
| **Public / NAT subnet** | Edge of VPC, attached to IGW | NAT Gateway (one per AZ — centralized egress NAT, see assumption below), no firewall ENIs |
| **GWLB Endpoint subnet** | Hosts the GWLB Endpoint (GWLBe / PrivateLink interface) | GWLBe ENI(s) |
| **Firewall subnet** | Hosts firewall EC2 instances (GWLB targets) | Firewall data-plane ENI; GWLB's own LB nodes deploy here via subnet-mapping at GWLB creation |
| **TGW-attach subnet** | Hosts the Inspection VPC's Transit Gateway attachment ENI | TGW attachment ENI |

Example allocation (3 AZs, /27 per subnet, leaves headroom in the /23):

| AZ | Public/NAT | GWLB Endpoint | Firewall | TGW-attach |
|---|---|---|---|---|
| AZ-a | 10.99.0.0/27 | 10.99.0.32/27 | 10.99.0.64/27 | 10.99.0.96/27 |
| AZ-b | 10.99.0.128/27 | 10.99.0.160/27 | 10.99.0.192/27 | 10.99.0.224/27 |
| AZ-c | 10.99.1.0/27 | 10.99.1.32/27 | 10.99.1.64/27 | 10.99.1.96/27 |

Size GWLBe subnets ≥ /27 — undersized subnets can throttle GWLB endpoint ENI scaling under load. Firewall subnets need 1 IP per instance plus headroom for ASG replacement overlap.

**Confirmed working assumption (do not silently re-open, but flag at the Section 3 checkpoint if you find a concrete reason it's wrong): centralized egress NAT is co-located in the Inspection VPC's Public/NAT subnet, one NAT Gateway per AZ.** This is why the IGW is attached directly to the Inspection VPC rather than a separate egress VPC.

### 4.2 Transit Gateway Topology

**Attachments:**
- One TGW attachment per spoke VPC, subnets spanning that spoke's AZs.
- One TGW attachment for the Inspection VPC, using the TGW-attach subnet in each of the 2–3 AZs above. This must cover every AZ that has a firewall instance — a missing AZ here means that AZ has no inspection capacity reachable via TGW.
- **Enable `appliance_mode_support = "enable"` on the Inspection VPC's TGW attachment.** This is a *separate* setting from GWLB's own target-group Appliance Mode (Section 4.4) and is equally mandatory. Without it, TGW can route different packets of the *same flow* to different AZ ENIs of the Inspection VPC attachment, since TGW's per-packet AZ selection is not otherwise flow-sticky — this breaks flow symmetry **upstream of GWLB**, and GWLB's own Appliance Mode cannot fix an asymmetry that already happened at the TGW hop. Treat this at the same severity as the GWLB target-group setting; both must be true simultaneously for stateful inspection to work.

**Route tables (the "appliance VPC route table" pattern for symmetric routing):**

`TGW-RT-SPOKES` (associated with every spoke VPC attachment):

| Destination | Target | Notes |
|---|---|---|
| `0.0.0.0/0` | Inspection VPC attachment | **Static route only — do not propagate other spokes' routes into this table.** This is what forces east-west traffic through inspection; spokes structurally cannot see each other's routes. |

`TGW-RT-INSPECTION` (associated with the Inspection VPC attachment):

| Destination | Target | Notes |
|---|---|---|
| `<spoke-A-CIDR>` | Spoke A attachment | Auto-populated via route propagation |
| `<spoke-B-CIDR>` | Spoke B attachment | Same |
| ... | ... | One propagated route per spoke |

No default route needed in `TGW-RT-INSPECTION` — internet-bound routing after inspection is handled by the Inspection VPC's own subnet route tables, not by TGW.

Each spoke VPC's own local route table needs `0.0.0.0/0 → TGW attachment` so all its egress funnels to TGW.

### 4.3 Route Tables — Implementation Detail

**TGW-attach subnet RT (per AZ):**

| Destination | Target |
|---|---|
| `10.99.0.0/23` | local |
| `0.0.0.0/0` | GWLB Endpoint (same-AZ) |

**GWLB Endpoint subnet RT (per AZ):**

| Destination | Target |
|---|---|
| `10.99.0.0/23` | local |
| `<spoke-A-CIDR>`, `<spoke-B-CIDR>`, ... | TGW attachment |
| `0.0.0.0/0` | NAT Gateway (same AZ) |

This is the table that decides where a packet goes **after** the firewall returns it: specific spoke CIDRs (longest-prefix match) go back to TGW for east-west; the default route goes to the NAT Gateway for north-south egress.

**Firewall subnet RT (per AZ):**

| Destination | Target |
|---|---|
| `10.99.0.0/23` | local |
| `0.0.0.0/0` | IGW (direct) or NAT GW |

Needed only for the firewall instances' own management-plane egress (licensing/updates/log export) — not part of the inspected data path, which the firewall handles via GENEVE decap/encap, not normal IP routing.

**Public/NAT subnet RT:**

| Destination | Target |
|---|---|
| `10.99.0.0/23` | local |
| `0.0.0.0/0` | IGW |

**Ingress Route Table — IGW edge association (required, and the piece most commonly missed):**

By default, traffic arriving at the IGW routes straight to its destination ENI (e.g. the NAT Gateway) via normal per-subnet route tables — bypassing the firewall on the way back in. AWS supports associating a route table directly with the IGW itself (edge association, not a subnet association) to intercept this.

`RT-Ingress`, associated with the IGW:

| Destination | Target |
|---|---|
| `<NAT-GW-A-private-IP>/32` | GWLB Endpoint (AZ-a) |
| `<NAT-GW-B-private-IP>/32` | GWLB Endpoint (AZ-b) |
| `<NAT-GW-C-private-IP>/32` | GWLB Endpoint (AZ-c) |

Without this, inbound reply packets for outbound-initiated (north-south) sessions skip the firewall entirely on the reverse leg. **Do not omit this — verify it explicitly in your pre-`cdk deploy` review pass.**

### 4.4 GWLB Configuration

> **CORRECTION (verified against current AWS documentation while building
> `inspection_stack.py` — the original research below, from before this
> project had live web access, got this wrong):** there is **no target-group
> "Appliance Mode" boolean attribute**. GWLB target groups only expose five
> attributes: `deregistration_delay.timeout_seconds`, `stickiness.enabled`,
> `stickiness.type` (`source_ip_dest_ip` / `source_ip_dest_ip_proto`),
> `target_failover.on_deregistration`, `target_failover.on_unhealthy` — no
> cross-zone attribute either. Flow symmetry for a GWLB target group's
> **default (5-tuple) stickiness is inherent to the service**, and per AWS's
> own docs, **2-tuple/3-tuple stickiness (`stickiness.enabled=true` +
> `stickiness.type=source_ip_dest_ip[_proto]`) is explicitly NOT supported
> together with Transit Gateway Appliance Mode** — the two features are
> mutually exclusive, not complementary. The correct, compatible
> configuration is: leave the target group at its **default 5-tuple
> stickiness** (`stickiness.enabled` unset/false) and rely on TGW's
> `appliance_mode_support=enable` (Section 4.2) as the *only* Appliance Mode
> switch in this design. Every "both must be enabled" statement below and in
> the pitfall checklist is corrected by this note — treat the table row
> below as superseded.

| Setting | Value | Rationale |
|---|---|---|
| Target group protocol/port | `GENEVE` / `6081` | Mandatory — the only protocol GWLB target groups speak to targets |
| **Target type** | `instance` | Matches the one-firewall-per-AZ, ASG-managed design in Section 5; this decision is final, not vendor-dependent (see Section 5 — this is a custom Linux stack, not a commercial NGFW AMI) |
| Health check protocol/port | TCP against the **gwlbtun daemon's native health-check listener** (`-p PORT` flag, Section 5.1) | Never GENEVE — GWLB target-group health checks do not support GENEVE as the check protocol. Use gwlbtun's built-in listener rather than a hand-rolled script. |
| Health check interval / thresholds | **Explicitly override the default: interval 10s, healthy threshold 3, unhealthy threshold 3** | The AWS default interval is **30 seconds**, not 10 — 10s is a deliberate faster-detection override you are choosing, not "the default." Document it as an override. At these overridden values, failure detection is ≈30s (3 × 10s); at true defaults it would be ≈90s (3 × 30s). Get this math right wherever it's referenced (ASG grace period, HA runbook). |
| Cross-zone load balancing | **N/A — no such target-group attribute exists for GWLB** (see correction note above) | GWLB target groups don't expose a cross-zone toggle at all; each AZ's GWLB endpoint only ever forwards to that AZ's registered, healthy targets by design. |
| **Flow symmetry** | **Default 5-tuple stickiness (leave `stickiness.enabled` unset/false); rely on the TGW-attachment-level Appliance Mode in Section 4.2 as the sole switch** | See correction note above — there is no target-group-level Appliance Mode to enable, and 2-/3-tuple stickiness is explicitly incompatible with TGW Appliance Mode. |
| `target_failover.on_deregistration` / `on_unhealthy` | **`rebalance` / `rebalance`** (must match) | With 2 appliances per AZ specifically for HA, rebalancing existing flows onto the surviving healthy target on deregistration/failure is the point of having a second instance — the AWS default (`no_rebalance`) would otherwise black-hole in-flight flows until the ASG replaces the instance. |
| Security groups on firewall targets | Allow inbound UDP/6081 + the health-check port from the VPC CIDR | GWLB has no narrow, whitelistable source IPs; the real security boundary is the firewall's own nftables policy, not the SG. |

### 4.5 Traffic Flow Walkthroughs

**North-South egress (spoke → internet):**
1. Spoke A instance → `0.0.0.0/0 → TGW attachment` (spoke route table).
2. TGW: `TGW-RT-SPOKES` sends everything to the Inspection VPC attachment.
3. Lands in Inspection VPC's TGW-attach subnet (AZ-local) → `0.0.0.0/0 → GWLBe (same AZ)`.
4. GWLBe → GWLB (regional service) → GWLB GENEVE-encapsulates and forwards to the AZ-local firewall target (zonal affinity, cross-zone disabled).
5. Firewall decapsulates, inspects, allows, re-encapsulates, returns to GWLB → GWLBe.
6. GWLB Endpoint subnet RT: no spoke-CIDR match → falls to default → NAT Gateway (same AZ).
7. NAT Gateway SNATs to its EIP → IGW → internet.

**North-South return leg:**
8. Reply arrives at IGW addressed to the NAT Gateway's EIP.
9. `RT-Ingress` (IGW edge association) matches the NAT Gateway's private IP → redirects to the AZ-matched GWLBe — this is what re-injects the return packet into inspection.
10. GWLB forwards to the **same** firewall target that handled the outbound leg (guaranteed by Appliance Mode's flow-symmetric hashing at both the GWLB and TGW layers).
11. Returned via GWLBe into the GWLB Endpoint subnet; route table matches Spoke A's CIDR (more specific than default) → TGW attachment.
12. TGW: `TGW-RT-INSPECTION` has the propagated route for Spoke A → delivered to Spoke A's attachment → instance receives the reply.

**East-West (Spoke A → Spoke B):**
1. Spoke A instance → `0.0.0.0/0 → TGW attachment` (Spoke A has no direct route to Spoke B).
2. TGW: `TGW-RT-SPOKES` sends it to the Inspection VPC attachment.
3. Lands in TGW-attach subnet (AZ-a) → `0.0.0.0/0 → GWLBe (AZ-a)` → GWLB → firewall (AZ-a) → inspected, allowed, returned.
4. Returned into GWLB Endpoint subnet; route table matches Spoke B's CIDR (specific, propagated) → TGW attachment, **not** NAT/IGW.
5. TGW: `TGW-RT-INSPECTION` has the propagated route for Spoke B → delivered to Spoke B's attachment.
6. Return traffic follows the identical pattern in reverse; both legs hash to the same firewall target (Appliance Mode at both layers), so the firewall sees full bidirectional flow state.

### 4.6 HA Behavior

**AZ failure:** that AZ's TGW-attach ENI, GWLBe, and firewall instance become unreachable together. TGW attachments span multiple AZs and can route around the dead AZ's ENI. Whether the *surviving* AZs' GWLB nodes absorb the dead AZ's traffic depends on GWLB's own cross-zone setting (next point) — with it disabled, recovery depends on TGW routing an entire end-to-end path through a healthy AZ, not on GWLB shifting targets within one endpoint.

**Cross-zone trade-off (explicit, decide and document):**
- **Disabled (default):** a single unhealthy firewall instance black-holes that AZ's inspection path until the ASG replaces it and it passes health checks — budget roughly the failure-detection window (≈30s at the overridden 10s×3 settings above) plus instance launch/bootstrap time plus healthy-threshold time; low single-digit minutes with a golden AMI.
- **Enabled:** a healthy target in another AZ can cover for an unhealthy one, shrinking that window, at the cost of inter-AZ data-transfer charges/latency and reduced zonal isolation. Appliance Mode still pins each individual flow to one target for its lifetime either way.

**Firewall instance failure / ASG replacement:**
- One ASG **per AZ**, each pinned to a single subnet with `min=max=desired=1`, rather than one shared multi-AZ ASG — a single multi-AZ ASG's built-in AZ-rebalancing can transiently deviate from a strict 1-per-AZ count during scaling events; N independent single-instance ASGs guarantee the mapping deterministically and isolate blast radius.
- Use a golden AMI with the firewall stack baked in (Section 5.4) to minimize time-to-healthy; set the ASG health-check grace period to match realistic bootstrap time.

### 4.7 Pitfall Checklist (verify explicitly before proposing `cdk deploy`)

**Routing correctness**
- [ ] `TGW-RT-SPOKES` has **only** the default route to the Inspection VPC attachment — no propagation of other spokes' routes.
- [ ] `TGW-RT-INSPECTION` has propagation enabled from every spoke attachment.
- [ ] GWLB Endpoint subnet route tables have specific spoke-CIDR routes to TGW *in addition to* the default route to the NAT Gateway.
- [ ] `RT-Ingress` (IGW edge association) is in place for every AZ's NAT Gateway private IP.

**GWLB / TGW symmetry (both required, independently)**
- [ ] Inspection VPC's TGW attachment has **`appliance_mode_support = "enable"`** — the *only* Appliance Mode switch in this design (see the correction note in Section 4.4: there is no separate target-group-level attribute).
- [ ] GWLB target group is left at **default 5-tuple stickiness** (`stickiness.enabled` unset/false) — 2-/3-tuple stickiness is explicitly incompatible with TGW Appliance Mode per AWS's docs, not an alternative way to achieve symmetry.
- [ ] Health-check protocol is TCP against gwlbtun's `-p PORT` listener — never GENEVE.
- [ ] Cross-zone load balancing decision is deliberate and documented, not left at an undiscussed default.
- [ ] GWLB subnet mappings cover every AZ that has a firewall instance.
- [ ] GWLB's VPC Endpoint Service (PrivateLink) acceptance is configured (auto-accept, or explicit account acceptance) — otherwise GWLBe creation hangs in `pendingAcceptance`.

**Instance / ENI level**
- [ ] Security group on the firewall's GWLB-facing ENI allows GENEVE/6081 + the health-check port.
- [ ] **`source_dest_check` on the firewall's primary GWLB-facing ENI does NOT need to be disabled.** The outer GENEVE packet's destination is the target ENI's own IP:6081, so it is not being "forwarded" in the classic routed-appliance sense that this check blocks. If (and only if) the firewall also uses a **separate secondary ENI** for conventional IP forwarding outside the GWLB/tunnel path, *that* ENI needs `source_dest_check = false`. Do not disable it on the primary ENI by reflex — this is a common false carryover from NAT-instance-era patterns.
- [ ] MTU is verified end-to-end, not assumed (see Section 5.4 for concrete values) — GWLB/GENEVE does not fragment oversized encapsulated frames, it silently drops them, so a mismatch here shows up as intermittent large-transfer failures, not a clean error.

**Fleet management**
- [ ] One ASG per AZ (`min=max=desired=1`).
- [ ] Golden AMI / fast bootstrap to minimize the unhealthy-to-healthy window.
- [ ] Any optional management-plane NLB is on a physically separate ENI/subnet from the data plane and appears nowhere in inspected-path route tables (Section 4.0).

### 4.8 Remaining open items

1. **Cross-zone load-balancing final choice** against the client's actual RTO tolerance for a single-instance failure — default is disabled; flag if changed.
2. **Final AZ count (2 vs 3)** — default is 3; flag if changed for cost reasons.
3. **Custom domain / WAF for the diagram site and any public-facing exposure decisions** — see Section 6.5.

(NAT-gateway placement, GWLB target type, health-check port, and firewall vendor/AMI are **not** open items — they are decided in this document; do not re-litigate them mid-build.)

---

## 5. Linux Firewall Software Stack Specification

### 5.0 Concept mapping (for anyone expecting a commercial NGFW)

This is a **capability match to a Palo-Alto-style zone-based policy model**, built entirely from open-source components — not a commercial NGFW and not a PANOS clone. Never label any box, diagram caption, comment, or log prefix with a vendor name (see Section 6.4). The real stack:

| Concept | What's actually running |
|---|---|
| Zone-based security policy rulebase | **nftables** — named zone chains, rendered from a YAML policy-as-code file |
| Zones (trust/untrust/dmz) | **nftables named sets/maps** classifying by source/destination CIDR — **not** by physical interface (Section 5.2) |
| App-ID / Threat Prevention / IPS | **Suricata** in inline IPS mode via NFQUEUE, with Emerging Threats Open |
| GENEVE tunnel data plane to GWLB | **aws-gateway-load-balancer-tunnel-handler** (`gwlbtun`) |
| Central policy push | S3 (versioned artifact store) + SSM Parameter Store (version pointer) + systemd-timer pull, push-assisted by SSM Run Command |
| Traffic / threat logs | nftables `log` output + Suricata `eve.json` → CloudWatch Logs → optional Firehose → S3 |

### 5.1 GENEVE Tunnel Termination — `aws-gateway-load-balancer-tunnel-handler` (gwlbtun)

Use AWS's own open-source (C++) reference daemon: https://github.com/aws-samples/aws-gateway-load-balancer-tunnel-handler. Do not hand-roll a GENEVE decap with raw `ip link add type geneve` — GWLB's per-flow cookie handling (below) is non-trivial and this tool already solves it correctly.

**Mechanics:**
- Binds UDP/6081, receives GENEVE-encapsulated frames from GWLB.
- On the first packet from a given GWLB Endpoint, dynamically creates a paired interface set: `gwi-<X>` (ingress) and `gwo-<X>` (egress), one pair per GWLB endpoint the appliance is attached to (typically one pair per AZ's GWLBe in this multi-AZ design).
- Decapsulated inner packets are injected onto `gwi-<X>`; the appliance's own IP stack (`ip_forward=1`) routes them through nftables/Suricata; the post-inspection packet is emitted back out on `gwo-<X>`.
- The daemon tracks GWLB's per-flow **flow cookie** in the GENEVE header. When re-encapsulating the return packet, it must echo that cookie and target it back to the exact source GWLBE IP the packet arrived from — this is the mechanism that makes both the GWLB-side and TGW-side Appliance Mode settings (Section 4.2/4.4) actually deliver symmetric flows to a stateful engine. Do **not** build with the `NO_RETURN_TRAFFIC` compile flag — that variant removes `gwo-*` and cookie tracking for one-armed/sniffer deployments only, not applicable here.
- `-c FILE` / `-r FILE` hook scripts fire on tunnel create/destroy. Use these to: bring the link up, **set MTU (Section 5.4)**, and set `sysctl net.ipv4.conf.<gwi-X>.rp_filter=0` (or `2`, loose mode) — required because locally-observed return-path asymmetry on the tunnel interface would otherwise trip strict reverse-path filtering and silently blackhole traffic.
- Built-in health-check listener: `-p PORT` (add `-j` for JSON, `-s` for simple status code). **Point the GWLB target group's health check at this port** (Section 4.4) — do not write a separate hand-rolled healthcheck.
- Sizing/scaling flags: `--udpthreads`, `--udpaffinity`, `--tunthreads`, `--tunaffinity` — pin to specific vCPUs, distinct from the cores pinned to Suricata (Section 5.3).
- Requires `CAP_NET_ADMIN` — grant via systemd `AmbientCapabilities=CAP_NET_ADMIN` rather than running the whole process as root.

### 5.2 Zone-Based Policy Engine ("the PL") — nftables

**Why nftables over iptables:** single ruleset for IPv4/IPv6 (`table inet`), atomic ruleset replace (`nft -f`, one netlink transaction — no flush-then-reload race window), native maps/sets with interval support (CIDR-keyed lookups without one rule per subnet).

**Architectural nuance — get this right:** because all traffic (north-south *and* east-west) arrives through the same GWLB GENEVE tunnel, the appliance does not get one interface per security zone. `gwi-<X>`/`gwo-<X>` pairs are per-AZ plumbing, not per-zone. **Zone membership is derived from source/destination IP/CIDR, never from interface name.**

> Terminology note: nftables' own `ct zone` primitive is conntrack table partitioning — unrelated to "security zone." Never conflate them in code or comments.

**Table/chain structure** (corrected — see the two fixes below relative to earlier drafts of this design):

```
table inet fw {

    # ---- Zone membership (source of truth: zone-definitions.yaml) ----
    # FIX: no catch-all 0.0.0.0/0 entry in this map. nftables interval
    # sets/maps require non-overlapping ranges; a superset element
    # (0.0.0.0/0) coexisting with more-specific subnet elements in the
    # same interval map is not valid and will fail `nft -c -f`.
    # Unmatched traffic is handled by an explicit fallback rule below,
    # not by an entry in this map.
    map cidr_zone_v4 {
        type ipv4_addr : mark ; flags interval
        elements = {
            10.0.0.0/16   : 0x00000001,   # zone-trust  (spoke VPC A)
            10.1.0.0/16   : 0x00000001,   # zone-trust  (spoke VPC B)
            10.100.0.0/24 : 0x00000002,   # zone-dmz
        }
    }
    map cidr_zone_v6 { type ipv6_addr : mark ; flags interval ; elements = { ... } }

    # ---- Classify once per new connection, persist for life of flow ----
    chain zone-classify {
        type filter hook forward priority -10; policy accept;
        ct state new meta mark set ip  saddr map @cidr_zone_v4
        ct state new meta mark set ip6 saddr map @cidr_zone_v6
        # Fallback: anything that didn't match a defined zone CIDR is untrust.
        ct state new meta mark 0x00000000 meta mark set 0x00000003
        ct state new ct mark set meta mark          # cache src-zone in conntrack
    }

    # ---- Dispatch to per zone-pair chain (dest zone looked up live) ----
    chain zone-policy {
        type filter hook forward priority 0; policy drop;
        ct state established,related accept
        ct state invalid drop

        ct mark 0x00000001 ip daddr @zone_untrust_v4 jump zone-trust-to-untrust
        ct mark 0x00000001 ip daddr @zone_dmz_v4      jump zone-trust-to-dmz
        ct mark 0x00000002 ip daddr @zone_trust_v4    jump zone-dmz-to-trust
        ct mark 0x00000003 ip daddr @zone_trust_v4    jump zone-untrust-to-trust
        # ... every zone-pair the policy list actually defines, in generated order

        log prefix "PL-DEFAULT-DENY: " drop           # implicit deny-all, logged
    }

    # ---- One chain per zone-pair == one "policy context" ----
    chain zone-trust-to-untrust {
        tcp dport 443 log prefix "PL-ALLOW rule=trust-untrust-web: " accept
        tcp dport 80  log prefix "PL-ALLOW rule=trust-untrust-http: " \
            queue num 0-3 options fanout,bypass          # allow AND send to Suricata (5.3)
        log prefix "PL-DENY rule=trust-untrust-default: " drop
    }
    chain zone-dmz-to-trust     { ... }
    chain zone-untrust-to-trust { ... }
}
```

`zone_trust_v4` / `zone_dmz_v4` / `zone_untrust_v4` above are **destination-zone sets** used by the live dispatch lookup — these must be generated by `generate_nft.py` alongside `cidr_zone_v4`/`cidr_zone_v6`; do not leave them referenced-but-undefined.

**Default posture (flag as a policy decision, not a technical default):** interzone default = **deny + log** for every zone pair. Intrazone traffic is not implicitly allowed either — since this is a centralized inspection VPC specifically for east-west visibility/control, intra-trust traffic should also transit named rules.

**Policy-as-code pipeline:**

```
policy/
├── zone-definitions.yaml     # zone name -> CIDR list(s)
├── policy.yaml                # ordered rule list (the actual PL)
└── generate_nft.py            # renders policy.yaml -> /etc/nftables/pl.nft
```

`policy.yaml` schema (one entry = one rule row):

```yaml
- name: trust-untrust-web
  src_zone: trust
  dst_zone: untrust
  service: [{proto: tcp, port: 443}]
  action: allow
  log: true
  inspect: false        # true => also route through Suricata NFQUEUE
- name: trust-untrust-http
  src_zone: trust
  dst_zone: untrust
  service: [{proto: tcp, port: 80}]
  action: allow
  log: true
  inspect: true
```

`generate_nft.py` responsibilities:
1. Validate schema (pydantic/jsonschema) — reject unknown zones, overlapping/shadowed rules, duplicate rule names.
2. Render `zone-definitions.yaml` into the `cidr_zone_v4`/`cidr_zone_v6` **source**-zone maps (no catch-all element — see fix above).
3. **Render the corresponding `zone_trust_v4` / `zone_dmz_v4` / `zone_untrust_v4` (etc.) destination-zone sets** used by `zone-policy`'s dispatch rules — these are a separate generation step from #2 and must not be skipped.
4. Render `policy.yaml`, grouped/ordered, into per-zone-pair chains; `inspect: true` rows get `queue num 0-3 options fanout,bypass` appended to their `accept`.
5. Emit one `.nft` file, validate with `nft -c -f pl.nft` (syntax check, no apply) before it ships to any instance — this is the CI gate.
6. On the instance, apply via `nft -f pl.nft` (atomic, single-transaction reload).

### 5.3 Inline IPS — Suricata over NFQUEUE

Only traffic the PL has already **allowed** and flagged `inspect: true` reaches Suricata:

```
tcp dport 80 queue num 0-3 options fanout,bypass
```

- `queue num 0-3` fans across 4 kernel NFQUEUE queues; `fanout` distributes by CPU id so it scales cleanly across Suricata worker threads pinned to matching cores.
- `bypass` = fail-open: if no process is attached to a queue, matching packets are ACCEPTed rather than dropped. **This is a deliberate availability-vs-security trade-off — default to `bypass` on**, paired with a hard CloudWatch alarm on `suricata.service` health so "uninspected" windows are seconds, not silent and indefinite. For a specific high-sensitivity zone pair (e.g. `dmz-to-trust`), override to fail-closed per-rule by simply omitting `bypass` on that chain's queue clause — this is a per-rule generator knob, not global.
- Run Suricata with matching queue numbers (`suricata -q 0 -q 1 -q 2 -q 3`); align `threading.cpu-affinity` in `suricata.yaml` to the same cores nftables' `fanout` hashes onto, and to cores **not** used by gwlbtun's `--tunaffinity`/`--udpaffinity` pins, to avoid contention.
- Also set the `nfq:` block in `suricata.yaml` (`fail-open`, `batchcount`, `route-queue`) consistently with the posture above — this is a second, independent fail-open control at the libnetfilter_queue layer (kernel-queue-overflow/backpressure), distinct from nft's `bypass`.
- Ruleset: **Emerging Threats Open (ET Open)**, pulled via `suricata-update` on a systemd timer. Use `suricatasc -c ruleset-reload-nonblocking` for live reload without dropping inspected traffic.
- `HOME_NET` = the union of all inspected VPC CIDRs — drive it from the same `zone-definitions.yaml` used by the nftables generator (single source of truth).

**Logging:** Suricata `eve.json` + nftables `log` lines (via journald/syslog) both ship via CloudWatch Agent into distinct log groups (`/inspection-vpc/suricata-eve`, `/inspection-vpc/nft-pl`). Optionally subscribe both to Kinesis Firehose → S3 (Parquet) for long-retention/Athena querying.

### 5.4 EC2 / OS Baseline

**AMI: Ubuntu 24.04 LTS** — over Amazon Linux 2023 specifically because of Suricata packaging/patch maintainability (official apt package + OISF's own repo track Ubuntu primarily; AL2023 has no equivalent low-friction path). nftables, cloud-init, and the `ena` driver are equivalently solid on both, so this is decided on the one component with real CVE-driven churn.

**Instance type:** sustained (not bursty) workload — **avoid burstable (t-family)**. Baseline: Nitro, high-networking compute-optimized, `c6in.2xlarge` (8 vCPU) or `c6in.4xlarge` (16 vCPU) per instance. Scale **horizontally** via the ASG (more targets across AZs), not vertically. Graviton (`c7gn`) is a valid price/perf POC candidate (gwlbtun and Suricata both build natively on arm64); `c6in` (x86) is the safer day-one default. Drive ASG scaling policy off a custom CloudWatch metric derived from Suricata's `capture.kernel_drops`/`capture.kernel_packets` (queue-drop rate) — not CPU% alone — since NFQUEUE backpressure is the real early-warning signal.

**Target type is `instance`** (Section 4.4) — matches this design's one-ENI-per-instance, one-ASG-per-AZ model.

**ENI / kernel settings (per instance):**
- `net.ipv4.ip_forward=1` (and `net.ipv6.conf.all.forwarding=1` if dual-stack) — persist via `/etc/sysctl.d/99-inspection.conf`.
- `net.ipv4.conf.<gwi-*>.rp_filter=0` (or `2`) per tunnel interface — set from gwlbtun's `-c` create-hook script, since these interfaces appear dynamically.
- **`source_dest_check` on the primary GWLB-facing ENI is left at its default (enabled) — do NOT disable it.** Correction from an earlier draft of this spec: GENEVE-encapsulated packets from GWLB are addressed to the target ENI's own IP:6081, so this is not a "forwarding" pattern the check blocks. Only disable `source_dest_check` on a **separate secondary ENI** if the firewall also does conventional IP forwarding outside the GWLB/tunnel path — never on the primary data-plane ENI.
- Raise `net.netfilter.nf_conntrack_max` (and `nf_conntrack_buckets`) well above default — a centralized inspection VPC aggregates flow volume from every attached spoke.
- `net.ipv4.conf.all.accept_redirects=0` — standard router hardening.
- **MTU (verify explicitly, do not skip):**
  - Set the primary/GWLB-facing ENI to **jumbo frames (9001 MTU)** — Nitro instances and this VPC support it, and it gives headroom to receive GENEVE-encapsulated frames without fragmentation. GWLB/GENEVE does **not** fragment oversized frames; it drops them, so an undersized MTU here fails silently and intermittently rather than cleanly.
  - In gwlbtun's `-c` create-hook script, set `gwi-<X>`/`gwo-<X>` tunnel interface MTU to match the actual **inner-packet** MTU used across the spoke VPCs and Transit Gateway — this is environment-specific, confirm it rather than hardcoding. Transit Gateway attachments have a hard **8500-byte MTU ceiling**, so 8500 is the practical maximum regardless of what the Inspection VPC itself could otherwise support; if spokes have not enabled jumbo frames, use 1500 instead. AWS documents GENEVE encapsulation overhead (on the order of tens of bytes) on top of the original packet — confirm the current documented figure against AWS's GWLB docs at build time, and always keep tunnel-interface MTU comfortably below the primary ENI's MTU minus that overhead.
  - **Before declaring this done, run an actual path-MTU/large-packet test through the full spoke → inspection → internet path** (not just a config review) — MTU mismatches in this design manifest as "some connections randomly stall on large transfers," not a clean error, and are easy to miss in a first-pass build.

**Provisioning — golden AMI (Packer) for the heavy layer, thin cloud-init for the fast-changing layer:**

| Layer | Where built | Why |
|---|---|---|
| OS baseline, gwlbtun binary, Suricata + ET Open baseline, nftables, sysctl hardening, systemd units | Packer golden AMI, version-pinned, built/tested in CI | Fast deterministic ASG scale-out with zero external repo dependency at launch time; immutable and testable before rollout |
| Current PL (`policy.yaml`), latest ET Open rule diffs, log-shipping registration | Thin cloud-init at boot + systemd-timer thereafter (Section 5.5) | Changes far more often than the software stack; baking these into the AMI would defeat policy-as-code |

Launch Template pins the golden AMI ID; a CI/CD pipeline republishes a new AMI on a cadence (or on CVE/security patch), then triggers an ASG **instance refresh** for a health-check-gated rolling replacement.

### 5.5 Policy-as-Code Config Management

Source of truth: `policy.yaml` + `zone-definitions.yaml` in Git, PR-reviewed. CI on merge:
1. Schema validation + shadow-rule lint.
2. `generate_nft.py` render + `nft -c -f` syntax gate.
3. Publish raw YAML + rendered `.nft` to a **versioned S3 bucket** (S3 versioning on — Parameter Store's 4–8KB limit makes it unsuitable for the actual rulebase payload once it grows).
4. Write the new S3 object version-id (or a semantic tag) into an **SSM Parameter Store** parameter — the cheap, pollable pointer, not the payload.
5. Propagate via both:
   - **Push:** SSM Run Command / State Manager Association targeted at the ASG's instances by tag, for near-immediate rollout.
   - **Pull:** on-instance `pl-sync.timer` (systemd, e.g. every 5 min) as the fallback — also run synchronously once at boot (from cloud-init, before the instance is marked healthy) so a freshly launched instance never comes up with a stale/empty PL.

**`pl-sync.service` logic:**
1. Compare local `/var/lib/pl-sync/version` against the current SSM parameter.
2. If changed: pull `policy.yaml`/`zone-definitions.yaml` from S3, run `generate_nft.py`, `nft -c -f` validate.
3. Only on successful validation: atomic `nft -f` apply, update local version file, emit an audit log line (`PL-APPLIED version=<x>`).
4. On validation failure: keep the last-known-good ruleset running, raise a CloudWatch alarm/SNS notification — never apply a broken PL.

IAM: instance profile scoped **read-only** to the specific S3 prefix and SSM parameter path (least privilege); CloudTrail captures every fetch.

### 5.6 Diagram Sub-Label — exact text to use

The diagram (Section 6) must disclose the real stack under a generic "Inspection Firewall (Linux EC2)" box title. Use exactly this text (already finalized — the diagram-building phase should not invent or guess a label):

**Two-line version (default — use this):**
```
NFTABLES ZONE POLICY (PL) + SURICATA IPS
GWLB GENEVE TUNNEL HANDLER
```

**Single-line fallback (only if the two-line version does not fit the firewall box at build time — see Section 6.6):**
```
NFTABLES + SURICATA IPS ON GWLB GENEVE TUNNEL
```

Both are ALL CAPS already — see Section 6.6 for why this rules out a small-caps CSS treatment. Never substitute a vendor name (Palo Alto, PANOS, or any other commercial product) anywhere in the diagram markup, comments, alt text, or `<title>`/`<desc>` SVG elements.

---

## 6. Diagram + Hosting Specification

### 6.0 Problem being fixed

The prior diagram was rejected as not aligned/polished. Root cause in hand-placed HTML/CSS diagrams is almost always the same: boxes positioned with independent margins/floats drift relative to each other, and connectors are drawn as freehand paths that don't terminate exactly on box edges. **Fix: the entire diagram body is a single SVG on a fixed `viewBox` grid.** Every box origin, every connector vertex, and every arrowhead anchor lives in that same coordinate space, snapped to a common grid unit. CSS is used only for page chrome (title, subtitle, legend, container, theming) — never to position diagram elements relative to each other.

### 6.1 Node & Edge Inventory (the contract — nothing else gets added without updating this table)

**Nodes:**

| # | Node | Tier | Count | Notes |
|---|---|---|---|---|
| 1 | Internet | External actor | 1 | Outside all boundaries, left edge |
| 2 | Internet Gateway (IGW) | Service | 1 | On the Inspection VPC boundary line |
| 3 | Inspection VPC | Boundary | 1 | Dashed rounded rect containing AZ swimlanes |
| 4 | Gateway Load Balancer (GWLB) | Service | 1 | Regional service; drawn as one band spanning the AZ swimlanes it fronts |
| 5 | GWLB Endpoint | Service (small) | 1 per AZ | Inside each AZ's **GWLB Endpoint subnet** tier box |
| 6 | Inspection Firewall (Linux EC2) | Service | 1 per AZ (2–3) | Title + `{{FIREWALL_SOFTWARE_LABEL}}` sub-label — see Section 6.6 |
| 7 | Availability Zone swimlane | Swimlane | 2–3 | Labeled column nested inside the Inspection VPC boundary |
| 8 | **GWLB Endpoint Subnet** | Subnet tier | 1 per AZ | Contains node 5. *(Renamed from an earlier "Ingress Subnet" — that name collided with the unrelated "Ingress Route Table" concept in node/edge 16 below; do not use "Ingress" for this tier.)* |
| 9 | Firewall Subnet | Subnet tier | 1 per AZ | Contains node 6 |
| 10 | **TGW-attach Subnet** | Subnet tier | 1 per AZ | Hosts the Inspection VPC's TGW attachment ENI for that AZ — this is a real, load-bearing subnet tier (Section 4.1) and must appear as a fourth subnet tier alongside 8/9/Public |
| 11 | **Public / NAT Subnet** | Subnet tier | 1 per AZ | Contains node 12 |
| 12 | **NAT Gateway** | Service (small) | 1 per AZ | Centralized egress NAT (Section 4.1 assumption) — must be represented; it is directly named in the Ingress Route Table mechanism |
| 13 | Transit Gateway (TGW) | Service | 1 | Right of / below the Inspection VPC |
| 14 | Workload VPC A / B (spoke) | Boundary (small) | 2 | Attached to TGW, illustrative spokes |
| 15 | Management & Logging | Service (small) | 1 | Represents CloudWatch/logging — one shared node, not one per firewall |
| 16 | *(annotation, not a box)* IGW Ingress Routing | Callout | 1 | Represents the IGW edge-association route table (Section 4.3) that redirects NAT Gateway return traffic into inspection — render as a distinctly-styled connector/label (Section 6.3), not a physical node, since it's a route-table behavior, not a component |
| 17 | Viewer / Browser | External actor | 1 | Meta panel only |
| 18 | Amazon CloudFront | Service | 1 | Meta panel only |
| 19 | Amazon S3 (origin) | Service | 1 | Meta panel only |

**Edges:**

| # | Edge | Flow class | Style |
|---|---|---|---|
| A | Internet ↔ IGW | North-South | Solid, primary color, double-headed |
| B | IGW ↔ NAT Gateway (per AZ) | North-South | Solid, primary color, double-headed |
| B2 | IGW ↔ GWLB Endpoint (per AZ) | North-South (ingress-routing redirect) | **Distinctly styled** (e.g. dotted, primary color, thinner) from edge B — represents the IGW Ingress Route Table redirecting NAT Gateway return traffic into inspection (node/edge 16). Label this connector directly: "Ingress RT redirect." This is the mechanism most likely to be missed in a first-pass build — making it visually explicit in the diagram is intentional. |
| C | **GWLB Endpoint → GWLB → Firewall** (per AZ) | North-South | Solid, primary color, double-headed (hairpin return via GWLB), enters **top edge** of firewall box only. *(Corrected component order — traffic hits the GWLB Endpoint, the local PrivateLink presence, before the regional GWLB service, not the reverse.)* |
| D | TGW-attach Subnet ⇄ TGW ⇄ Firewall fleet (shared "inspection bus") | East-West | Solid, secondary color, double-headed, enters **side/bottom edge** of the firewall box only — never the same edge as flow C |
| E | TGW ⇄ Workload VPC A | East-West | Solid, secondary color |
| F | TGW ⇄ Workload VPC B | East-West | Solid, secondary color |
| G | Firewall fleet → Management & Logging | Management/control-plane | Thin dashed, neutral color, single representative line (not fan-out per instance) |
| H | Viewer → CloudFront → S3 | Meta / page-delivery | Dashed, accent color, confined to the separated meta panel (Section 6.4) |

This inventory is the contract: every node/edge above must exist in the final SVG; nothing else should be added without updating this table.

### 6.2 Visual Hierarchy (top → bottom of page)

1. **Title** — e.g. "Centralized Traffic Inspection Architecture"
2. **Subtitle** — one line of context (env/account/region scope)
3. **Legend** — flow-color key, rendered as **real HTML** (`<ul>`), not SVG text (accessibility + reflow — see Section 6.7)
4. **Diagram body** — the single SVG, in a max-width centered container
5. **Footnote / meta caption** — small print noting the CloudFront/S3 panel is "how this page is delivered," not part of the depicted network

Each section is a distinct semantic block (`<header>`, legend `<nav>`, `<main><figure><svg>…</svg></figure></main>`, `<footer>`) so hierarchy is structural, not just visual.

### 6.3 Layout System — SVG Coordinate Grid

- `viewBox="0 0 1600 1000"`, `preserveAspectRatio="xMidYMid meet"`.
- **Base grid unit: 8px.** Every box `x`/`y`/`width`/`height` and every connector vertex must be a multiple of 8 — no eyeballed odd coordinates.
- Illustrative region layout (finalize exact numbers during build, but every value must stay grid-snapped):

| Region | Approx. X | Approx. Y | Contains |
|---|---|---|---|
| Internet + IGW column | 40–200 | 380–520 | Nodes 1, 2 |
| Inspection VPC boundary | 240–1080 | 80–760 | Nodes 3, 7–12 |
| GWLB service band | 280–1040 | 120–184 | Node 4 — *(corrected Y-range: the band's fixed height is 64px per Section 6.5's sizing table; 120–200 would imply height 80, which contradicts that table — use 120–184)* |
| AZ swimlanes (2-AZ variant) | 280–660 / 660–1040 | 240–740 | Nodes 5, 6, 8, 9, 10, 11, 12 ×2 |
| AZ swimlanes (3-AZ variant) | 280–533 / 533–787 / 787–1040 | 240–740 | same ×3 |
| Inspection-bus channel (E-W routing) | 1080–1160 | 700 (fixed y) | Routing spine for edge D |
| Transit Gateway | 1160–1320 | 640–760 | Node 13 |
| Workload VPC A / B | 1360–1560 | 480–580 / 620–720 | Node 14 ×2 |
| Management & Logging | 280–460 | 800–880 | Node 15 |
| CloudFront/S3 meta panel | 1160–1560 | 80–260 | Nodes 17–19, visually separated (dotted accent border) |

- **AZ swimlane width formula:** `swimlane_width = (VPC_inner_width − (N−1) × gutter) / N`, gutter fixed at 24px (3 grid units) — 2-AZ and 3-AZ variants are generated from the same formula, never re-eyeballed.
- **Routing channels:** reserve fixed gutter bands between regions exclusively for connector routing — no box in a gutter, no connector crossing a box.
- **Dev-grid overlay (build aid only):** include an optional `<g id="dev-grid" style="display:none">` of light gridlines at 8px/40px spacing, toggleable during layout QA, confirmed hidden by default before handoff.

### 6.4 Sizing System

| Tier | Corner radius | Stroke width | Fill | Border |
|---|---|---|---|---|
| Boundary (VPC, spoke VPCs) | 16 | 2 | none / near-transparent tint | dashed |
| Swimlane (AZ column) | 12 | 1.5 | subtle tint, distinct from subnet tier | dashed |
| Subnet tier | 8 | 1 | light tint | solid |
| Service box | 6 | 1.5–2 | solid surface color | solid |

**Fixed box dimensions per tier (multiples of 8, reused everywhere, no one-off sizing):**

| Box type | Width × Height |
|---|---|
| Service box (standard — IGW, TGW, CloudFront, S3, NAT Gateway) | 160 × 64 |
| **Firewall EC2 instance box** | **200 × 104** — *(widened/heightened from an earlier 176×88 to reliably fit the two-line sub-label from Section 5.6 at the specified font size — see Section 6.6 for the sizing rationale; this is now the fixed, reused value for this box type, not a one-off)* |
| GWLB Endpoint (small node) | 120 × 40 |
| GWLB (spanning band) | variable width (spans AZ swimlanes) × 64 |
| Management & Logging | 160 × 56 |

**Typography:**

| Element | Size | Weight | Case/style |
|---|---|---|---|
| Diagram title (HTML) | 24px | 600 | Sentence case |
| Subtitle (HTML) | 14px | 400 | Sentence case, muted |
| VPC/boundary label | 13px | 600 | Uppercase, letter-spacing 0.5px |
| AZ swimlane label | 12px | 600 | Uppercase |
| Subnet tier label | 11px | 500 | Sentence case, muted |
| Service box title | 13–14px | 600 | Sentence case |
| Service box sub-label (generic) | 10–11px | 500 | Muted |
| **Firewall software sub-label** | see **Section 6.6** below | | *(corrected cross-reference — an earlier draft pointed to §7/connector-routing by mistake)* |
| Legend label (HTML) | 13px | 500 | Sentence case |

All SVG text uses the same system-font stack as the page (Section 6.8); set `text-anchor`/`dominant-baseline` consistently per role.

### 6.5 Component Catalog

Each node type gets one reusable "stamp" `<g>` template (title, dimensions, radius, fill/stroke role, text position), reused with translated coordinates for every instance — this is what prevents "every box is slightly different" drift.

- **IGW / TGW / GWLB / CloudFront / S3 / NAT Gateway / Mgmt-Logging:** service-box tier, centered simple inline-SVG pictogram + title, no sub-label.
- **Firewall EC2 instance:** larger service-box variant (200×104), icon + title ("Inspection Firewall (Linux EC2)") + sub-label, stacked vertically with fixed internal padding, each row a fixed y-offset multiple of 8 from box top.
- **VPC / spoke VPC boundary:** label in a small top-left tab/header band (not centered — centered labels on large boundary boxes read as floating/misaligned).
- **AZ swimlane:** label centered in a header strip at the top of the column, full-height dashed column below.
- **Subnet tier box:** small label top-left, node(s) inside.

### 6.6 Firewall Box & Software Sub-Label — full spec

**Box content, top to bottom, fixed offsets:** icon (optional, small, top-center) → title "Inspection Firewall (Linux EC2)" → sub-label (Section 5.6 text).

**Sub-label styling:**

```css
.firewall-sublabel {
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  fill: var(--text-muted);
  opacity: 0.85;
}
```

- **Use uppercase + letter-spacing only. Do not implement a `font-variant-caps: all-small-caps` fallback at all.** The label text in Section 5.6 is already fully uppercase, and true small-caps requires lowercase source characters to render correctly — applying small-caps CSS to already-uppercased text is a no-op at best and a rendering trap at worst. This removes the "pick one, don't mix" ambiguity from an earlier draft by simply not offering small-caps as an option.
- Vertical gap between title baseline and sub-label first line: fixed 16px (2 grid units). If rendering the two-line label, use a fixed 14px line-height between the sub-label's own two lines.
- **Sizing check (do this during the build, don't skip):** the two-line default label ("NFTABLES ZONE POLICY (PL) + SURICATA IPS" / "GWLB GENEVE TUNNEL HANDLER") at 10px uppercase with 0.06em tracking is wide — verify it fits within the firewall box's 200px width (minus horizontal padding) using the actual rendered font. The box was deliberately sized to 200×104 (up from a plain service box) specifically to accommodate this. If it still doesn't fit cleanly at render time:
  1. First, try the **single-line fallback** from Section 5.6 (still likely wide — check it too).
  2. If neither fits at 10px without truncation, widen the firewall box further in fixed 8px increments (updating the "fixed box dimensions" table in Section 6.4 so it stays a single reused value, not a per-instance override) rather than shrinking the font below 10px or wrapping mid-word.
  3. Do not solve this by switching to small-caps (see above) or by hardcoding a shorter, invented label — the exact text is fixed in Section 5.6.
- The placeholder-token workflow from earlier drafts is unnecessary here since the real label text is already finalized (Section 5.6) — write the actual text directly into the SVG, do not ship a `{{FIREWALL_SOFTWARE_LABEL}}` template token requiring a separate find/replace step. Apply this identical text/styling to every firewall instance box (one per AZ), no per-AZ variation.

**GWLB Endpoint note:** shown as a small node inside each AZ's GWLB Endpoint subnet tier box, for technical accuracy (it's what receives redirected traffic before the firewall). Do not omit it for a "simpler" diagram — it's load-bearing per the node inventory in Section 6.1.

### 6.7 Connector & Routing Spec

**Routing rules:**
- **Orthogonal only:** every connector is a `<path>` built from horizontal/vertical segments (`M`/`L`), 90° bends only — no diagonals, no beziers. Non-negotiable for the "aligned" requirement.
- **Fixed bend points:** bends occur only inside reserved routing gutters (Section 6.3), never inside or across a box.
- **Dedicated box edges per flow class at the firewall box:** north-south (edge C) enters the **top edge** only; east-west (edge D) enters the **side or bottom edge** only, whichever faces the TGW routing channel — never the same edge as edge C. This visually disambiguates the convergence point.
- **Hairpin/bidirectional flows:** GWLB↔firewall and TGW-bus↔firewall are round-trip paths — represent each as a **single line with arrowheads on both ends**, not two overlapping one-way lines.
- **Parallel-offset rule:** if two same-class lines must share a channel segment, offset by a fixed 6px so they render as distinguishable parallel lines, never coincident.
- **Corners:** sharp 90° by default; if a softer look is wanted, apply one fixed small radius (e.g. 4px) to *every* bend uniformly — never mix sharp and rounded corners in the same diagram.

**Arrowheads:**
- One `<marker>` per flow-color role in `<defs>` (`marker-arrow-primary`, `marker-arrow-secondary`, `marker-arrow-mgmt`, `marker-arrow-meta`), identical geometry, differing only by fill color token.
- Marker fill references the same CSS custom property as its line's stroke, so one theme-token change updates line + arrowhead together.
- Consistent `markerWidth`/`markerHeight`/`refX`/`refY` across all markers.

**Semantic color roles (tokens, no hardcoded hex):**

| Token | Used for |
|---|---|
| `--flow-north-south` (primary) | Edges A, B, B2, C |
| `--flow-east-west` (secondary) | Edges D, E, F |
| `--flow-mgmt` (neutral, muted, dashed, thin) | Edge G |
| `--flow-meta` (accent) | Edge H |
| `--boundary-stroke` (neutral) | VPC/swimlane dashed borders — visually distinct from all flow colors |

Both light and dark palettes must satisfy **WCAG AA contrast** against their respective backgrounds for all four flow tokens plus text tokens, and the two main flow colors (north-south vs. east-west) must remain distinguishable to colorblind viewers, not just by hue — pair the color distinction with a line-style distinction (e.g. differing dash pattern or thickness) as a backup differentiator.

### 6.8 Legend Spec

Rendered in **HTML**, not SVG (`<ul class="legend">` of swatch + label pairs), positioned between subtitle and diagram — real text is more accessible (screen readers, zoom, search) and reflows via CSS flex-wrap without SVG layout math. One entry per flow role in Section 6.7's token table, plus a boundary/neutral swatch explaining the dashed rounded rectangles (VPC/AZ boundaries) — a distinct visual language from the flow lines. Labels: "North-South — Internet ingress/egress," "East-West — Inter-VPC via Transit Gateway," "Management / Control Plane," "Page Delivery (CloudFront + S3)."

### 6.9 CloudFront/S3 Meta Panel (page-delivery path)

This must appear (per the node inventory) without being confused for part of the depicted network — it's unrelated (the diagram describes an inspection VPC's data plane; CloudFront/S3 is just how the page itself loads).

- Visually separated inset panel, top-right corner (Section 6.3 region map).
- Distinct border style: **dotted, accent color** (not the dashed boundary style used for VPCs).
- Small caption directly above it: *"How this page is delivered."*
- Its own mini-flow (Viewer → CloudFront → S3) uses `--flow-meta`, never reusing `--flow-north-south`/`--flow-east-west`.
- **Not connected by any line to the main diagram** — a deliberate island, reinforcing that it's out-of-band/meta content.

### 6.10 Responsive Behavior

- Outer page container: `max-width: 1400px; margin: 0 auto; padding: 0 24px;` — no child ever exceeds viewport width.
- `<div class="diagram-scroll">` wraps the `<svg>`, with `overflow-x: auto; overflow-y: hidden; -webkit-overflow-scrolling: touch;`.
- The SVG scales fluidly via `viewBox` down to a floor width (`min-width: 900px` on the `<svg>` element) below which text becomes illegible — below that floor the wrapper's own horizontal scroll kicks in. Do not rely on infinite SVG shrinking.
- Add a subtle edge-fade or "scroll for full diagram →" affordance on the wrapper when actively scrollable (CSS-only gradient mask), so horizontal scroll is discoverable on touch devices.
- `body`/`html` must never need `overflow-x: hidden` as a band-aid — any overflow outside the diagram wrapper is a layout bug to fix at the source.
- Title/subtitle/legend/footer reflow normally as ordinary responsive HTML; only the SVG diagram body has a scroll fallback.

### 6.11 Light / Dark Mode

- All colors as CSS custom properties on `:root`, with a `@media (prefers-color-scheme: dark)` block overriding the full token set (backgrounds, surfaces, text, boundary stroke, all four flow tokens).
- SVG consumes the same CSS variables (`fill`/`stroke: var(--token)` or classes styled from the external `<style>` block) — no colors hardcoded inline on SVG shapes, or dark mode will only re-theme the HTML chrome.
- Verify both palettes independently: text-on-surface contrast, flow-line-on-background contrast, and that the four flow colors remain mutually distinguishable, including for colorblind viewers.

### 6.12 Self-Contained / CSP Constraints

- **One `.html` file.** All CSS in a single inline `<style>` block; all SVG inline (no `<img src="diagram.svg">`); zero JS — this diagram needs none.
- **Fonts:** system stack only (`-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`) — no `@font-face`, no Google Fonts, no CDN font links.
- **Icons:** simple inline-SVG geometric primitives (rounded rect, simple cloud/shield/cylinder shapes), not the official AWS Architecture Icons set — dependency-free, no icon-license/versioning question.
- **No external network calls of any kind** from the page — this is what lets CloudFront serve it behind a strict CSP with `script-src 'none'`.

### 6.13 Accessibility Notes

- `<svg>` gets a `<title>` and `<desc>` describing the diagram at a high level, text content only, no vendor name (consistent with Section 5.0/5.6).
- Each box's label is real SVG `<text>`, selectable and readable by assistive tech — not raster/icon-only.
- Legend (HTML) covers the color-meaning-to-text mapping required for colorblind/non-visual users (don't rely on color alone — flow lines should also differ in style, not just hue).

### 6.14 File Structure Skeleton

```
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>…</title>
  <style>
    :root { /* light theme tokens */ }
    @media (prefers-color-scheme: dark) { :root { /* dark overrides */ } }
    /* page chrome: header, subtitle, legend, container, diagram-scroll wrapper, footer */
    /* svg element rules: min-width, display:block, theme-token-driven fill/stroke classes */
  </style>
</head>
<body>
  <header> <!-- title, subtitle --> </header>
  <nav class="legend"> <!-- flow-color key, real HTML list --> </nav>
  <main>
    <figure>
      <div class="diagram-scroll">
        <svg viewBox="0 0 1600 1000" role="img" aria-labelledby="diagram-title diagram-desc">
          <title id="diagram-title">…</title>
          <desc id="diagram-desc">…</desc>
          <defs> <!-- arrowhead markers, reusable symbols --> </defs>
          <g id="boundaries">        <!-- Inspection VPC, spoke VPCs --> </g>
          <g id="swimlanes">         <!-- AZ columns + 4 subnet tiers --> </g>
          <g id="services">          <!-- IGW, NAT GW, GWLB, GWLB endpoints, firewall instances, TGW, mgmt/logging --> </g>
          <g id="flows-north-south"> <!-- edges A, B, B2, C --> </g>
          <g id="flows-east-west">   <!-- edges D, E, F --> </g>
          <g id="flows-mgmt">        <!-- edge G --> </g>
          <g id="meta-panel">        <!-- CloudFront/S3/viewer, edge H --> </g>
          <g id="dev-grid" style="display:none"> <!-- build-time alignment aid, hidden --> </g>
        </svg>
      </div>
    </figure>
  </main>
  <footer> <!-- meta-panel caption / disclaimer --> </footer>
</body>
</html>
```

Layering order: boundaries → swimlanes → services → flow lines → meta panel, so flow lines render on top of boxes and z-order is deterministic.

### 6.15 S3 + CloudFront Hosting Spec

**S3 bucket:**

| Setting | Value |
|---|---|
| Access | Private — Block Public Access: all 4 settings ON |
| Website hosting feature | **Off** — do not enable the S3 static-website endpoint, it doesn't support OAC and only serves publicly. Origin is the bucket's standard REST endpoint. |
| Bucket policy | `s3:GetObject` only to principal `cloudfront.amazonaws.com`, scoped with `Condition: { StringEquals: { "AWS:SourceArn": "<this distribution ARN>" } }` — OAC pattern, no wildcard principal, no legacy OAI |
| Versioning | On (rollback safety on redeploys) |
| Encryption | Default SSE-S3 |
| Object | `index.html` |

**CloudFront distribution:**

| Setting | Value |
|---|---|
| Origin | S3 bucket via **Origin Access Control (OAC)**, not legacy OAI |
| Viewer protocol policy | Redirect HTTP → HTTPS |
| Default root object | `index.html` |
| Allowed methods | GET, HEAD only |
| Certificate (no custom domain) | Default `*.cloudfront.net` certificate — works immediately, no ACM needed |
| Certificate (custom domain, optional) | Requires ACM cert **in us-east-1**, the hostname as an Alternate Domain Name, and a DNS record pointing at the distribution — **do not provision until confirmed (Section 6.16)** |
| Response headers policy | `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `X-Frame-Options: DENY`, and a strict `Content-Security-Policy` (e.g. `default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; img-src 'self' data:; base-uri 'none'` — finalize against what the shipped file actually needs; `script-src 'none'` is achievable since there is genuinely no `<script>`) |
| Access logging | Optional |
| WAF | Optional — see Section 6.16 |

**Cache strategy:** fixed key + short TTL (recommended for a single, occasionally-redeployed page): keep `index.html` as the key, set S3 object metadata `Cache-Control: public, max-age=300, must-revalidate`, run a CloudFront invalidation on `/index.html` as the last step of every redeploy.

### 6.16 Confirm before finalizing (do not provision until confirmed)

1. **Custom domain vs. default `*.cloudfront.net` URL** — default to the plain CloudFront URL unless told otherwise.
2. **Fully public link access vs. restricted (WAF IP allow-list, or signed URLs/cookies)** — this page depicts internal network architecture; default to leaving it open only behind the private S3 + CloudFront-only access pattern (no direct S3 access, HTTPS-only), and flag WAF/signed-URL restriction as an available upgrade if the user wants it.

### 6.17 No vendor label — restated explicitly

**The diagram must never display "Palo Alto," "PANOS," or any other commercial NGFW vendor name anywhere** — not in a box title, sub-label, code comment, `alt` text, or SVG `<title>`/`<desc>`. The firewall box title is exactly "Inspection Firewall (Linux EC2)" and the sub-label is exactly the text specified in Section 5.6. This is the real, deployed software stack (nftables + Suricata + gwlbtun) — disclose it accurately, do not imply a commercial product that isn't there.

---
