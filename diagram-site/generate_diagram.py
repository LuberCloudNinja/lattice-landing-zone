#!/usr/bin/env python3
"""generate_diagram.py -- builds diagram-site/index.html from computed,
grid-snapped coordinates rather than hand-placed SVG numbers.

Implements docs/inspection-architecture-reference.md Section 6 (the design
spec for this diagram): a single SVG on a fixed 8px coordinate grid, node/
edge inventory per Section 6.1, swimlane widths computed from Section 6.3's
formula (never re-eyeballed), orthogonal-only connectors, real-HTML legend,
light/dark mode via CSS custom properties, no vendor name anywhere on the
firewall boxes (Section 6.17).

Run this to regenerate index.html after changing the layout:
    python3 generate_diagram.py
"""

from pathlib import Path

GRID = 8
AZ_COUNT = 2  # must match config.INSPECTION_AZ_COUNT
FIREWALL_APPLIANCES_PER_AZ = 2  # must match config.FIREWALL_APPLIANCES_PER_AZ

VIEWBOX_W = 1600
VIEWBOX_H = 1000

# Exact text from docs/inspection-architecture-reference.md Section 5.6 --
# do not invent a shorter label or substitute a vendor name (Section 6.17).
FIREWALL_TITLE = "Inspection Firewall (Linux EC2)"
FIREWALL_SUBLABEL_LINE1 = "NFTABLES ZONE POLICY (PL) + SURICATA IPS"
FIREWALL_SUBLABEL_LINE2 = "GWLB GENEVE TUNNEL HANDLER"


def snap(v: float) -> int:
    """Round to the nearest grid unit -- a safety net; every coordinate
    below is derived from grid-unit arithmetic already, this just guards
    against a stray non-grid value ever reaching the output."""
    return round(v / GRID) * GRID


# ---------------------------------------------------------------------------
# Region layout -- computed, not eyeballed (Section 6.3)
# ---------------------------------------------------------------------------
INTERNET_COL_X, INTERNET_COL_W = 40, 160
INTERNET_Y, IGW_Y = 384, 456
SERVICE_BOX_H = 64

VPC_X, VPC_Y, VPC_W, VPC_H = 240, 80, 840, 680
GWLB_BAND_X, GWLB_BAND_Y, GWLB_BAND_W, GWLB_BAND_H = 280, 120, 760, 64

SWIMLANES_Y, SWIMLANES_H = 240, 496
SWIMLANE_GUTTER = 24
SWIMLANE_W = snap((GWLB_BAND_W - (AZ_COUNT - 1) * SWIMLANE_GUTTER) / AZ_COUNT)
SWIMLANE_XS = [GWLB_BAND_X + i * (SWIMLANE_W + SWIMLANE_GUTTER) for i in range(AZ_COUNT)]

AZ_HEADER_H = 24
TIER_GUTTER = 8
TIER_HEIGHTS = {"gwlb_endpoint": 80, "firewall": 240, "tgw_attach": 56, "public_nat": 72}
# 240 for the firewall tier: two stacked 104-tall boxes (one per appliance
# per this project's config.FIREWALL_APPLIANCES_PER_AZ) + label + gutter.
# Sum + 3 inter-tier gutters (8 each) MUST equal SWIMLANES_H - AZ_HEADER_H
# (472) exactly, or a tier silently overflows the swimlane's own boundary
# box (found by actually screenshotting a first draft -- the original
# 88/240/88/48 split summed to 488, 16px over budget, and the undersized
# public_nat tier made its own label collide with the NAT Gateway box
# inside it. This split sums to 448 + 24 = 472, verified against the
# swimlane height below by an assertion, not just by eye.

BUS_X, BUS_W = 1080, 80
TGW_X, TGW_Y, TGW_W, TGW_H = 1160, 640, 160, 64
SPOKE_X, SPOKE_W = 1360, 200
SPOKE_A_Y, SPOKE_A_H = 480, 96
SPOKE_B_Y, SPOKE_B_H = 616, 96
MGMT_X, MGMT_Y, MGMT_W, MGMT_H = 280, 800, 160, 56
META_X, META_Y, META_W, META_H = 1160, 80, 400, 176

FIREWALL_W, FIREWALL_H = 200, 104
GWLB_EP_W, GWLB_EP_H = 120, 40


def build_layout():
    """Returns (tier_ys: dict, swimlane info) -- the vertical stack of the
    4 subnet tiers inside a swimlane, computed once and reused per AZ."""
    y = SWIMLANES_Y + AZ_HEADER_H
    tier_ys = {}
    for tier in ("gwlb_endpoint", "firewall", "tgw_attach", "public_nat"):
        tier_ys[tier] = y
        y += TIER_HEIGHTS[tier] + TIER_GUTTER
    y -= TIER_GUTTER  # no trailing gutter after the last tier
    swimlane_bottom = SWIMLANES_Y + SWIMLANES_H
    assert y == swimlane_bottom, (
        f"subnet tier stack ends at y={y}, but the swimlane's own boundary "
        f"ends at y={swimlane_bottom} -- TIER_HEIGHTS no longer sums to "
        f"SWIMLANES_H - AZ_HEADER_H; a tier would silently overflow the "
        f"swimlane box. Fix TIER_HEIGHTS, don't just ignore this."
    )
    return tier_ys


TIER_YS = build_layout()


# ---------------------------------------------------------------------------
# SVG component "stamps" (Section 6.5) -- one template per node type, reused
# via translated coordinates so every instance is identical.
# ---------------------------------------------------------------------------

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def icon_cloud(cx: int, cy: int) -> str:
    return (f'<g transform="translate({cx-12},{cy-8})" class="icon">'
            f'<path d="M4 16a6 6 0 0 1 11.3-2.8A5 5 0 0 1 20 18H5a4 4 0 0 1-1-7.9z" fill="none" stroke="currentColor" stroke-width="1.5"/>'
            f'</g>')


def icon_gateway(cx: int, cy: int) -> str:
    return (f'<g transform="translate({cx-10},{cy-10})" class="icon">'
            f'<rect x="0" y="4" width="20" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>'
            f'<line x1="4" y1="0" x2="4" y2="4" stroke="currentColor" stroke-width="1.5"/>'
            f'<line x1="16" y1="0" x2="16" y2="4" stroke="currentColor" stroke-width="1.5"/>'
            f'</g>')


def icon_lb(cx: int, cy: int) -> str:
    return (f'<g transform="translate({cx-11},{cy-9})" class="icon">'
            f'<circle cx="11" cy="3" r="2.5" fill="none" stroke="currentColor" stroke-width="1.3"/>'
            f'<circle cx="3" cy="15" r="2.5" fill="none" stroke="currentColor" stroke-width="1.3"/>'
            f'<circle cx="19" cy="15" r="2.5" fill="none" stroke="currentColor" stroke-width="1.3"/>'
            f'<line x1="11" y1="5.5" x2="3" y2="12.7" stroke="currentColor" stroke-width="1.3"/>'
            f'<line x1="11" y1="5.5" x2="19" y2="12.7" stroke="currentColor" stroke-width="1.3"/>'
            f'</g>')


def icon_shield(cx: int, cy: int) -> str:
    return (f'<g transform="translate({cx-9},{cy-10})" class="icon">'
            f'<path d="M9 0 L18 3.5 V10 C18 15 14 18.5 9 20 C4 18.5 0 15 0 10 V3.5 Z" fill="none" stroke="currentColor" stroke-width="1.5"/>'
            f'</g>')


def icon_server(cx: int, cy: int) -> str:
    return (f'<g transform="translate({cx-10},{cy-8})" class="icon">'
            f'<rect x="0" y="0" width="20" height="16" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/>'
            f'<line x1="0" y1="5.5" x2="20" y2="5.5" stroke="currentColor" stroke-width="1"/>'
            f'<line x1="0" y1="11" x2="20" y2="11" stroke="currentColor" stroke-width="1"/>'
            f'</g>')


def icon_bucket(cx: int, cy: int) -> str:
    return (f'<g transform="translate({cx-9},{cy-9})" class="icon">'
            f'<path d="M0 4 L18 4 L15 18 L3 18 Z" fill="none" stroke="currentColor" stroke-width="1.5"/>'
            f'<line x1="0" y1="4" x2="18" y2="4" stroke="currentColor" stroke-width="1.5"/>'
            f'</g>')


def icon_eye(cx: int, cy: int) -> str:
    return (f'<g transform="translate({cx-11},{cy-7})" class="icon">'
            f'<path d="M0 7 C3 1 19 1 22 7 C19 13 3 13 0 7 Z" fill="none" stroke="currentColor" stroke-width="1.4"/>'
            f'<circle cx="11" cy="7" r="3" fill="none" stroke="currentColor" stroke-width="1.4"/>'
            f'</g>')


def icon_bot(cx: int, cy: int) -> str:
    """Agent/AI node marker -- a simple robot-head glyph, same minimalist
    line-art style as the other icons above."""
    return (f'<g transform="translate({cx-10},{cy-10})" class="icon">'
            f'<rect x="2" y="4" width="16" height="13" rx="3" fill="none" stroke="currentColor" stroke-width="1.5"/>'
            f'<line x1="10" y1="0" x2="10" y2="4" stroke="currentColor" stroke-width="1.5"/>'
            f'<circle cx="10" cy="1" r="1.4" fill="currentColor" stroke="none"/>'
            f'<circle cx="6.5" cy="10.5" r="1.6" fill="currentColor" stroke="none"/>'
            f'<circle cx="13.5" cy="10.5" r="1.6" fill="currentColor" stroke="none"/>'
            f'</g>')


def service_box(x, y, w, h, title, icon_fn=None, sub=None, dim=False) -> str:
    icon_svg = icon_fn(x + w // 2, y + 22) if icon_fn else ""
    title_y = y + (h - 12 if icon_fn else h // 2) if not sub else y + h - 26
    if icon_fn:
        title_y = y + 44
    else:
        title_y = y + h // 2 + 4
    parts = [f'<g class="stamp-service">',
             f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" class="{"box-service-dim" if dim else "box-service"}"/>',
             icon_svg,
             f'<text x="{x + w//2}" y="{title_y}" class="label-service-title" text-anchor="middle">{esc(title)}</text>']
    if sub:
        parts.append(f'<text x="{x + w//2}" y="{y + h - 10}" class="label-service-sub" text-anchor="middle">{esc(sub)}</text>')
    parts.append('</g>')
    return "\n".join(parts)


def firewall_box(x, y, label_suffix: str = "") -> str:
    w, h = FIREWALL_W, FIREWALL_H
    title = FIREWALL_TITLE + (f" {label_suffix}" if label_suffix else "")
    return (
        f'<g class="stamp-firewall">'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" class="box-service box-firewall"/>'
        f'{icon_shield(x + w//2, y + 20)}'
        f'<text x="{x + w//2}" y="{y + 42}" class="label-service-title" text-anchor="middle">{esc(title)}</text>'
        f'<text x="{x + w//2}" y="{y + 62}" class="firewall-sublabel" text-anchor="middle">{esc(FIREWALL_SUBLABEL_LINE1)}</text>'
        f'<text x="{x + w//2}" y="{y + 76}" class="firewall-sublabel" text-anchor="middle">{esc(FIREWALL_SUBLABEL_LINE2)}</text>'
        f'</g>'
    )


def subnet_tier(x, y, w, h, label) -> str:
    return (
        f'<g class="stamp-subnet-tier">'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" class="box-subnet-tier"/>'
        f'<text x="{x + 10}" y="{y + 16}" class="label-subnet-tier">{esc(label)}</text>'
        f'</g>'
    )


def swimlane(x, y, w, h, label) -> str:
    return (
        f'<g class="stamp-swimlane">'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" class="box-swimlane"/>'
        f'<text x="{x + w/2}" y="{y + 16}" class="label-swimlane" text-anchor="middle">{esc(label)}</text>'
        f'</g>'
    )


def boundary(x, y, w, h, label, small=False) -> str:
    return (
        f'<g class="stamp-boundary">'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" class="{"box-boundary-small" if small else "box-boundary"}"/>'
        f'<text x="{x + 10}" y="{y + 20}" class="label-boundary">{esc(label)}</text>'
        f'</g>'
    )


def orthogonal_path(points, flow_class, bidirectional=True, dashed=False, label=None) -> str:
    d = f"M {points[0][0]} {points[0][1]} " + " ".join(f"L {px} {py}" for px, py in points[1:])
    marker_start = f'marker-start="url(#marker-arrow-{flow_class})" ' if bidirectional else ""
    dash = 'stroke-dasharray="6 4" ' if dashed else ""
    out = (f'<path d="{d}" class="flow flow-{flow_class}" {dash}'
           f'{marker_start}marker-end="url(#marker-arrow-{flow_class})"/>')
    if label:
        lx, ly = points[len(points) // 2]
        out += f'<text x="{lx}" y="{ly - 6}" class="flow-label">{esc(label)}</text>'
    return out


# ---------------------------------------------------------------------------
# Assemble the diagram
# ---------------------------------------------------------------------------

def build_svg() -> str:
    boundaries, swimlanes_g, services, flows_ns, flows_ew, flows_mgmt, meta = [], [], [], [], [], [], []

    # ---- Internet + IGW (node 1, 2) ----
    services.append(service_box(INTERNET_COL_X, INTERNET_Y, INTERNET_COL_W, SERVICE_BOX_H, "Internet", icon_cloud))
    igw_x, igw_y = INTERNET_COL_X, IGW_Y
    services.append(service_box(igw_x, igw_y, INTERNET_COL_W, SERVICE_BOX_H, "Internet Gateway", icon_gateway))

    # ---- Inspection VPC boundary (node 3) ----
    boundaries.append(boundary(VPC_X, VPC_Y, VPC_W, VPC_H, "INSPECTION VPC"))

    # ---- GWLB band (node 4) ----
    services.append(service_box(GWLB_BAND_X, GWLB_BAND_Y, GWLB_BAND_W, GWLB_BAND_H, "Gateway Load Balancer", icon_lb))

    nat_positions = []
    gwlbe_positions = []
    tgw_attach_centers = []
    firewall_positions = []

    for i in range(AZ_COUNT):
        sx = SWIMLANE_XS[i]
        az_label = f"AVAILABILITY ZONE {chr(65 + i)}"
        swimlanes_g.append(swimlane(sx, SWIMLANES_Y, SWIMLANE_W, SWIMLANES_H, az_label))

        # GWLB Endpoint Subnet tier (node 8) + GWLB Endpoint (node 5)
        ty = TIER_YS["gwlb_endpoint"]
        th = TIER_HEIGHTS["gwlb_endpoint"]
        swimlanes_g.append(subnet_tier(sx + 8, ty, SWIMLANE_W - 16, th, "GWLB ENDPOINT SUBNET"))
        gep_x = snap(sx + (SWIMLANE_W - GWLB_EP_W) / 2)
        gep_y = ty + th - GWLB_EP_H - 8
        services.append(service_box(gep_x, gep_y, GWLB_EP_W, GWLB_EP_H, "GWLB Endpoint", None))
        gwlbe_positions.append((gep_x + GWLB_EP_W // 2, gep_y))

        # Firewall Subnet tier (node 9) + 2 firewall appliances (node 6)
        ty = TIER_YS["firewall"]
        th = TIER_HEIGHTS["firewall"]
        swimlanes_g.append(subnet_tier(sx + 8, ty, SWIMLANE_W - 16, th, "FIREWALL SUBNET"))
        fw_x = snap(sx + (SWIMLANE_W - FIREWALL_W) / 2)
        fw_y1 = ty + 24
        fw_y2 = fw_y1 + FIREWALL_H + 8
        services.append(firewall_box(fw_x, fw_y1, "#1"))
        services.append(firewall_box(fw_x, fw_y2, "#2"))
        firewall_positions.append((fw_x + FIREWALL_W // 2, fw_y1))  # top edge anchor for flow C
        # Right-edge anchor for flow D, at appliance #2's mid-height -- the
        # Firewall Subnet tier box has a clear ~76px margin to the right of
        # the stacked appliance boxes (tier box is 352 wide, appliances 200),
        # so a short horizontal hop here stays inside that tier's own empty
        # space before the path turns to leave the swimlane -- never crosses
        # a box.
        firewall_positions.append((fw_x + FIREWALL_W, fw_y2 + FIREWALL_H // 2))

        # TGW-attach Subnet tier (node 10) -- no big node, just the ENI concept
        ty = TIER_YS["tgw_attach"]
        th = TIER_HEIGHTS["tgw_attach"]
        swimlanes_g.append(subnet_tier(sx + 8, ty, SWIMLANE_W - 16, th, "TGW-ATTACH SUBNET"))
        tgw_attach_centers.append((sx + SWIMLANE_W - 8, ty + th // 2))

        # Public / NAT Subnet tier (node 11) + NAT Gateway (node 12)
        ty = TIER_YS["public_nat"]
        th = TIER_HEIGHTS["public_nat"]
        swimlanes_g.append(subnet_tier(sx + 8, ty, SWIMLANE_W - 16, th, "PUBLIC / NAT SUBNET"))
        nat_w, nat_h = 112, 32
        nat_x = snap(sx + (SWIMLANE_W - nat_w) / 2)
        nat_y = ty + th - nat_h - 8
        services.append(service_box(nat_x, nat_y, nat_w, nat_h, "NAT Gateway", None))
        nat_positions.append((nat_x + nat_w // 2, nat_y))

    # ---- Transit Gateway (node 13) ----
    services.append(service_box(TGW_X, TGW_Y, TGW_W, TGW_H, "Transit Gateway", icon_lb))

    # ---- Workload spokes (node 14) ----
    boundaries.append(boundary(SPOKE_X, SPOKE_A_Y, SPOKE_W, SPOKE_A_H, "APP-VPC (SPOKE)", small=True))
    boundaries.append(boundary(SPOKE_X, SPOKE_B_Y, SPOKE_W, SPOKE_B_H, "PROVIDER-VPC (SPOKE)", small=True))

    # ---- Management & Logging (node 15) ----
    services.append(service_box(MGMT_X, MGMT_Y, MGMT_W, MGMT_H, "Management & Logging", icon_eye))

    # ---- Edges ----
    # A: Internet <-> IGW
    flows_ns.append(orthogonal_path(
        [(INTERNET_COL_X + INTERNET_COL_W, INTERNET_Y + SERVICE_BOX_H // 2),
         (igw_x + INTERNET_COL_W, igw_y + SERVICE_BOX_H // 2)], "north-south"))
    # B + B2: IGW <-> NAT Gateway (per AZ), IGW <-> GWLB Endpoint (ingress redirect, dotted)
    igw_out_x = igw_x + INTERNET_COL_W
    igw_out_y = igw_y + SERVICE_BOX_H // 2
    for i in range(AZ_COUNT):
        nat_cx, nat_top_y = nat_positions[i]
        bend_x = VPC_X - 24
        flows_ns.append(orthogonal_path(
            [(igw_out_x, igw_out_y), (bend_x, igw_out_y), (bend_x, nat_top_y - 12), (nat_cx, nat_top_y - 12), (nat_cx, nat_top_y)],
            "north-south"))
        gep_cx, gep_top_y = gwlbe_positions[i]
        bend_x2 = VPC_X - 12
        path_points = [(igw_out_x, igw_out_y + 8), (bend_x2, igw_out_y + 8), (bend_x2, gep_top_y - 24), (gep_cx, gep_top_y - 24), (gep_cx, gep_top_y)]
        flows_ns.append(orthogonal_path(path_points, "north-south", dashed=True, bidirectional=False))
        if i == 0:
            # Label placed over the clear horizontal run just right of the
            # bend (not the path midpoint, which lands on top of the AZ
            # swimlane's own "GWLB ENDPOINT SUBNET" tier label).
            flows_ns.append(f'<text x="{bend_x2 + 8}" y="{igw_out_y}" class="flow-label">Ingress RT redirect</text>')

    # C: GWLB Endpoint -> GWLB -> Firewall (top edge), per AZ
    for i in range(AZ_COUNT):
        gep_cx, gep_top_y = gwlbe_positions[i]
        fw_top_cx, fw_top_y = firewall_positions[2 * i]
        flows_ns.append(orthogonal_path(
            [(gep_cx, gep_top_y), (gep_cx, GWLB_BAND_Y + GWLB_BAND_H), ], "north-south"))
        flows_ns.append(orthogonal_path(
            [(fw_top_cx, GWLB_BAND_Y + GWLB_BAND_H), (fw_top_cx, fw_top_y)], "north-south"))

    # D: Firewall <-> TGW-attach subnet <-> inspection bus <-> TGW, per AZ.
    # Routed through a single shared horizontal channel BELOW all swimlane
    # content (swimlanes end at y=736; channel at y=744) so AZ-A's line
    # never has to cross AZ-B's swimlane to reach the bus on the right --
    # everything drops down to the clear channel first, then travels right.
    channel_y = 744
    bus_x_center = BUS_X + BUS_W // 2
    for i in range(AZ_COUNT):
        attach_x, attach_y = tgw_attach_centers[i]
        fw_right_x, fw_right_y = firewall_positions[2 * i + 1]
        this_channel_y = channel_y + i * 8  # parallel-offset rule: 8px apart per AZ, never coincident
        flows_ew.append(orthogonal_path(
            [(fw_right_x, fw_right_y), (attach_x, fw_right_y), (attach_x, attach_y)], "east-west",
            bidirectional=False))
        flows_ew.append(orthogonal_path(
            [(attach_x, attach_y), (attach_x, this_channel_y), (bus_x_center, this_channel_y)], "east-west"))
    # bus channel <-> TGW
    flows_ew.append(orthogonal_path(
        [(bus_x_center, channel_y), (bus_x_center, TGW_Y + TGW_H // 2), (TGW_X, TGW_Y + TGW_H // 2)],
        "east-west"))

    # E, F: TGW <-> spokes
    flows_ew.append(orthogonal_path(
        [(TGW_X + TGW_W, TGW_Y + 16), (SPOKE_X - 24, TGW_Y + 16), (SPOKE_X - 24, SPOKE_A_Y + SPOKE_A_H // 2), (SPOKE_X, SPOKE_A_Y + SPOKE_A_H // 2)],
        "east-west"))
    flows_ew.append(orthogonal_path(
        [(TGW_X + TGW_W, TGW_Y + 48), (SPOKE_X - 12, TGW_Y + 48), (SPOKE_X - 12, SPOKE_B_Y + SPOKE_B_H // 2), (SPOKE_X, SPOKE_B_Y + SPOKE_B_H // 2)],
        "east-west"))

    # G: Firewall fleet -> Management & Logging (single representative line,
    # AZ-A's appliance #1 only -- not fan-out per instance, per spec).
    # Routed left through the Firewall Subnet tier's own clear left margin,
    # then through the gap between the VPC boundary edge (x=240) and the
    # swimlane edge (x=280) -- genuinely clear space, crosses no other box.
    mgmt_channel_x = 256
    # firewall_positions[0] is AZ-A appliance #1's TOP-CENTER anchor (used by
    # flow C above) -- derive its left-edge, mid-height point from that.
    fw_left_x = firewall_positions[0][0] - FIREWALL_W // 2
    fw_left_y = firewall_positions[0][1] + FIREWALL_H // 2
    flows_mgmt.append(orthogonal_path(
        [(fw_left_x, fw_left_y), (mgmt_channel_x, fw_left_y), (mgmt_channel_x, MGMT_Y + MGMT_H // 2), (MGMT_X, MGMT_Y + MGMT_H // 2)],
        "mgmt", dashed=True, bidirectional=False))

    # ---- Meta panel: CloudFront/S3 page-delivery (nodes 17-19, edge H) ----
    meta.append(f'<rect x="{META_X}" y="{META_Y}" width="{META_W}" height="{META_H}" rx="10" class="box-meta-panel"/>')
    meta.append(f'<text x="{META_X + 16}" y="{META_Y - 8}" class="label-meta-caption">How this page is delivered</text>')
    viewer_x, viewer_y = META_X + 24, META_Y + 40
    cf_x, cf_y = META_X + 160, META_Y + 40
    s3_x, s3_y = META_X + 296, META_Y + 40
    meta.append(service_box(viewer_x, viewer_y - 20, 96, 56, "Viewer", icon_eye))
    meta.append(service_box(cf_x, cf_y - 20, 96, 56, "CloudFront", icon_lb))
    meta.append(service_box(s3_x, s3_y - 20, 96, 56, "S3 (origin)", icon_bucket))
    meta.append(orthogonal_path([(viewer_x + 96, viewer_y + 8), (cf_x, cf_y + 8)], "meta"))
    meta.append(orthogonal_path([(cf_x + 96, cf_y + 8), (s3_x, s3_y + 8)], "meta"))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south", "east-west", "mgmt", "meta")
    )

    return f'''<svg viewBox="0 0 {VIEWBOX_W} {VIEWBOX_H}" role="img" aria-labelledby="diagram-title diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="diagram-title">Centralized traffic-inspection architecture</title>
  <desc id="diagram-desc">Internet traffic enters through an Internet Gateway into an Inspection VPC, where a Gateway Load Balancer distributes it across a Linux EC2 firewall fleet (two appliances per Availability Zone) for inspection before continuing to a NAT Gateway for egress or, for east-west traffic, back through a Transit Gateway to spoke VPCs. A separate panel shows how this page itself is delivered via CloudFront and S3.</desc>
  <defs>
    {defs}
  </defs>
  <g id="boundaries">{"".join(boundaries)}</g>
  <g id="swimlanes">{"".join(swimlanes_g)}</g>
  <g id="services">{"".join(services)}</g>
  <g id="flows-north-south">{"".join(flows_ns)}</g>
  <g id="flows-east-west">{"".join(flows_ew)}</g>
  <g id="flows-mgmt">{"".join(flows_mgmt)}</g>
  <g id="meta-panel">{"".join(meta)}</g>
</svg>'''


def build_governance_svg() -> str:
    """Second, independent diagram: the recording/detection plane +
    EventBridge -> Lambda -> SNS auto-remediation flow (governance_stack.py
    / drift_remediation_stack.py). Deliberately its own small SVG, not
    grafted onto build_svg()'s inspection-flow canvas above -- that one is
    governed by docs/inspection-architecture-reference.md Section 6's exact
    grid/assertion spec for a single, narrow purpose (the GWLB/firewall/TGW
    traffic path); this is a different concern entirely."""
    w, h = 1600, 692
    services, flows, boundaries = [], [], []

    trail_x, trail_y = 40, 148
    services.append(service_box(trail_x, trail_y, 160, 64, "CloudTrail", icon_eye))

    detect_x = 280
    detect_labels = [("AWS Config", 40), ("Security Hub", 148), ("GuardDuty", 256)]
    for label, dy in detect_labels:
        services.append(service_box(detect_x, dy, 160, 64, label, icon_shield))
        flows.append(orthogonal_path(
            [(trail_x + 160, trail_y + 32), (detect_x - 24, trail_y + 32), (detect_x - 24, dy + 32), (detect_x, dy + 32)],
            "north-south", bidirectional=False))

    eb_x, eb_y = 560, 148
    services.append(service_box(eb_x, eb_y, 160, 64, "EventBridge Rule", icon_gateway,
                                 sub="mutating API call, NOT the pipeline role"))
    flows.append(orthogonal_path([(trail_x + 160, trail_y + 32), (eb_x, eb_y + 32)], "north-south", bidirectional=False))

    lambda_x, lambda_y = 840, 148
    services.append(service_box(lambda_x, lambda_y, 200, 64, "drift-remediator", icon_server,
                                 sub="tag-gated: ManagedBy=cdk skipped"))
    flows.append(orthogonal_path([(eb_x + 160, eb_y + 32), (lambda_x, lambda_y + 32)], "north-south", bidirectional=False))

    sns_x, sns_y = 1160, 148
    services.append(service_box(sns_x, sns_y, 160, 64, "SNS: governance-alerts", icon_bucket))
    flows.append(orthogonal_path([(lambda_x + 200, lambda_y + 32), (sns_x, sns_y + 32)], "north-south", bidirectional=False))

    email_x, email_y = 1400, 148
    services.append(service_box(email_x, email_y, 160, 64, "Email", icon_cloud, sub="config.REMEDIATION_EMAIL"))
    flows.append(orthogonal_path([(sns_x + 160, sns_y + 32), (email_x, email_y + 32)], "north-south", bidirectional=False))

    # Cross-cutting: permissions boundary + 5 CMKs, applied to every stack.
    boundaries.append(boundary(40, 260, 1520, 72, "SecurityStack -- 5 CMKs (logs/buckets/ebs/secrets/sns) + permissions boundary, applied project-wide", small=True))

    # ------------------------------------------------------------------
    # Auto-heal loop (auto_heal_stack.py) -- a second, independent
    # EventBridge -> Step Functions -> Lambda -> SNS flow, deterministic
    # remediation for 3 known failure modes rather than drift detection.
    # ------------------------------------------------------------------
    heal_y = 372
    boundaries.append(boundary(40, heal_y - 24, 1520, 148, "auto_heal_stack.py -- deterministic remediation (3 known failure modes, never a sweep)", small=True))

    alarm_x, alarm_w, alarm_gutter = 80, 180, 16
    alarm_labels = ["VPN tunnel down", "Lattice target unhealthy", "Bedrock throttling"]
    sfn_y = heal_y + 40
    for i, label in enumerate(alarm_labels):
        ax = alarm_x + i * (alarm_w + alarm_gutter)
        services.append(service_box(ax, sfn_y, alarm_w, 56, label, icon_eye))

    sfn_x = alarm_x + len(alarm_labels) * (alarm_w + alarm_gutter) + 24
    services.append(service_box(sfn_x, sfn_y, 200, 56, "Step Functions", icon_gateway, sub="Notify -> Remediate -> Notify"))
    for i in range(len(alarm_labels)):
        ax = alarm_x + i * (alarm_w + alarm_gutter)
        flows.append(orthogonal_path([(ax + alarm_w, sfn_y + 28), (sfn_x, sfn_y + 28)], "north-south", bidirectional=False))

    remediate_x = sfn_x + 240
    services.append(service_box(remediate_x, sfn_y, 200, 56, "auto-heal-remediate", icon_server, sub="1 named resource per type"))
    flows.append(orthogonal_path([(sfn_x + 200, sfn_y + 28), (remediate_x, sfn_y + 28)], "north-south", bidirectional=False))

    heal_sns_x = remediate_x + 240
    services.append(service_box(heal_sns_x, sfn_y, 200, 56, "SNS: governance-alerts", icon_bucket, sub="same topic as above -- before + after"))
    flows.append(orthogonal_path([(remediate_x + 200, sfn_y + 28), (heal_sns_x, sfn_y + 28)], "north-south", bidirectional=False))

    # ------------------------------------------------------------------
    # Governance metrics (governance_stack.py) -- a THIRD, independent loop:
    # Config/Security Hub/GuardDuty have no native "findings over time"
    # CloudWatch metric (confirmed via research), so a scheduled Lambda
    # polls all three and PutMetricData's a custom namespace the
    # lattice-lab dashboard graphs directly.
    # ------------------------------------------------------------------
    metrics_y = 596
    boundaries.append(boundary(40, metrics_y - 24, 1520, 96, "governance_stack.py -- custom dashboard metrics (every 15min)", small=True))

    schedule_x = 80
    services.append(service_box(schedule_x, metrics_y + 16, 200, 56, "EventBridge (rate 15min)", icon_gateway))

    metrics_lambda_x = schedule_x + 240
    services.append(service_box(metrics_lambda_x, metrics_y + 16, 220, 56, "governance-metrics-publisher", icon_server, sub="read-only Config/SecurityHub/GuardDuty"))
    flows.append(orthogonal_path([(schedule_x + 200, metrics_y + 44), (metrics_lambda_x, metrics_y + 44)], "north-south", bidirectional=False))
    for label, dy in detect_labels:
        flows.append(orthogonal_path(
            [(detect_x + 80, dy + 64), (detect_x + 80, metrics_y + 44), (metrics_lambda_x, metrics_y + 44)],
            "north-south", dashed=True, bidirectional=False))

    cw_metrics_x = metrics_lambda_x + 260
    services.append(service_box(cw_metrics_x, metrics_y + 16, 240, 56, "CloudWatch: LatticeLab/Governance", icon_bucket, sub="custom namespace, PutMetricData"))
    flows.append(orthogonal_path([(metrics_lambda_x + 220, metrics_y + 44), (cw_metrics_x, metrics_y + 44)], "north-south", bidirectional=False))

    dashboard_x = cw_metrics_x + 280
    services.append(service_box(dashboard_x, metrics_y + 16, 200, 56, "lattice-lab dashboard", icon_lb, sub="1 widget per architecture layer"))
    flows.append(orthogonal_path([(cw_metrics_x + 240, metrics_y + 44), (dashboard_x, metrics_y + 44)], "north-south", bidirectional=False))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south",)
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="gov-diagram-title gov-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="gov-diagram-title">Governance and drift-remediation flow</title>
  <desc id="gov-diagram-desc">CloudTrail feeds AWS Config, Security Hub, and GuardDuty for detection, and an EventBridge rule for mutating API calls made outside the pipeline. That rule triggers a Lambda which checks resource and principal tags before ever deleting anything, and publishes alerts to SNS by email throughout. A second, independent loop (auto_heal_stack.py) watches 3 named alarms -- VPN tunnel down, Lattice target unhealthy, Bedrock throttling -- and runs a Step Functions Notify-Remediate-Notify sequence against the one specific resource each alarm names, publishing to the same SNS topic before and after. A third, independent loop polls Config/Security Hub/GuardDuty every 15 minutes (none of the three publish a native "findings over time" CloudWatch metric) and writes a custom LatticeLab/Governance metric the lattice-lab dashboard graphs directly.</desc>
  <defs>{defs}</defs>
  <g id="gov-boundaries">{"".join(boundaries)}</g>
  <g id="gov-services">{"".join(services)}</g>
  <g id="gov-flows">{"".join(flows)}</g>
</svg>'''


def build_cloudwan_svg() -> str:
    """Third, independent diagram: the multi-region Cloud WAN layer
    (cloudwan_stack.py / region2_stack.py), gated behind ENABLE_CLOUDWAN."""
    w, h = 1600, 520
    boundaries, services, flows = [], [], []

    boundaries.append(boundary(40, 40, 900, 440, "us-east-1 (home region)"))
    boundaries.append(boundary(1000, 40, 560, 440, "us-east-2"))

    core_x, core_y, core_w, core_h = 80, 88, 820, 120
    boundaries.append(boundary(core_x, core_y, core_w, core_h, "Cloud WAN Core Network", small=True))
    seg_names = ["FastTrack", "SkyPath", "SkyTransit", "Workload"]
    seg_w, seg_gutter = 190, 16
    seg_x0 = core_x + 20
    seg_y = core_y + 48
    seg_centers = {}
    for i, name in enumerate(seg_names):
        sx = seg_x0 + i * (seg_w + seg_gutter)
        sub = "isolated" if name == "Workload" else None
        services.append(service_box(sx, seg_y, seg_w, 56, name, icon_shield, sub=sub))
        seg_centers[name] = (sx + seg_w // 2, seg_y + 56)

    app_x, app_y = 120, 280
    services.append(service_box(app_x, app_y, 200, 64, "app-vpc", icon_server, sub="segment=Workload"))
    flows.append(orthogonal_path([seg_centers["Workload"], (app_x + 100, app_y)], "north-south", bidirectional=False))

    provider_x, provider_y = 400, 280
    services.append(service_box(provider_x, provider_y, 200, 64, "provider-vpc", icon_server, sub="segment=FastTrack"))
    flows.append(orthogonal_path([seg_centers["FastTrack"], (provider_x + 100, provider_y)], "north-south", bidirectional=False))

    tgw_x, tgw_y = 680, 280
    services.append(service_box(tgw_x, tgw_y, 200, 64, "Existing TGW", icon_lb, sub="registered + peered"))
    flows.append(orthogonal_path([seg_centers["SkyTransit"], (tgw_x + 100, tgw_y)], "north-south", dashed=True, bidirectional=False))
    flows.append(f'<text x="{tgw_x - 4}" y="{tgw_y + 100}" class="label-service-sub">Peering only -- route-table attachment into ' +
                 'SkyTransit unresolved (AWS-side, see cloudwan_stack.py)</text>')

    share_y = seg_y + 56 + 24
    share_x = (seg_centers["SkyTransit"][0] + seg_centers["FastTrack"][0]) // 2
    flows.append(orthogonal_path(
        [(seg_centers["SkyTransit"][0], share_y), (seg_centers["FastTrack"][0], share_y)], "east-west", dashed=True))
    flows.append(f'<text x="{share_x - 60}" y="{share_y + 16}" class="flow-label">segment-actions: share SkyTransit -&gt; FastTrack</text>')

    region2_x, region2_y = 1040, 280
    services.append(service_box(region2_x, region2_y, 200, 64, "region2-prod-vpc", icon_server, sub="segment=Workload"))
    ssm_x, ssm_y = 1320, 280
    services.append(service_box(ssm_x, ssm_y, 160, 64, "SSM test instance", icon_server, sub="no NAT/IGW"))
    flows.append(orthogonal_path([(region2_x + 200, region2_y + 32), (ssm_x, ssm_y + 32)], "east-west", bidirectional=False))

    edge_y = core_y + core_h // 2
    flows.append(orthogonal_path([(core_x + core_w, edge_y), (1000 + 40, edge_y)], "east-west"))
    flows.append(orthogonal_path([(1000 + 40, edge_y), (region2_x + 100, region2_y)], "north-south", bidirectional=False))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south", "east-west")
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="wan-diagram-title wan-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="wan-diagram-title">Multi-region AWS Cloud WAN (ENABLE_CLOUDWAN)</title>
  <desc id="wan-diagram-desc">A Cloud WAN core network spanning us-east-1 and us-east-2, with four segments -- FastTrack, SkyPath, SkyTransit, and Workload (isolated) -- app-vpc and provider-vpc attached in us-east-1, a second Workload VPC attached in us-east-2, and the existing Transit Gateway registered and peered with the core network as an incremental migration path. Route-table-level attachment into the SkyTransit segment is not included -- a confirmed AWS-side gap, documented in cloudwan_stack.py.</desc>
  <defs>{defs}</defs>
  <g id="wan-boundaries">{"".join(boundaries)}</g>
  <g id="wan-services">{"".join(services)}</g>
  <g id="wan-flows">{"".join(flows)}</g>
</svg>'''


def build_agentic_ai_svg() -> str:
    """Fourth, independent diagram: the Agentic AI layer (agentic_ai_stack.py),
    gated behind ENABLE_AI. Two personas sharing one Lambda code asset + tool
    surface but distinct IAM roles, three-tier memory, Bedrock AgentCore
    primitives (Runtime/Gateway-MCP/Memory/Identity), and the MCP tool
    Lambdas' external targets -- shown at the same abstraction level as
    build_cloudwan_svg() above (representative nodes, not every resource)."""
    w, h = 1600, 760
    boundaries, services, flows = [], [], []

    cognito_x, cognito_y = 80, 40
    services.append(service_box(cognito_x, cognito_y, 160, 56, "Cognito User Pool", icon_shield, sub="JWT authorizer"))
    api_x, api_y = 320, 40
    services.append(service_box(api_x, api_y, 200, 56, "API Gateway (HTTP)", icon_gateway))
    flows.append(orthogonal_path([(cognito_x + 160, cognito_y + 28), (api_x, api_y + 28)], "north-south", bidirectional=False))

    vpc_x, vpc_y, vpc_w, vpc_h = 40, 128, 1520, 288
    boundaries.append(boundary(vpc_x, vpc_y, vpc_w, vpc_h, "app-vpc (Private subnets)"))

    persona_y = vpc_y + 40
    persona_defs = [("network-operator", 80, "read-only tools only"), ("connectivity-planner", 400, "+ propose_connectivity")]
    persona_centers = {}
    for name, dx, sub in persona_defs:
        px = vpc_x + dx
        services.append(service_box(px, persona_y, 280, 64, f"agent-orchestrator ({name})", icon_bot, sub=sub))
        persona_centers[name] = (px + 140, persona_y + 64)
        flows.append(orthogonal_path([(api_x + 100, api_y + 56), (px + 140, persona_y)], "north-south", bidirectional=False))

    mem_y = persona_y + 104
    mem_defs = [("DynamoDB (working)", 80, "TTL + PITR + CMK"), ("S3 (durable)", 400, "append-only JSONL, versioned"), ("S3 Vectors + Knowledge Base (semantic)", 720, "Titan Embed v2, 1024-dim")]
    for label, dx, sub in mem_defs:
        mx = vpc_x + dx
        mw = 280 if dx < 700 else 340
        services.append(service_box(mx, mem_y, mw, 64, label, icon_bucket, sub=sub))
        for name in persona_centers:
            flows.append(orthogonal_path([persona_centers[name], (mx + mw // 2, mem_y)], "north-south", bidirectional=True))

    tools_x, tools_y = vpc_x + 1120, persona_y
    services.append(service_box(tools_x, tools_y, 360, mem_y + 64 - persona_y, "MCP tool Lambdas (x7)", icon_server,
                                 sub="query_kafka, search_memory, get_network_health,\nquery_governance, cloudwan_topology,\ndetect_anomalies, propose_connectivity"))
    for name in persona_centers:
        flows.append(orthogonal_path([persona_centers[name], (tools_x, persona_y + 32)], "east-west", bidirectional=True))

    agentcore_y = vpc_y + vpc_h + 40
    boundaries.append(boundary(vpc_x, agentcore_y, 900, 120, "Bedrock AgentCore"))
    ac_defs = [("Gateway (MCP)", 40), ("Memory", 260), ("Runtime x2", 480), ("Workload Identity", 700)]
    for label, dx in ac_defs:
        acx = vpc_x + dx
        services.append(service_box(acx, agentcore_y + 44, 180, 56, label, icon_bot))
    flows.append(orthogonal_path([(tools_x + 180, tools_y + (mem_y + 64 - persona_y)), (vpc_x + 130, agentcore_y + 44)], "north-south", dashed=True, bidirectional=True))
    flows.append(f'<text x="{vpc_x + 20}" y="{agentcore_y - 8}" class="flow-label">same Lambdas registered as real MCP Gateway targets</text>')

    ext_x = vpc_x + 1020
    ext_defs = [
        ("Lattice resource gateway", agentcore_y, False),
        ("Security Hub / Config", agentcore_y + 40, False),
        ("ALB target health / VPN", agentcore_y + 80, False),
        ("Cloud WAN core network", agentcore_y + 120, True),
        ("SageMaker anomaly table", agentcore_y + 160, True),
    ]
    for label, ey, conditional in ext_defs:
        services.append(service_box(ext_x, ey, 340, 32, label, None, dim=conditional))
        flows.append(orthogonal_path([(tools_x + 360, tools_y + 20), (ext_x, ey + 16)], "east-west", dashed=conditional, bidirectional=False))

    cc_x, cc_y = ext_x, agentcore_y + 200
    services.append(service_box(cc_x, cc_y, 340, 56, "CodeCommit (open a PR)", icon_bucket, sub="propose_connectivity -- never a direct mutation"))
    flows.append(orthogonal_path([(tools_x + 360, tools_y + 20), (cc_x, cc_y + 28)], "east-west", bidirectional=False))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south", "east-west")
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="ai-diagram-title ai-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="ai-diagram-title">Agentic AI layer (ENABLE_AI)</title>
  <desc id="ai-diagram-desc">A Cognito-authenticated API Gateway fronts two agent-orchestrator Lambda personas (network-operator, read-only; connectivity-planner, adds propose_connectivity) sharing three-tier memory (DynamoDB working, S3 durable, S3-Vectors-backed Knowledge Base semantic) and 7 MCP tool Lambdas, which are also registered as real Bedrock AgentCore Gateway MCP targets alongside a Memory, Runtime x2, and Workload Identity resource. The tool Lambdas read from Lattice, governance, network-health, and (when those layers are enabled) Cloud WAN and SageMaker resources, and the one tool with any write capability opens a CodeCommit pull request rather than ever mutating infrastructure directly.</desc>
  <defs>{defs}</defs>
  <g id="ai-boundaries">{"".join(boundaries)}</g>
  <g id="ai-services">{"".join(services)}</g>
  <g id="ai-flows">{"".join(flows)}</g>
</svg>'''


def build_sagemaker_svg() -> str:
    """Fifth, independent diagram: the SageMaker anomaly-detection layer
    (sagemaker_stack.py), gated behind ENABLE_SAGEMAKER. All 5 pipeline
    Lambdas are boto3/API-driven (there is no CloudFormation resource type
    for a training or transform job) -- see that stack's module docstring."""
    w, h = 1600, 360
    services, flows, boundaries = [], [], []

    vpc_x, vpc_y = 40, 128
    services.append(service_box(vpc_x, 40, 200, 56, "app-vpc Flow Logs", icon_eye))
    bucket_x = 320
    services.append(service_box(bucket_x, 40, 220, 56, "S3: raw/ -> processed/", icon_bucket))
    flows.append(orthogonal_path([(vpc_x + 200, 68), (bucket_x, 68)], "east-west", bidirectional=False))

    pre_x = 620
    services.append(service_box(pre_x, 40, 220, 56, "flow-log-preprocessor", icon_server, sub="daily -- numeric features -> CSV"))
    flows.append(orthogonal_path([(bucket_x + 220, 68), (pre_x, 68)], "east-west", bidirectional=False))

    train_x, train_y = 620, 152
    services.append(service_box(train_x, train_y, 220, 56, "rcf-trainer", icon_server, sub="weekly -- CreateTrainingJob (RCF)"))
    flows.append(orthogonal_path([(pre_x + 110, 96), (train_x + 110, train_y)], "north-south", bidirectional=False))

    promote_x = 900
    services.append(service_box(promote_x, train_y, 240, 56, "rcf-model-promoter", icon_server, sub="on training completion"))
    flows.append(orthogonal_path([(train_x + 220, train_y + 28), (promote_x, train_y + 28)], "east-west", dashed=True, bidirectional=False))
    flows.append(f'<text x="{train_x + 224}" y="{train_y - 6}" class="flow-label">EventBridge: Training Job State Change</text>')

    endpoint_x = 1220
    services.append(service_box(endpoint_x, train_y, 240, 56, "Async Inference Endpoint", icon_lb, sub="scale-to-zero autoscaling"))
    flows.append(orthogonal_path([(promote_x + 240, train_y + 28), (endpoint_x, train_y + 28)], "east-west", bidirectional=False))

    scorer_x, scorer_y = 900, 264
    services.append(service_box(scorer_x, scorer_y, 240, 56, "rcf-batch-scorer", icon_server, sub="every 6h -- CreateTransformJob"))
    flows.append(orthogonal_path([(promote_x + 120, train_y + 56), (scorer_x + 120, scorer_y)], "north-south", dashed=True, bidirectional=False))
    flows.append(f'<text x="{scorer_x - 4}" y="{scorer_y - 6}" class="label-service-sub">uses the currently-promoted model</text>')

    findings_x = 1220
    services.append(service_box(findings_x, scorer_y, 240, 56, "rcf-findings-processor", icon_server, sub="on transform completion"))
    flows.append(orthogonal_path([(scorer_x + 240, scorer_y + 28), (findings_x, scorer_y + 28)], "east-west", dashed=True, bidirectional=False))

    dynamo_y = 128
    services.append(service_box(40, dynamo_y, 200, 56, "DynamoDB findings", icon_bucket, sub="read by detect_anomalies"))
    sns_s3_y = dynamo_y + 72
    services.append(service_box(40, sns_s3_y, 200, 48, "SNS + S3", icon_bucket))
    flows.append(orthogonal_path([(findings_x + 120, scorer_y + 56), (findings_x + 120, sns_s3_y + 24), (240, sns_s3_y + 24)], "north-south", dashed=True, bidirectional=False))

    boundaries.append(boundary(40, sns_s3_y + 72, 220, 72, "teardown_cleanup (on cdk destroy only)", small=True))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south", "east-west")
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="sm-diagram-title sm-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="sm-diagram-title">SageMaker anomaly-detection layer (ENABLE_SAGEMAKER)</title>
  <desc id="sm-diagram-desc">VPC Flow Logs land in S3 and get preprocessed into numeric-feature CSV daily; a weekly training job produces a new Random Cut Forest model generation, which a promoter Lambda swaps onto a scale-to-zero Async Inference endpoint; a separate 6-hourly batch-transform job scores accumulated data in bulk with whichever model is currently promoted, and a findings processor turns qualifying anomaly scores into DynamoDB + S3 + SNS output that agentic_ai_stack.py's detect_anomalies MCP tool reads. Because training/transform jobs aren't CloudFormation resources, a teardown custom resource sweeps every runtime-created endpoint/model/config on `cdk destroy`.</desc>
  <defs>{defs}</defs>
  <g id="sm-services">{"".join(services)}</g>
  <g id="sm-boundaries">{"".join(boundaries)}</g>
  <g id="sm-flows">{"".join(flows)}</g>
</svg>'''


CSS = """
:root {
  --bg: #f7f8fa;
  --surface: #ffffff;
  --surface-tint: #eef1f5;
  --text: #1a1f29;
  --text-muted: #5b6472;
  --boundary-stroke: #9aa4b2;
  --box-stroke: #3d4a5c;
  --flow-north-south: #1f6feb;
  --flow-east-west: #b35c00;
  --flow-mgmt: #6e7681;
  --flow-meta: #8250df;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --surface-tint: #1c2431;
    --text: #e6edf3;
    --text-muted: #9ba7b4;
    --boundary-stroke: #4b5563;
    --box-stroke: #8b98a9;
    --flow-north-south: #58a6ff;
    --flow-east-west: #ffa657;
    --flow-mgmt: #8b949e;
    --flow-meta: #d2a8ff;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.page { max-width: 1400px; margin: 0 auto; padding: 0 24px 48px; }
header { padding-top: 32px; }
h1 { font-size: 24px; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 18px; font-weight: 600; margin: 32px 0 4px; }
.subtitle { font-size: 14px; font-weight: 400; color: var(--text-muted); margin: 0 0 20px; }
nav.legend { list-style: none; padding: 0; margin: 0 0 20px; display: flex; flex-wrap: wrap; gap: 16px 24px; }
nav.legend ul { list-style: none; padding: 0; margin: 0; display: flex; flex-wrap: wrap; gap: 16px 24px; }
nav.legend li { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 500; }
.swatch { width: 20px; height: 3px; border-radius: 2px; display: inline-block; }
.swatch.dashed { background: none; border-top: 3px dashed currentColor; height: 0; }
.swatch.dotted { background: none; border-top: 3px dotted currentColor; height: 0; }
.swatch-ns { background: var(--flow-north-south); color: var(--flow-north-south); }
.swatch-ew { background: var(--flow-east-west); color: var(--flow-east-west); }
.swatch-mgmt { color: var(--flow-mgmt); }
.swatch-meta { color: var(--flow-meta); }
.swatch-boundary { color: var(--boundary-stroke); }
figure { margin: 0; }
.diagram-scroll { overflow-x: auto; overflow-y: hidden; -webkit-overflow-scrolling: touch; border: 1px solid var(--surface-tint); border-radius: 8px; background: var(--surface); position: relative; }
svg { display: block; width: 100%; min-width: 900px; height: auto; }
footer { margin-top: 16px; font-size: 12px; color: var(--text-muted); }

.box-boundary { fill: none; stroke: var(--boundary-stroke); stroke-width: 2; stroke-dasharray: 10 6; }
.box-boundary-small { fill: none; stroke: var(--boundary-stroke); stroke-width: 2; stroke-dasharray: 8 5; }
.box-swimlane { fill: var(--surface-tint); fill-opacity: 0.5; stroke: var(--boundary-stroke); stroke-width: 1.5; stroke-dasharray: 6 4; }
.box-subnet-tier { fill: var(--surface); stroke: var(--boundary-stroke); stroke-width: 1; }
.box-service { fill: var(--surface); stroke: var(--box-stroke); stroke-width: 1.5; }
.box-service-dim { fill: var(--surface-tint); stroke: var(--box-stroke); stroke-width: 1.5; }
.box-firewall { stroke-width: 2; }
.box-meta-panel { fill: var(--surface); stroke: var(--flow-meta); stroke-width: 1.5; stroke-dasharray: 3 4; }

.label-boundary { font-size: 13px; font-weight: 600; fill: var(--text); text-transform: uppercase; letter-spacing: 0.5px; }
.label-swimlane { font-size: 12px; font-weight: 600; fill: var(--text); text-transform: uppercase; }
.label-subnet-tier { font-size: 11px; font-weight: 500; fill: var(--text-muted); }
.label-service-title { font-size: 13px; font-weight: 600; fill: var(--text); }
.label-service-sub { font-size: 10px; font-weight: 500; fill: var(--text-muted); }
.label-meta-caption { font-size: 11px; font-weight: 500; fill: var(--text-muted); font-style: italic; }
.flow-label { font-size: 10px; font-weight: 500; fill: var(--flow-north-south); }
.icon { color: var(--text-muted); }

.firewall-sublabel {
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  fill: var(--text-muted);
  opacity: 0.85;
}

.flow { fill: none; stroke-width: 2; }
.flow-north-south { stroke: var(--flow-north-south); }
.flow-east-west { stroke: var(--flow-east-west); stroke-dasharray: 14 4; }
.flow-mgmt { stroke: var(--flow-mgmt); stroke-width: 1.5; stroke-dasharray: 3 3; }
.flow-meta { stroke: var(--flow-meta); stroke-width: 1.5; stroke-dasharray: 5 4; }
.marker-north-south { fill: var(--flow-north-south); }
.marker-east-west { fill: var(--flow-east-west); }
.marker-mgmt { fill: var(--flow-mgmt); }
.marker-meta { fill: var(--flow-meta); }

#dev-grid { display: none; }
"""


def build_html() -> str:
    svg = build_svg()
    gov_svg = build_governance_svg()
    wan_svg = build_cloudwan_svg()
    ai_svg = build_agentic_ai_svg()
    sagemaker_svg = build_sagemaker_svg()
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Centralized Traffic Inspection Architecture -- lattice-lab</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
<header>
  <h1>Centralized Traffic Inspection Architecture</h1>
  <p class="subtitle">lattice-lab &middot; inspection-vpc &middot; {AZ_COUNT} Availability Zones &middot; {FIREWALL_APPLIANCES_PER_AZ} firewall appliances per AZ</p>
</header>
<nav class="legend" aria-label="Diagram legend">
  <ul>
    <li><span class="swatch swatch-ns"></span> North-South &mdash; Internet ingress/egress</li>
    <li><span class="swatch swatch-ew"></span> East-West &mdash; Inter-VPC via Transit Gateway</li>
    <li><span class="swatch dashed swatch-mgmt"></span> Management / Control Plane</li>
    <li><span class="swatch dashed swatch-meta"></span> Page Delivery (CloudFront + S3)</li>
    <li><span class="swatch dashed swatch-boundary"></span> VPC / AZ boundary</li>
  </ul>
</nav>
<main>
  <figure>
    <div class="diagram-scroll">
      {svg}
    </div>
  </figure>

  <h2>Governance and Drift-Remediation</h2>
  <p class="subtitle">security_stack.py &middot; governance_stack.py &middot; drift_remediation_stack.py</p>
  <figure>
    <div class="diagram-scroll">
      {gov_svg}
    </div>
  </figure>

  <h2>Multi-Region AWS Cloud WAN</h2>
  <p class="subtitle">cloudwan_stack.py &middot; region2_stack.py &middot; gated behind ENABLE_CLOUDWAN (off by default -- see README.md's cost warning)</p>
  <figure>
    <div class="diagram-scroll">
      {wan_svg}
    </div>
  </figure>

  <h2>Agentic AI Layer</h2>
  <p class="subtitle">agentic_ai_stack.py &middot; gated behind ENABLE_AI (off by default -- see README.md's cost warning)</p>
  <figure>
    <div class="diagram-scroll">
      {ai_svg}
    </div>
  </figure>

  <h2>SageMaker Anomaly Detection</h2>
  <p class="subtitle">sagemaker_stack.py &middot; gated behind ENABLE_SAGEMAKER (off by default -- see README.md's cost warning)</p>
  <figure>
    <div class="diagram-scroll">
      {sagemaker_svg}
    </div>
  </figure>
</main>
<footer>
  The CloudFront/S3 panel (top right of the first diagram) shows how this page is delivered to your browser &mdash; it is not part of the depicted network.
</footer>
</div>
</body>
</html>
'''


if __name__ == "__main__":
    out_path = Path(__file__).parent / "index.html"
    out_path.write_text(build_html())
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")
