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

from aws_icons import aws_icon, aws_icon_defs

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
MGMT_X, MGMT_Y, MGMT_W, MGMT_H = 280, 800, 180, 56
META_X, META_Y, META_W, META_H = 1160, 80, 400, 176

FIREWALL_W, FIREWALL_H = 200, 104
GWLB_EP_W, GWLB_EP_H = 120, 40

# on-prem (simulated) + Site-to-Site VPN, in the clear band below the
# Inspection VPC boundary (VPC bottom = VPC_Y + VPC_H = 760) and to the
# right of MGMT (which starts at MGMT_X=280) -- placed at x=480 so the
# connector's horizontal run at y=770 (clear corridor: below the VPC's
# 760 bottom edge, above MGMT/onprem's own 784-800 tops) never crosses
# any swimlane, tier, or the MGMT box.
ONPREM_X, ONPREM_Y, ONPREM_W, ONPREM_H = 480, 784, 300, 140


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


def icon_eye(cx: int, cy: int) -> str:
    return (f'<g transform="translate({cx-11},{cy-7})" class="icon">'
            f'<path d="M0 7 C3 1 19 1 22 7 C19 13 3 13 0 7 Z" fill="none" stroke="currentColor" stroke-width="1.4"/>'
            f'<circle cx="11" cy="7" r="3" fill="none" stroke="currentColor" stroke-width="1.4"/>'
            f'</g>')


# Two distinct internal layouts, picked automatically by service_box() --
# neither depends on box height h being any particular value, so panels
# never need their hand-tuned row math touched to accommodate a label:
#
#  * icon + title, no sub: icon centered on top, title centered below it
#    (a compact "badge" -- unchanged from the original design).
#  * icon + title + sub: icon at the LEFT, vertically centered, title/sub
#    stacked as a left-aligned two-line block to its right (a "list row" --
#    the standard icon-left pattern from Stripe/Linear/Vercel-style docs
#    UI). This is what actually fixes the title/sub collision bug: a
#    two-line text block only needs the box's own height to fit two lines
#    of text, not "icon height + gap + two lines," so it works at any
#    normal box height instead of forcing every box taller.
ICON_SIZE = 28
ICON_SIZE_ROW = 24            # slightly smaller for the icon-left row layout
BADGE_ICON_CY_OFFSET = 22     # icon center, measured from box top (badge layout)
BADGE_TITLE_Y = 50            # title baseline, measured from box top (badge layout)


def service_box(x, y, w, h, title, icon=None, sub=None, dim=False) -> str:
    """icon may be a real AWS icon name (str, looked up in aws_icons.ICONS
    and rendered as the official/matching-style colored badge) or one of the
    two plain line-glyph functions above (icon_cloud / icon_eye) for the
    handful of non-AWS conceptual nodes (Internet, a human Viewer) -- an AWS
    icon there would misleadingly imply those are AWS resources."""
    parts = [f'<g class="stamp-service">',
             f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" class="{"box-service-dim" if dim else "box-service"}"/>']

    if icon and sub:
        # icon-left row layout
        icon_cx = x + 16 + ICON_SIZE_ROW // 2
        icon_cy = y + h // 2
        if isinstance(icon, str):
            parts.append(aws_icon(icon_cx, icon_cy, ICON_SIZE_ROW, icon))
        elif callable(icon):
            parts.append(icon(icon_cx, icon_cy))
        text_x = x + 16 + ICON_SIZE_ROW + 12
        parts.append(f'<text x="{text_x}" y="{y + h//2 - 4}" class="label-service-title">{esc(title)}</text>')
        parts.append(f'<text x="{text_x}" y="{y + h//2 + 12}" class="label-service-sub">{esc(sub)}</text>')
    elif icon:
        # centered badge layout (title only, no sub)
        icon_cx = x + w // 2
        if isinstance(icon, str):
            parts.append(aws_icon(icon_cx, y + BADGE_ICON_CY_OFFSET, ICON_SIZE, icon))
        elif callable(icon):
            parts.append(icon(icon_cx, y + BADGE_ICON_CY_OFFSET))
        parts.append(f'<text x="{icon_cx}" y="{y + BADGE_TITLE_Y}" class="label-service-title" text-anchor="middle">{esc(title)}</text>')
    else:
        # no icon at all -- centered title, optional centered sub below it
        title_y = y + h // 2 + (4 if not sub else -2)
        parts.append(f'<text x="{x + w//2}" y="{title_y}" class="label-service-title" text-anchor="middle">{esc(title)}</text>')
        if sub:
            parts.append(f'<text x="{x + w//2}" y="{title_y + 16}" class="label-service-sub" text-anchor="middle">{esc(sub)}</text>')

    parts.append('</g>')
    return "\n".join(parts)


def firewall_box(x, y, label_suffix: str = "") -> str:
    w, h = FIREWALL_W, FIREWALL_H
    title = FIREWALL_TITLE + (f" {label_suffix}" if label_suffix else "")
    return (
        f'<g class="stamp-firewall">'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" class="box-service box-firewall"/>'
        f'{aws_icon(x + w//2, y + 20, ICON_SIZE, "network-firewall")}'
        f'<text x="{x + w//2}" y="{y + 46}" class="label-service-title" text-anchor="middle">{esc(title)}</text>'
        f'<text x="{x + w//2}" y="{y + 66}" class="firewall-sublabel" text-anchor="middle">{esc(FIREWALL_SUBLABEL_LINE1)}</text>'
        f'<text x="{x + w//2}" y="{y + 78}" class="firewall-sublabel" text-anchor="middle">{esc(FIREWALL_SUBLABEL_LINE2)}</text>'
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
    services.append(service_box(igw_x, igw_y, INTERNET_COL_W, SERVICE_BOX_H, "Internet Gateway", "internet-gateway"))

    # ---- Inspection VPC boundary (node 3) ----
    boundaries.append(boundary(VPC_X, VPC_Y, VPC_W, VPC_H, "INSPECTION VPC"))

    # ---- GWLB band (node 4) ----
    services.append(service_box(GWLB_BAND_X, GWLB_BAND_Y, GWLB_BAND_W, GWLB_BAND_H, "Gateway Load Balancer", "gwlb"))

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
    services.append(service_box(TGW_X, TGW_Y, TGW_W, TGW_H, "Transit Gateway", "transit-gateway"))

    # ---- Workload spokes (node 14) ----
    boundaries.append(boundary(SPOKE_X, SPOKE_A_Y, SPOKE_W, SPOKE_A_H, "APP-VPC (SPOKE)", small=True))
    boundaries.append(boundary(SPOKE_X, SPOKE_B_Y, SPOKE_W, SPOKE_B_H, "PROVIDER-VPC (SPOKE)", small=True))

    # ---- Management & Logging (node 15) ----
    services.append(service_box(MGMT_X, MGMT_Y, MGMT_W, MGMT_H, "Management & Logging", "cloudwatch"))

    # ---- On-prem (simulated) + Site-to-Site VPN (network_stack.py's
    # onprem-vpc/libreswan) -- the network-layer hybrid-connectivity path,
    # distinct from VPC Lattice's app-layer Resource Gateway path shown in
    # the Application Path panel below.
    boundaries.append(boundary(ONPREM_X, ONPREM_Y, ONPREM_W, ONPREM_H, "ON-PREM (SIMULATED)", small=True))
    services.append(service_box(ONPREM_X + 20, ONPREM_Y + 40, ONPREM_W - 40, 72,
                                 "Customer Gateway", "vpn-gateway", sub="libreswan -- TCP/9092"))

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

    # H: on-prem <-> TGW (Site-to-Site VPN, static routes). Routed through
    # the clear corridor at y=770 -- below the VPC's own bottom edge (760)
    # and above MGMT/on-prem's tops (800/784) -- so it never crosses a
    # swimlane, tier, or the MGMT box; final vertical run at x=1240 is
    # inside TGW's own horizontal span, approaching its bottom edge.
    onprem_top_x = ONPREM_X + ONPREM_W // 2
    vpn_channel_y = 770
    vpn_tgw_x = TGW_X + 80
    flows_ew.append(orthogonal_path(
        [(onprem_top_x, ONPREM_Y), (onprem_top_x, vpn_channel_y), (vpn_tgw_x, vpn_channel_y), (vpn_tgw_x, TGW_Y + TGW_H)],
        "east-west", dashed=True))
    flows_ew.append(f'<text x="{onprem_top_x + 12}" y="{vpn_channel_y - 6}" class="flow-label">Site-to-Site VPN (2 tunnels, static routes)</text>')

    # ---- Meta panel: CloudFront/S3 page-delivery (nodes 17-19, edge H) ----
    meta.append(f'<rect x="{META_X}" y="{META_Y}" width="{META_W}" height="{META_H}" rx="10" class="box-meta-panel"/>')
    meta.append(f'<text x="{META_X + 16}" y="{META_Y - 8}" class="label-meta-caption">How this page is delivered</text>')
    viewer_x, viewer_y = META_X + 24, META_Y + 40
    cf_x, cf_y = META_X + 160, META_Y + 40
    s3_x, s3_y = META_X + 296, META_Y + 40
    meta.append(service_box(viewer_x, viewer_y - 20, 96, 56, "Viewer", icon_eye))
    meta.append(service_box(cf_x, cf_y - 20, 96, 56, "CloudFront", "cloudfront"))
    meta.append(service_box(s3_x, s3_y - 20, 96, 56, "S3 (origin)", "s3"))
    meta.append(orthogonal_path([(viewer_x + 96, viewer_y + 8), (cf_x, cf_y + 8)], "meta"))
    meta.append(orthogonal_path([(cf_x + 96, cf_y + 8), (s3_x, s3_y + 8)], "meta"))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south", "east-west", "mgmt", "meta")
    )

    return f'''<svg viewBox="0 0 {VIEWBOX_W} {VIEWBOX_H}" role="img" aria-labelledby="diagram-title diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="diagram-title">Centralized traffic-inspection architecture</title>
  <desc id="diagram-desc">Internet traffic enters through an Internet Gateway into an Inspection VPC, where a Gateway Load Balancer distributes it across a Linux EC2 firewall fleet (two appliances per Availability Zone) for inspection before continuing to a NAT Gateway for egress or, for east-west traffic, back through a Transit Gateway to spoke VPCs. A simulated on-premises network reaches the same Transit Gateway over a static-route Site-to-Site VPN (2 tunnels) -- the network-layer hybrid-connectivity path, distinct from VPC Lattice's app-layer Resource Gateway shown in the Application Path panel. A separate panel shows how this page itself is delivered via CloudFront and S3.</desc>
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
    """Second, independent diagram: SecurityStack's project-wide primitives,
    the recording/detection plane, and the EventBridge -> Lambda -> SNS
    auto-remediation flow (security_stack.py / governance_stack.py /
    drift_remediation_stack.py). Deliberately its own small SVG, not grafted
    onto build_svg()'s inspection-flow canvas above -- that one is governed
    by docs/inspection-architecture-reference.md Section 6's exact
    grid/assertion spec for a single, narrow purpose (the GWLB/firewall/TGW
    traffic path); this is a different concern entirely."""
    w, h = 1600, 732
    services, flows, boundaries = [], [], []

    trail_x, trail_y = 40, 148
    services.append(service_box(trail_x, trail_y, 160, 64, "CloudTrail", "cloudtrail"))

    detect_x = 280
    detect_labels = [("AWS Config", 40), ("Security Hub", 148), ("GuardDuty", 256)]
    detect_icons = {"AWS Config": "config", "Security Hub": "security-hub", "GuardDuty": "guardduty"}
    for label, dy in detect_labels:
        services.append(service_box(detect_x, dy, 160, 64, label, detect_icons[label]))
        flows.append(orthogonal_path(
            [(trail_x + 160, trail_y + 32), (detect_x - 24, trail_y + 32), (detect_x - 24, dy + 32), (detect_x, dy + 32)],
            "north-south", bidirectional=False))

    eb_x, eb_y = 560, 148
    services.append(service_box(eb_x, eb_y, 160, 64, "EventBridge", "eventbridge",
                                 sub="non-pipeline"))
    flows.append(orthogonal_path([(trail_x + 160, trail_y + 32), (eb_x, eb_y + 32)], "north-south", bidirectional=False))

    lambda_x, lambda_y = 840, 148
    services.append(service_box(lambda_x, lambda_y, 200, 64, "drift-remediator", "lambda",
                                 sub="tag-gated"))
    flows.append(orthogonal_path([(eb_x + 160, eb_y + 32), (lambda_x, lambda_y + 32)], "north-south", bidirectional=False))

    sns_x, sns_y = 1160, 148
    services.append(service_box(sns_x, sns_y, 160, 64, "governance-alerts", "sns"))
    flows.append(orthogonal_path([(lambda_x + 200, lambda_y + 32), (sns_x, sns_y + 32)], "north-south", bidirectional=False))

    email_x, email_y = 1400, 148
    services.append(service_box(email_x, email_y, 160, 64, "Email", icon_cloud, sub="config.REMEDIATION_EMAIL"))
    flows.append(orthogonal_path([(sns_x + 160, sns_y + 32), (email_x, email_y + 32)], "north-south", bidirectional=False))

    # Cross-cutting: security_stack.py's own primitives, applied to every
    # other stack as constructor props (5 CMKs, permissions boundary, IAM
    # Access Analyzer, the project-wide S3 access-logs bucket) -- previously
    # just a text label on this boundary bar; now the real resources.
    sec_y = 260
    boundaries.append(boundary(40, sec_y, 1520, 104,
                                "SecurityStack -- applied to every other stack as constructor props", small=True))
    sec_items = [
        ("KMS -- 5 CMKs", "kms", "logs / buckets / ebs / secrets / sns"),
        ("Permissions Boundary", "iam", "hard ceiling on every project role"),
        ("IAM Access Analyzer", "iam", "flags external-principal grants"),
        ("S3 access-logs bucket", "s3", "project-wide server-access-log sink"),
    ]
    sec_w, sec_gutter = 340, 27
    for i, (label, icon, sub) in enumerate(sec_items):
        sx = 80 + i * (sec_w + sec_gutter)
        services.append(service_box(sx, sec_y + 28, sec_w, 64, label, icon, sub=sub))

    # ------------------------------------------------------------------
    # Auto-heal loop (auto_heal_stack.py) -- a second, independent
    # EventBridge -> Step Functions -> Lambda -> SNS flow, deterministic
    # remediation for 3 known failure modes rather than drift detection.
    # ------------------------------------------------------------------
    heal_y = 412
    boundaries.append(boundary(40, heal_y - 24, 1520, 148, "auto_heal_stack.py -- deterministic remediation (3 known failure modes, never a sweep)", small=True))

    alarm_x, alarm_w, alarm_gutter = 80, 180, 16
    alarm_labels = ["VPN tunnel down", "Lattice unhealthy", "Bedrock throttling"]
    sfn_y = heal_y + 40
    for i, label in enumerate(alarm_labels):
        ax = alarm_x + i * (alarm_w + alarm_gutter)
        services.append(service_box(ax, sfn_y, alarm_w, 56, label, "cloudwatch"))

    sfn_x = alarm_x + len(alarm_labels) * (alarm_w + alarm_gutter) + 24
    services.append(service_box(sfn_x, sfn_y, 200, 56, "Step Functions", "step-functions", sub="notify / fix / notify"))
    for i in range(len(alarm_labels)):
        ax = alarm_x + i * (alarm_w + alarm_gutter)
        flows.append(orthogonal_path([(ax + alarm_w, sfn_y + 28), (sfn_x, sfn_y + 28)], "north-south", bidirectional=False))

    remediate_x = sfn_x + 240
    services.append(service_box(remediate_x, sfn_y, 200, 56, "auto-heal-fix", "lambda", sub="1 resource per type"))
    flows.append(orthogonal_path([(sfn_x + 200, sfn_y + 28), (remediate_x, sfn_y + 28)], "north-south", bidirectional=False))

    heal_sns_x = remediate_x + 240
    services.append(service_box(heal_sns_x, sfn_y, 200, 56, "governance-alerts", "sns", sub="before + after"))
    flows.append(orthogonal_path([(remediate_x + 200, sfn_y + 28), (heal_sns_x, sfn_y + 28)], "north-south", bidirectional=False))

    # ------------------------------------------------------------------
    # Governance metrics (governance_stack.py) -- a THIRD, independent loop:
    # Config/Security Hub/GuardDuty have no native "findings over time"
    # CloudWatch metric (confirmed via research), so a scheduled Lambda
    # polls all three and PutMetricData's a custom namespace the
    # lattice-lab dashboard graphs directly.
    # ------------------------------------------------------------------
    metrics_y = 636
    boundaries.append(boundary(40, metrics_y - 24, 1520, 96, "governance_stack.py -- custom dashboard metrics (every 15min)", small=True))

    schedule_x = 80
    services.append(service_box(schedule_x, metrics_y + 16, 200, 56, "EventBridge (rate 15min)", "eventbridge"))

    metrics_lambda_x = schedule_x + 240
    services.append(service_box(metrics_lambda_x, metrics_y + 16, 220, 56, "metrics-publisher", "lambda", sub="read-only poller"))
    flows.append(orthogonal_path([(schedule_x + 200, metrics_y + 44), (metrics_lambda_x, metrics_y + 44)], "north-south", bidirectional=False))
    for label, dy in detect_labels:
        flows.append(orthogonal_path(
            [(detect_x + 80, dy + 64), (detect_x + 80, metrics_y + 44), (metrics_lambda_x, metrics_y + 44)],
            "north-south", dashed=True, bidirectional=False))

    cw_metrics_x = metrics_lambda_x + 260
    services.append(service_box(cw_metrics_x, metrics_y + 16, 240, 56, "LatticeLab/Governance", "cloudwatch", sub="custom CW namespace"))
    flows.append(orthogonal_path([(metrics_lambda_x + 220, metrics_y + 44), (cw_metrics_x, metrics_y + 44)], "north-south", bidirectional=False))

    dashboard_x = cw_metrics_x + 280
    services.append(service_box(dashboard_x, metrics_y + 16, 200, 56, "lab dashboard", "cloudwatch", sub="1 widget per layer"))
    flows.append(orthogonal_path([(cw_metrics_x + 240, metrics_y + 44), (dashboard_x, metrics_y + 44)], "north-south", bidirectional=False))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south",)
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="gov-diagram-title gov-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="gov-diagram-title">Security, governance, and drift-remediation</title>
  <desc id="gov-diagram-desc">CloudTrail feeds AWS Config, Security Hub, and GuardDuty for detection, and an EventBridge rule for mutating API calls made outside the pipeline. That rule triggers a Lambda which checks resource and principal tags before ever deleting anything, and publishes alerts to SNS by email throughout. Underpinning all of it, SecurityStack supplies 5 customer-managed KMS keys, a permissions-boundary policy applied as a hard ceiling on every project IAM role, an IAM Access Analyzer, and the project-wide S3 access-log sink -- passed into every other stack as constructor props. A second, independent loop (auto_heal_stack.py) watches 3 named alarms -- VPN tunnel down, Lattice target unhealthy, Bedrock throttling -- and runs a Step Functions Notify-Remediate-Notify sequence against the one specific resource each alarm names, publishing to the same SNS topic before and after. A third, independent loop polls Config/Security Hub/GuardDuty every 15 minutes (none of the three publish a native "findings over time" CloudWatch metric) and writes a custom LatticeLab/Governance metric the lattice-lab dashboard graphs directly.</desc>
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
        services.append(service_box(sx, seg_y, seg_w, 56, name, "cloud-wan", sub=sub))
        seg_centers[name] = (sx + seg_w // 2, seg_y + 56)

    app_x, app_y = 120, 280
    services.append(service_box(app_x, app_y, 200, 64, "app-vpc", "vpc", sub="segment=Workload"))
    flows.append(orthogonal_path([seg_centers["Workload"], (app_x + 100, app_y)], "north-south", bidirectional=False))

    provider_x, provider_y = 400, 280
    services.append(service_box(provider_x, provider_y, 200, 64, "provider-vpc", "vpc", sub="segment=FastTrack"))
    flows.append(orthogonal_path([seg_centers["FastTrack"], (provider_x + 100, provider_y)], "north-south", bidirectional=False))

    tgw_x, tgw_y = 680, 280
    services.append(service_box(tgw_x, tgw_y, 200, 64, "Existing TGW", "transit-gateway", sub="registered + peered"))
    flows.append(orthogonal_path([seg_centers["SkyTransit"], (tgw_x + 100, tgw_y)], "north-south", dashed=True, bidirectional=False))
    flows.append(f'<text x="{tgw_x - 4}" y="{tgw_y + 100}" class="label-service-sub">Peering only -- route-table attachment into ' +
                 'SkyTransit unresolved (AWS-side, see cloudwan_stack.py)</text>')

    share_y = seg_y + 56 + 24
    share_x = (seg_centers["SkyTransit"][0] + seg_centers["FastTrack"][0]) // 2
    flows.append(orthogonal_path(
        [(seg_centers["SkyTransit"][0], share_y), (seg_centers["FastTrack"][0], share_y)], "east-west", dashed=True))
    flows.append(f'<text x="{share_x - 60}" y="{share_y + 16}" class="flow-label">segment-actions: share SkyTransit -&gt; FastTrack</text>')

    region2_x, region2_y = 1040, 280
    services.append(service_box(region2_x, region2_y, 200, 64, "region2-prod-vpc", "vpc", sub="segment=Workload"))
    ssm_x, ssm_y = 1320, 280
    services.append(service_box(ssm_x, ssm_y, 180, 64, "SSM test host", "ec2", sub="no NAT/IGW"))
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
    services.append(service_box(cognito_x, cognito_y, 160, 56, "Cognito", "cognito", sub="JWT authorizer"))
    api_x, api_y = 320, 40
    services.append(service_box(api_x, api_y, 200, 56, "API Gateway (HTTP)", "api-gateway"))
    flows.append(orthogonal_path([(cognito_x + 160, cognito_y + 28), (api_x, api_y + 28)], "north-south", bidirectional=False))

    bedrock_x, bedrock_y = 1240, 40
    services.append(service_box(bedrock_x, bedrock_y, 320, 56, "Amazon Bedrock", "bedrock",
                                 sub="claude-3-5-sonnet (config.BEDROCK_MODEL_ID)"))

    vpc_x, vpc_y, vpc_w, vpc_h = 40, 128, 1520, 288
    boundaries.append(boundary(vpc_x, vpc_y, vpc_w, vpc_h, "app-vpc (Private subnets)"))

    persona_y = vpc_y + 40
    persona_defs = [("network-operator", 80, "read-only tools only"), ("connectivity-planner", 400, "+ propose_connectivity")]
    persona_centers = {}
    for name, dx, sub in persona_defs:
        px = vpc_x + dx
        services.append(service_box(px, persona_y, 280, 64, name, "agentcore", sub=sub))
        persona_centers[name] = (px + 140, persona_y + 64)
        flows.append(orthogonal_path([(api_x + 100, api_y + 56), (px + 140, persona_y)], "north-south", bidirectional=False))
        flows.append(orthogonal_path(
            [(px + 220, persona_y), (px + 220, vpc_y - 16), (bedrock_x + 160, vpc_y - 16), (bedrock_x + 160, bedrock_y + 56)],
            "north-south", dashed=True, bidirectional=False))

    mem_y = persona_y + 104
    mem_defs = [
        ("DynamoDB (working)", 80, "TTL + PITR + CMK", "dynamodb"),
        ("S3 (durable)", 400, "append-only JSONL, versioned", "s3"),
        ("S3 Vectors + Knowledge Base", 720, "semantic -- Titan Embed v2", "s3-vectors"),
    ]
    for label, dx, sub, icon in mem_defs:
        mx = vpc_x + dx
        mw = 280 if dx < 700 else 340
        services.append(service_box(mx, mem_y, mw, 64, label, icon, sub=sub))
        for name in persona_centers:
            flows.append(orthogonal_path([persona_centers[name], (mx + mw // 2, mem_y)], "north-south", bidirectional=True))

    tools_x, tools_y = vpc_x + 1120, persona_y
    services.append(service_box(tools_x, tools_y, 360, mem_y + 64 - persona_y, "MCP tool Lambdas (x7)", "lambda",
                                 sub="query_kafka, search_memory, get_network_health,\nquery_governance, cloudwan_topology,\ndetect_anomalies, propose_connectivity"))
    for name in persona_centers:
        flows.append(orthogonal_path([persona_centers[name], (tools_x, persona_y + 32)], "east-west", bidirectional=True))

    agentcore_y = vpc_y + vpc_h + 40
    boundaries.append(boundary(vpc_x, agentcore_y, 900, 120, "Bedrock AgentCore"))
    ac_defs = [("Gateway (MCP)", 40, "agentcore"), ("Memory", 260, "agentcore"), ("Runtime x2", 480, "agentcore"), ("Workload Identity", 700, "iam")]
    for label, dx, icon in ac_defs:
        acx = vpc_x + dx
        services.append(service_box(acx, agentcore_y + 44, 180, 56, label, icon))
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
    services.append(service_box(cc_x, cc_y, 340, 56, "CodeCommit (open a PR)", "codecommit", sub="propose_connectivity -- never a direct mutation"))
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
    services.append(service_box(vpc_x, 40, 200, 56, "app-vpc Flow Logs", "cloudwatch"))
    bucket_x = 320
    services.append(service_box(bucket_x, 40, 220, 56, "S3: raw/ -> processed/", "s3"))
    flows.append(orthogonal_path([(vpc_x + 200, 68), (bucket_x, 68)], "east-west", bidirectional=False))

    pre_x = 620
    services.append(service_box(pre_x, 40, 220, 56, "log-preprocessor", "lambda", sub="daily -> numeric CSV"))
    flows.append(orthogonal_path([(bucket_x + 220, 68), (pre_x, 68)], "east-west", bidirectional=False))

    train_x, train_y = 620, 152
    services.append(service_box(train_x, train_y, 220, 56, "rcf-trainer", "lambda", sub="weekly training job"))
    flows.append(orthogonal_path([(pre_x + 110, 96), (train_x + 110, train_y)], "north-south", bidirectional=False))

    promote_x = 900
    services.append(service_box(promote_x, train_y, 240, 56, "rcf-model-promoter", "lambda", sub="on training completion"))
    flows.append(orthogonal_path([(train_x + 220, train_y + 28), (promote_x, train_y + 28)], "east-west", dashed=True, bidirectional=False))
    flows.append(f'<text x="{train_x + 224}" y="{train_y - 6}" class="flow-label">EventBridge: Training Job State Change</text>')

    endpoint_x = 1220
    services.append(service_box(endpoint_x, train_y, 240, 56, "Async Endpoint", "sagemaker", sub="scale-to-zero"))
    flows.append(orthogonal_path([(promote_x + 240, train_y + 28), (endpoint_x, train_y + 28)], "east-west", bidirectional=False))

    scorer_x, scorer_y = 900, 264
    services.append(service_box(scorer_x, scorer_y, 240, 56, "rcf-batch-scorer", "lambda", sub="every 6h -- CreateTransformJob"))
    flows.append(orthogonal_path([(promote_x + 120, train_y + 56), (scorer_x + 120, scorer_y)], "north-south", dashed=True, bidirectional=False))
    flows.append(f'<text x="{scorer_x - 4}" y="{scorer_y - 6}" class="label-service-sub">uses the currently-promoted model</text>')

    findings_x = 1220
    services.append(service_box(findings_x, scorer_y, 240, 56, "rcf-findings-processor", "lambda", sub="on transform completion"))
    flows.append(orthogonal_path([(scorer_x + 240, scorer_y + 28), (findings_x, scorer_y + 28)], "east-west", dashed=True, bidirectional=False))

    dynamo_y = 128
    services.append(service_box(40, dynamo_y, 200, 56, "DynamoDB findings", "dynamodb", sub="read by MCP tool"))
    sns_s3_y = dynamo_y + 72
    services.append(service_box(40, sns_s3_y, 200, 56, "SNS + S3", "sns"))
    flows.append(orthogonal_path([(findings_x + 120, scorer_y + 56), (findings_x + 120, sns_s3_y + 28), (240, sns_s3_y + 28)], "north-south", dashed=True, bidirectional=False))

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


def build_application_path_svg() -> str:
    """Sixth, independent diagram: the request path through the always-on
    application layer -- privatelink_stack.py + threetier_stack.py --
    Viewer -> CloudFront -> (default) static S3 frontend or (/api/*) the
    internal app-tier ALB via a CloudFront VPC origin -> Fargate -> DynamoDB,
    plus the separate provider-vpc PrivateLink cluster (NLB -> Fargate)
    reached from app-vpc through a one-way interface endpoint. VPC Lattice's
    own service network is a big enough topic to get its own panel next."""
    w, h = 1600, 320
    services, flows, boundaries = [], [], []

    viewer_x, viewer_y = 40, 80
    services.append(service_box(viewer_x, viewer_y, 110, 56, "Viewer", icon_eye))
    cf_x = 190
    services.append(service_box(cf_x, viewer_y, 160, 56, "CloudFront", "cloudfront"))
    web_x = 390
    services.append(service_box(web_x, viewer_y, 180, 56, "S3 (web tier)", "s3", sub="static frontend, OAC"))
    flows.append(orthogonal_path([(viewer_x + 110, viewer_y + 28), (cf_x, viewer_y + 28)], "north-south", bidirectional=False))
    flows.append(orthogonal_path([(cf_x + 160, viewer_y + 28), (web_x, viewer_y + 28)], "north-south", bidirectional=False))
    flows.append(f'<text x="{cf_x + 4}" y="{viewer_y - 8}" class="flow-label">default route</text>')

    app_x, app_y, app_w, app_h = 620, 20, 580, 240
    boundaries.append(boundary(app_x, app_y, app_w, app_h, "app-vpc"))
    alb_x, alb_y = app_x + 40, app_y + 60
    services.append(service_box(alb_x, alb_y, 140, 56, "ALB", "elb", sub="internal"))
    fargate_x = alb_x + 180
    services.append(service_box(fargate_x, alb_y, 180, 56, "Fargate (app tier)", "fargate"))
    dynamo_x = fargate_x + 220
    services.append(service_box(dynamo_x, alb_y, 140, 56, "DynamoDB", "dynamodb"))
    flows.append(orthogonal_path([(alb_x + 140, alb_y + 28), (fargate_x, alb_y + 28)], "east-west", bidirectional=False))
    flows.append(orthogonal_path([(fargate_x + 180, alb_y + 28), (dynamo_x, alb_y + 28)], "east-west", bidirectional=False))
    flows.append(orthogonal_path(
        [(cf_x + 80, viewer_y + 56), (cf_x + 80, alb_y - 20), (alb_x + 70, alb_y - 20), (alb_x + 70, alb_y)],
        "north-south", dashed=True, bidirectional=False))
    flows.append(f'<text x="{cf_x + 90}" y="{alb_y - 26}" class="flow-label">/api/* via CloudFront VPC origin</text>')

    lattice_host_y = alb_y + 96
    services.append(service_box(alb_x, lattice_host_y, 520, 56, "EC2: LatticeInstanceTargetHost", "ec2",
                                 sub="same host = Lattice INSTANCE + IP + TLS_PASSTHROUGH targets -- see next panel"))

    prov_x, prov_y, prov_w, prov_h = 1240, 20, 320, 240
    boundaries.append(boundary(prov_x, prov_y, prov_w, prov_h, "provider-vpc (PrivateLink)"))
    nlb_x = prov_x + 40
    services.append(service_box(nlb_x, alb_y, 240, 56, "NLB", "nlb", sub="fronts a VPC Endpoint Service"))
    provider_fargate_y = alb_y + 96
    services.append(service_box(nlb_x, provider_fargate_y, 240, 56, "Fargate (provider)", "fargate"))
    flows.append(orthogonal_path([(nlb_x + 120, alb_y + 56), (nlb_x + 120, provider_fargate_y)], "north-south", bidirectional=False))

    pl_y = alb_y + 28
    flows.append(orthogonal_path(
        [(dynamo_x + 140, pl_y), (prov_x - 24, pl_y), (prov_x - 24, alb_y + 28), (nlb_x, alb_y + 28)],
        "east-west", dashed=True, bidirectional=False))
    flows.append(f'<text x="{dynamo_x + 150}" y="{pl_y - 8}" class="flow-label">PrivateLink -- one-way, no peering</text>')

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south", "east-west")
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="app-diagram-title app-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="app-diagram-title">Application path -- three-tier + PrivateLink</title>
  <desc id="app-diagram-desc">A viewer hits CloudFront, which serves the static frontend from a private S3 bucket by default or, for /api/* requests, reaches the internal app-tier ALB through a CloudFront VPC origin (no public ALB needed). The ALB forwards to a Fargate service which reads/writes DynamoDB; the same app-vpc also hosts one EC2 instance that exists solely to be a VPC Lattice target (see the next panel). A separate provider-vpc runs its own Fargate service behind an internal NLB, which fronts a PrivateLink VPC Endpoint Service; app-vpc reaches it one-way through an interface endpoint, with no VPC peering and no shared route tables.</desc>
  <defs>{defs}</defs>
  <g id="app-boundaries">{"".join(boundaries)}</g>
  <g id="app-services">{"".join(services)}</g>
  <g id="app-flows">{"".join(flows)}</g>
</svg>'''


def build_lattice_svg() -> str:
    """Seventh, independent diagram: lattice_stack.py's service network --
    3 services demonstrating path-based, header-based, weighted-canary,
    and TLS_PASSTHROUGH routing across EC2/Lambda/ALB target types, plus a
    Resource Gateway giving the mesh its own (app-layer) hybrid ingress to
    the same on-prem broker build_svg() reaches over a network-layer VPN,
    a cross-account RAM share, and access logs. Representative nodes at the
    same abstraction level as the other secondary panels -- lattice_stack.py
    itself has ~30 CDK constructs; see SPEC.md for the exhaustive list."""
    w, h = 1600, 600
    services, flows, boundaries = [], [], []

    boundaries.append(boundary(40, 40, 1520, 520, "VPC Lattice Service Network -- lattice-lab (AWS_IAM auth)"))

    svc_y = 88
    svc_w, svc_gutter = 320, 40
    svc1_x = 80
    services.append(service_box(svc1_x, svc_y, svc_w, 64, "Service: primary", "vpc-lattice",
                                 sub="path + header + weighted-canary + HTTPS"))
    svc2_x = svc1_x + svc_w + svc_gutter
    services.append(service_box(svc2_x, svc_y, svc_w, 64, "Service: TLS passthrough", "vpc-lattice",
                                 sub="custom domain, TCP:443"))
    svc3_x = svc2_x + svc_w + svc_gutter
    services.append(service_box(svc3_x, svc_y, svc_w, 64, "Service: dual-stack", "vpc-lattice",
                                 sub="independent 2nd Lambda-backed service"))

    target_y = svc_y + 160
    inst_x = 80
    services.append(service_box(inst_x, target_y, 300, 64, "EC2 instance target host", "ec2",
                                 sub="INSTANCE + IP + TLS targets"))
    lambda_x = inst_x + 340
    services.append(service_box(lambda_x, target_y, 240, 64, "canary Lambda", "lambda",
                                 sub="header + weighted + dual-stack"))
    alb_x = lambda_x + 280
    services.append(service_box(alb_x, target_y, 260, 64, "App ALB (ThreeTierStack)", "elb",
                                 sub="see Application Path panel", dim=True))

    def route(sx, label, tx, dashed=True):
        flows.append(orthogonal_path([(sx, svc_y + 64), (sx, target_y - 16), (tx, target_y - 16), (tx, target_y)],
                                      "north-south", dashed=dashed, bidirectional=False))
        flows.append(f'<text x="{min(sx, tx) + 4}" y="{target_y - 22}" class="flow-label">{esc(label)}</text>')

    route(svc1_x + 60, "/v1/* -> IP target group", inst_x + 60)
    route(svc1_x + 160, "x-canary:true -> Lambda target group", lambda_x + 60)
    route(svc1_x + 260, "/canary/* weighted 90/10", alb_x + 60)
    flows.append(orthogonal_path([(svc2_x + 160, svc_y + 64), (svc2_x + 160, target_y - 40), (inst_x + 220, target_y - 40), (inst_x + 220, target_y)],
                                  "north-south", dashed=True, bidirectional=False))
    flows.append(orthogonal_path([(svc3_x + 160, svc_y + 64), (svc3_x + 160, target_y + 32), (lambda_x + 180, target_y + 32), (lambda_x + 180, target_y + 64)],
                                  "east-west", dashed=True, bidirectional=False))

    gw_y = target_y + 128
    resgw_x = 80
    services.append(service_box(resgw_x, gw_y, 300, 64, "Resource Gateway", "vpc-lattice",
                                 sub="hybrid ingress -- app-layer path"))
    broker_x = resgw_x + 340
    services.append(service_box(broker_x, gw_y, 260, 64, "on-prem broker (TCP/9092)", "ec2",
                                 sub="app-layer hybrid path", dim=True))
    flows.append(orthogonal_path([(resgw_x + 300, gw_y + 32), (broker_x, gw_y + 32)], "east-west", bidirectional=False))
    flows.append(f'<text x="{resgw_x + 306}" y="{gw_y + 24}" class="flow-label">Resource Configuration (SINGLE/TCP:9092)</text>')

    side_y = gw_y + 96
    side_items = [
        ("app-vpc association", "vpc", "ServiceNetworkVpcAssociation"),
        ("provider-vpc (via SN endpoint)", "vpc", "reaches services without joining the network"),
        ("RAM share -> 2nd account", "iam", "cross-account ServiceNetwork ARN share"),
        ("Access logs: CW Logs + S3", "cloudwatch", "ServiceNetwork -> Logs, Service -> S3"),
    ]
    side_w, side_gutter = 340, 24
    for i, (label, icon, sub) in enumerate(side_items):
        sx = 80 + i * (side_w + side_gutter)
        services.append(service_box(sx, side_y, side_w, 64, label, icon, sub=sub, dim=True))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south", "east-west")
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="lattice-diagram-title lattice-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="lattice-diagram-title">VPC Lattice service network</title>
  <desc id="lattice-diagram-desc">Three independent VPC Lattice services share one service network. The primary service demonstrates path-based (/v1/*), header-based (x-canary:true), and weighted-canary (/canary/* 90/10) routing across an EC2 INSTANCE target, a Lambda target, and ThreeTierStack's own app ALB as a target -- plus an HTTPS listener using a self-signed imported certificate. A second service demonstrates TLS_PASSTHROUGH straight to the same EC2 host's TLS port; a third, independent Lambda-backed service demonstrates dual-stack routing. A Resource Gateway gives the mesh its own app-layer hybrid-connectivity path to the same simulated on-prem broker the network-layer Site-to-Site VPN reaches in the core network panel -- two different hybrid patterns, side by side. The service network is also shared cross-account via AWS RAM and streams access logs to CloudWatch Logs and S3.</desc>
  <defs>{defs}</defs>
  <g id="lattice-boundaries">{"".join(boundaries)}</g>
  <g id="lattice-services">{"".join(services)}</g>
  <g id="lattice-flows">{"".join(flows)}</g>
</svg>'''


def build_pipeline_svg() -> str:
    """Eighth, independent diagram: the self-mutating CDK Pipeline
    (pipeline_stack.py) that deploys every other stack in this project --
    CodeCommit source (not GitHub -- see README), Synth, self-mutation,
    a human manual-approval gate, and the LandingZone deploy stage."""
    w, h = 1600, 300
    services, flows, boundaries = [], [], []

    boundaries.append(boundary(20, 20, 1560, 180, "AWS CodePipeline -- self-mutating CDK Pipeline"))

    y0 = 76
    cc_x = 60
    services.append(service_box(cc_x, y0, 180, 64, "CodeCommit", "codecommit", sub="not GitHub"))
    synth_x = cc_x + 220
    services.append(service_box(synth_x, y0, 200, 64, "Synth (CodeBuild)", "codebuild", sub="venv -> cdk synth"))
    selfmut_x = synth_x + 240
    services.append(service_box(selfmut_x, y0, 220, 64, "Self-Mutate", "codebuild", sub="if pipeline changed"))
    approve_x = selfmut_x + 260
    services.append(service_box(approve_x, y0, 200, 64, "PromoteToLandingZone", icon_eye, sub="human approval gate"))
    deploy_x = approve_x + 240
    services.append(service_box(deploy_x, y0, 300, 64, "LandingZone Stage", "cloudformation",
                                 sub="~15 stacks, 2 regions"))

    chain_y = y0 + 32
    for ax, bx in [(cc_x + 180, synth_x), (synth_x + 200, selfmut_x), (selfmut_x + 220, approve_x), (approve_x + 200, deploy_x)]:
        flows.append(orthogonal_path([(ax, chain_y), (bx, chain_y)], "north-south", bidirectional=False))

    loop_y = y0 - 24
    flows.append(orthogonal_path(
        [(selfmut_x + 20, y0), (selfmut_x + 20, loop_y), (selfmut_x + 200, loop_y), (selfmut_x + 200, y0)],
        "mgmt", dashed=True, bidirectional=False))
    flows.append(f'<text x="{selfmut_x + 24}" y="{loop_y - 6}" class="flow-label">self-mutating: redeploys its own pipeline definition first</text>')

    artifact_y = y0 + 112
    artifact_x = synth_x
    services.append(service_box(artifact_x, artifact_y, 200, 48, "Artifact Bucket", "s3", sub="state between stages"))
    for tx in (synth_x, selfmut_x, deploy_x):
        flows.append(orthogonal_path(
            [(artifact_x + 100, artifact_y), (artifact_x + 100, artifact_y - 16), (tx + 20, artifact_y - 16), (tx + 20, y0 + 64)],
            "mgmt", dashed=True, bidirectional=False))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south", "mgmt")
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="pipeline-diagram-title pipeline-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="pipeline-diagram-title">CI/CD -- self-mutating CDK Pipeline</title>
  <desc id="pipeline-diagram-desc">A self-mutating CDK Pipeline sourced from AWS CodeCommit (deliberately not GitHub -- see README) synths with CodeBuild, redeploys its own pipeline definition first whenever that definition changed, waits at a human manual-approval gate before any real-money change, then deploys the LandingZone stage -- every stack in this project, across 2 regions when Cloud WAN is enabled. An S3 artifact bucket passes state between stages.</desc>
  <defs>{defs}</defs>
  <g id="pipeline-boundaries">{"".join(boundaries)}</g>
  <g id="pipeline-services">{"".join(services)}</g>
  <g id="pipeline-flows">{"".join(flows)}</g>
</svg>'''


def build_blog_analytics_svg() -> str:
    """Ninth, independent diagram: the blog read-analytics layer
    (blog_analytics_stack.py) -- reached through the SAME CloudFront
    distribution as the three-tier web app (a /analytics/* behavior added
    onto ThreeTierStack's own distribution), and its dashboard widgets are
    added onto the existing lattice-lab dashboard, not a second one --
    same cross-stack construct-mutation pattern auto_heal_stack.py already
    uses on observability_stack.py."""
    w, h = 1600, 280
    services, flows, boundaries = [], [], []

    y0 = 70
    cf_x = 60
    services.append(service_box(cf_x, y0, 200, 64, "CloudFront", "cloudfront", sub="/analytics/* behavior"))
    api_x = cf_x + 240
    services.append(service_box(api_x, y0, 200, 64, "API Gateway (HTTP)", "api-gateway", sub="POST /events"))
    lambda_x = api_x + 240
    services.append(service_box(lambda_x, y0, 220, 64, "analytics-ingest", "lambda", sub="country from CF header"))
    ddb_x = lambda_x + 260
    services.append(service_box(ddb_x, y0, 220, 64, "DynamoDB (TTL 180d)", "dynamodb", sub="raw per-visitor events"))
    cw_x = ddb_x + 260
    services.append(service_box(cw_x, y0, 240, 64, "lattice-lab dashboard", "cloudwatch", sub="permanent aggregates"))

    chain_y = y0 + 32
    for ax, bx in [(cf_x + 200, api_x), (api_x + 200, lambda_x), (lambda_x + 220, ddb_x), (ddb_x + 220, cw_x)]:
        flows.append(orthogonal_path([(ax, chain_y), (bx, chain_y)], "north-south", bidirectional=False))
    flows.append(f'<text x="{ddb_x + 4}" y="{chain_y - 40}" class="flow-label">also PutMetricData directly -- survives the TTL</text>')

    beacon_y = y0 + 128
    boundaries.append(boundary(60, beacon_y - 24, 700, 96, "app/frontend-next -- AnalyticsBeacon.tsx (client)", small=True))
    services.append(service_box(100, beacon_y, 260, 56, "page_view / read_complete", None, sub="fired on load + 95% scroll"))
    services.append(service_box(400, beacon_y, 320, 56, "session_end (sendBeacon)", None, sub="time on page, max scroll, github_click"))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south",)
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="blog-diagram-title blog-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="blog-diagram-title">Blog read analytics</title>
  <desc id="blog-diagram-desc">The blog's client-side AnalyticsBeacon fires page_view on load, read_complete at 95% scroll, and a session_end beacon (via navigator.sendBeacon) with time-on-page and max-scroll on tab close, plus a github_click event on the repo link. All of it reaches the same CloudFront distribution as the rest of the site at /analytics/*, forwarded to an API Gateway HTTP API, a single Lambda that writes a TTL'd raw event to DynamoDB (country from CloudFront's own viewer-country header, no third-party geo-IP lookup) and publishes permanent aggregate metrics to CloudWatch -- graphed on the same lattice-lab dashboard every other layer uses.</desc>
  <defs>{defs}</defs>
  <g id="blog-boundaries">{"".join(boundaries)}</g>
  <g id="blog-services">{"".join(services)}</g>
  <g id="blog-flows">{"".join(flows)}</g>
</svg>'''


def build_blog_assistant_svg() -> str:
    """Tenth, independent diagram: the blog chat assistant
    (blog_assistant_stack.py) -- retrieval augmented generation over the
    project's own documentation. Reached through the same CloudFront
    distribution as the rest of the site (a /assistant/* behavior, same
    cross-stack construct-mutation pattern as blog_analytics_stack.py),
    mirroring agentic_ai_stack.py's semantic memory tier (S3 Vectors
    backed Bedrock Knowledge Base) but standing on its own, always
    deployed rather than gated behind ENABLE_AI."""
    w, h = 1600, 560
    services, flows, boundaries = [], [], []

    y0 = 70
    cf_x = 60
    services.append(service_box(cf_x, y0, 200, 64, "CloudFront", "cloudfront", sub="/assistant/* behavior"))
    api_x = cf_x + 240
    services.append(service_box(api_x, y0, 200, 64, "API Gateway (HTTP)", "api-gateway", sub="POST /assistant/ask"))
    lambda_x = api_x + 240
    services.append(service_box(lambda_x, y0, 220, 64, "blog-assistant-ask", "lambda", sub="retrieve, generate, recall"))

    chain_y = y0 + 32
    flows.append(orthogonal_path([(cf_x + 200, chain_y), (api_x, chain_y)], "north-south", bidirectional=False))
    flows.append(orthogonal_path([(api_x + 200, chain_y), (lambda_x, chain_y)], "north-south", bidirectional=False))

    row2_y = y0 + 128
    kb_x = lambda_x - 40
    services.append(service_box(kb_x, row2_y, 260, 64, "Knowledge Base", "bedrock", sub="Retrieve -- grounded context"))
    gen_x = kb_x + 300
    services.append(service_box(gen_x, row2_y, 220, 64, "Claude (Converse)", "bedrock", sub="config.BEDROCK_MODEL_ID"))
    mem_x = gen_x + 260
    services.append(service_box(mem_x, row2_y, 220, 64, "S3 memory (TTL 30d)", "s3", sub="1 object per conversation"))

    flows.append(orthogonal_path([(lambda_x + 30, y0 + 64), (kb_x + 130, row2_y)], "north-south", bidirectional=True))
    flows.append(orthogonal_path([(lambda_x + 110, y0 + 64), (gen_x + 110, row2_y)], "north-south", bidirectional=True))
    flows.append(orthogonal_path([(lambda_x + 190, y0 + 64), (mem_x + 110, row2_y)], "north-south", bidirectional=True))

    row3_y = row2_y + 128
    vec_x = kb_x
    services.append(service_box(vec_x, row3_y, 260, 56, "S3 Vectors index", "s3-vectors", sub="1024-dim, Titan Embed v2"))
    flows.append(orthogonal_path([(kb_x + 130, row2_y + 64), (vec_x + 130, row3_y)], "north-south", bidirectional=False))

    src_x = vec_x + 300
    services.append(service_box(src_x, row3_y, 260, 56, "KB source bucket", "s3", sub="this project's own blog text"))
    flows.append(orthogonal_path([(kb_x + 200, row2_y + 64), (src_x + 130, row3_y)], "north-south", dashed=True, bidirectional=False))
    flows.append(f'<text x="{src_x + 4}" y="{row3_y - 6}" class="flow-label">ingested at deploy time</text>')

    client_x = 60
    client_boundary_y = row3_y + 56 + 30
    boundaries.append(boundary(client_x, client_boundary_y, 700, 96, "app/frontend-next -- ChatConsole.tsx (client)", small=True))
    services.append(service_box(client_x + 40, client_boundary_y + 30, 260, 56, "question + conversationId", None, sub="POST body"))
    services.append(service_box(client_x + 340, client_boundary_y + 30, 340, 56, "answer text", None, sub="rendered in the console"))

    defs = "\n".join(
        f'<marker id="marker-arrow-{cls}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" class="marker-{cls}"/></marker>'
        for cls in ("north-south",)
    )
    return f'''<svg viewBox="0 0 {w} {h}" role="img" aria-labelledby="assistant-diagram-title assistant-diagram-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="assistant-diagram-title">Blog chat assistant</title>
  <desc id="assistant-diagram-desc">A visitor question reaches the same CloudFront distribution as the rest of the site at /assistant/*, forwarded to an API Gateway HTTP API and a single Lambda. That Lambda retrieves grounded context from a Bedrock Knowledge Base backed by an S3 Vectors index built from this project's own blog content, sends the question plus retrieved context plus prior conversation turns to Claude through the Bedrock Converse API, then reads and writes conversation history as a single JSON object per conversation id in an S3 bucket with a 30 day lifecycle rule. The knowledge base source bucket is ingested once at deploy time, not per request.</desc>
  <defs>{defs}</defs>
  <g id="assistant-boundaries">{"".join(boundaries)}</g>
  <g id="assistant-services">{"".join(services)}</g>
  <g id="assistant-flows">{"".join(flows)}</g>
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
nav.toc { margin: 0 0 24px; padding: 12px 16px; border: 1px solid var(--surface-tint); border-radius: 8px; background: var(--surface); }
nav.toc ul { list-style: none; padding: 0; margin: 0; display: flex; flex-wrap: wrap; gap: 8px 20px; }
nav.toc li { font-size: 13px; }
nav.toc a { color: var(--flow-north-south); text-decoration: none; font-weight: 500; }
nav.toc a:hover { text-decoration: underline; }
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



# Narrative content for each layer -- WHY it exists, WHAT it solves, HOW it
# works (grounded in the real inventory gathered for this build), and the
# named architecture PATTERN it demonstrates. Written for a technical
# interview panel walkthrough, one entry per panel anchor.
PROSE = {
    'network-inspection': {
        "eyebrow": 'Hub-and-Spoke Traffic Inspection',
        "why": "A multi-VPC estate with independent egress paths per VPC means N different places to apply firewall policy, N different places for it to drift, and no single point to run IPS signatures against inter-VPC traffic. The design goal here was one enforcement point that every spoke -- app, provider, and a simulated on-prem network over VPN -- passes through by construction, not by convention, so a misconfigured spoke route table can't silently bypass inspection.",
        "what": "Every spoke's default route (0.0.0.0/0) resolves to the inspection VPC, so north-south internet-bound traffic and east-west inter-spoke traffic both transit the same firewall tier before reaching their destination. The firewall fleet itself is horizontally scaled and self-healing -- an Auto Scaling Group per AZ behind a Gateway Load Balancer means a failed appliance is replaced and in-flight flows are rebalanced onto its surviving neighbor rather than black-holed. A second, narrower problem -- the firewall fleet's own outbound traffic (package installs, policy pulls) recursing back through its own inspection path and hitting a default-deny rule -- is solved with a dedicated, uninspected NAT instance.",
        "how": "The Transit Gateway has default route table association and propagation disabled; two explicit route tables replace it -- spoke-rt (app, provider, and the VPN's auto-created attachment) carries a single static 0.0.0.0/0 route to the inspection VPC attachment, while inspection-rt holds propagated spoke routes plus a static default route back to app-vpc specifically, since app-vpc is the only spoke with a NAT Gateway. Flow symmetry for stateful inspection comes from two independent switches that both have to be on: ApplianceModeSupport=enable on the inspection VPC's TGW attachment, and leaving the GWLB target group at its default 5-tuple stickiness (2-/3-tuple is documented as incompatible with TGW appliance mode). Inside inspection-vpc, a Gateway Load Balancer forwards GENEVE/6081 to a fleet of Ubuntu 24.04 EC2 instances running nftables and Suricata, provisioned as one Auto Scaling Group per AZ (min=max=desired=2) rather than one shared multi-AZ ASG, wired to the target group via the CFN escape hatch since GWLB isn't one of the L2 ASG's supported target types; target_failover is explicitly set to rebalance on both deregistration and unhealthy so a dying appliance doesn't drop live connections. GWLB Endpoints (one per AZ) carry traffic in from inspection-vpc's own TGW-attach subnet, and a matching set of endpoints sits in app-vpc next to its NAT Gateway, backed by an IGW edge-association route table that redirects the NAT Gateway's return traffic back through inspection -- without it, reply packets for outbound sessions would skip the firewall on the way back in. A simulated on-prem network reaches the same hub over a static-routes-only Site-to-Site VPN terminated by a self-managed libreswan EC2 instance acting as the customer gateway, landing on spoke-rt like any other spoke.",
        "pattern_name": 'Hub-and-spoke centralized inspection with a GWLB bump-in-the-wire',
        "pattern_desc": 'This is the standard AWS pattern for enforcing inline, stateful inspection across a multi-VPC environment from one scalable choke point instead of duplicating firewall policy per VPC. The tradeoff is explicit: every packet takes an extra hop through the Transit Gateway and GWLB before reaching its destination, and the two independent appliance-mode switches (TGW attachment option plus target group stickiness) have to be understood together or flow symmetry silently breaks.',
    },
    'blog-analytics': {
        "eyebrow": 'First-Party Read Analytics',
        "why": "A public-facing blog with no analytics at all can't answer the two questions that actually matter for a portfolio piece: did anyone read this, and did they act on it (click through to the repo)? A third-party analytics script (Google Analytics, a pixel) solves that but adds an external dependency, a third party in the data-collection chain, and a script the diagram-site's own strict CSP (script-src 'none') would have to be loosened to allow. The goal here was first-party analytics -- this project's own Lambda, this project's own DynamoDB table -- with the same zero-external-dependency discipline as the rest of the site.",
        "what": "Every page load, 95%-scroll completion, GitHub-link click, and end-of-session (time on page, max scroll depth) gets recorded, without any third-party script and without asking a third-party geo-IP service where the visitor is -- CloudFront already knows, and forwards it as a header. Raw, per-visitor rows are retained for 180 days and then expire automatically; the aggregate counts (page views, completions, click-throughs, average engagement) are published as CloudWatch metrics that outlive that TTL and graph on the same dashboard as every other layer in this project.",
        "how": "The client-side AnalyticsBeacon (a small React client component) fires page_view on mount, watches scroll position to detect a 95%+ read and fire read_complete once, and on pagehide/visibilitychange sends a final session_end beacon via navigator.sendBeacon (fire-and-forget, survives the tab actually closing, unlike a normal fetch). Every call is wrapped so a failed or slow analytics call can never block or break the reading experience. Requests hit /analytics/events on the SAME CloudFront distribution the blog itself is served from -- a behavior added onto ThreeTierStack's existing distribution from this stack, the identical cross-stack construct-mutation pattern auto_heal_stack.py already uses to extend observability_stack.py's dashboard -- so there's no CORS to configure and no second domain to manage. The Lambda derives the visitor's country from CloudFront-Viewer-Country (a header CloudFront itself sets, not a client-supplied or third-party-looked-up value) and their IP from X-Forwarded-For's first entry, writes one TTL'd item to DynamoDB, and separately calls PutMetricData so the aggregate survives independently of the raw record's expiry.",
        "pattern_name": 'First-party telemetry beacon with TTL’d raw storage + permanent aggregate metrics',
        "pattern_desc": "Splitting 'raw event, short-lived' from 'aggregate metric, permanent' is the same shape governance_stack.py uses for compliance findings: keep exactly the granularity you need for exactly as long as you need it, and let a cheaper, coarser signal outlive that. Here it also does double duty as a privacy control -- the source IP this project's owner explicitly asked to capture doesn't accumulate forever, only the count derived from it does.",
    },
    'blog-assistant': {
        "eyebrow": 'Grounded Retrieval Augmented Generation',
        "why": "A chatbot with no grounding either refuses to answer anything specific or, worse, confidently invents details about a project it has never actually seen. Neither is acceptable on a portfolio site where the whole point is demonstrating real engineering judgment. The goal was an assistant that answers from this project's own documentation, admits when a question falls outside that documentation, and remembers the last few turns of a conversation the same explicit way everything else in this project handles state: in a resource you can open and inspect, not a hidden session cache.",
        "what": "A visitor can ask the assistant anything about the architecture, the pipeline, the AI layer, or any other part of this project, and get an answer grounded in the actual blog content rather than a generic model guess. Retrieval happens against a Bedrock Knowledge Base built from this project's own written sections (both the plain-language and technical passages), generation runs on Anthropic's Claude through Bedrock's Converse API, and conversation memory persists as one JSON object per conversation id in S3, so a follow-up question like 'what about the on-prem path' still has context from the question before it.",
        "how": "blog_assistant_stack.py mirrors agentic_ai_stack.py's semantic memory tier almost exactly, the same S3 Vectors CfnVectorBucket/CfnIndex pair (1024-dim, Titan Embed v2, cosine distance), the same explicit iam.Policy-plus-add_dependency pattern on the Knowledge Base role (a role_arn reference alone does not create a CloudFormation dependency on the separate Policy resource that grants s3vectors:QueryVectors, confirmed the hard way in this project already), and the same indexing.s3vectors.amazonaws.com KMS grant S3 Vectors' own async indexing needs. Where it differs is standing entirely on its own rather than living inside the ENABLE_AI-gated stack, so the always-on blog's assistant works with no feature flag required. The single Lambda handling every request does three things in sequence: calls bedrock-agent-runtime Retrieve against the knowledge base, loads the last several turns of conversation history from S3 (capped and trimmed, not unbounded), and calls bedrock-runtime Converse with the retrieved passages, the history, and the new question, then writes the updated history back to S3 before returning. Reached through /assistant/* on the same CloudFront distribution as the rest of the site, the exact same cross-stack construct-mutation pattern blog_analytics_stack.py already established for /analytics/*.",
        "pattern_name": 'Retrieval Augmented Generation with explicit, inspectable memory',
        "pattern_desc": "Grounding a model in a vector-searchable knowledge base instead of trusting its own training data is the standard fix for both hallucination and staleness: the model answers from what is actually in the index, not from whatever it happened to learn once. Making memory a plain S3 object instead of a framework-managed session is a smaller but deliberate choice in the same spirit as the rest of this project: state that a human can open, read, and delete, not state hidden behind an abstraction.",
    },
    'application-path': {
        "eyebrow": 'Private-Origin Web + One-Way Service Exposure',
        "why": "A public ALB or a public Fargate service is unnecessary attack surface when CloudFront is the only legitimate entry point, and cross-VPC service consumption doesn't need peering or shared route tables when only one service, in one direction, needs to cross the boundary. This layer proves both: a web app with zero public compute behind it, and an app-vpc-to-provider-vpc boundary where the consumer can reach the provider but the provider has no path back at all.",
        "what": "CloudFront is the only public entry point. The default behavior serves the static frontend straight from a private S3 bucket (OAC-only, BLOCK_ALL public access); /api/* is proxied through a CloudFront VPC origin directly to an internal ALB that never gets a public IP or public DNS name. Separately, app-vpc needs to call a service that physically lives in provider-vpc without merging the two networks -- PrivateLink delivers that without a peering connection, without a transit gateway, and without either VPC learning the other's CIDR.",
        "how": "CloudFront's default behavior origins on the web bucket via Origin Access Control; the /api/* behavior uses origins.VpcOrigin.with_application_load_balancer(alb, http_port=80) to reach the internal ALB (internet_facing=False) over ENIs CloudFront manages inside app-vpc, with the ALL_VIEWER_EXCEPT_HOST_HEADER origin request policy and caching disabled since these are API calls, not static assets. The ALB forwards to the app-tier Fargate service (256 CPU / 512 MB, IP-type target group since awsvpc mode has no host instance to register by ID), which writes to a DynamoDB TableV2 on on-demand billing with point-in-time recovery enabled, reached over a DynamoDB gateway VPC endpoint rather than the NAT path. On the PrivateLink side, provider-vpc runs its own Fargate service behind an internal NLB (explicit security group -- CDK's auto-generated placeholder SG has no real ingress/egress and silently fails target health checks if you skip it), fronted by a VPC Endpoint Service with acceptance_required=False since this is single-account. app-vpc consumes it through an Interface VPC Endpoint with private_dns_enabled=False, so the consumer resolves the endpoint's own generated DNS name rather than a verified custom domain. There's no peering connection, no route table entry for provider-vpc's 10.2.0.0/16 in app-vpc, and none for app-vpc's 10.1.0.0/16 in provider-vpc -- the endpoint is just an ENI projected into app-vpc, so the provider has no route back into the consumer's network at all.",
        "pattern_name": 'Private-origin CDN fronting + one-way PrivateLink service exposure',
        "pattern_desc": "CloudFront-to-private-origin removes the ALB's public attack surface without giving up edge TLS termination and caching; PrivateLink removes the need for VPC peering or a transit gateway when only one service, one direction, needs to cross a VPC boundary. The shared tradeoff is asymmetry by design -- the consumer has zero visibility into the provider's network and vice versa, which is exactly the isolation property you want between an app tier and a third-party-style backend, at the cost of not being able to route to anything else in that VPC even if a future use case needed it.",
    },
    'lattice': {
        "eyebrow": 'Application-Layer Service Mesh',
        "why": "Three independent compute primitives — an EC2 Auto Scaling target, a Lambda function, and ThreeTierStack's existing app-tier ALB — needed to sit behind one consistent, authenticated routing surface without forcing every consumer to know which primitive answers which request. Reimplementing path, header, and weighted routing logic per target type (a Lambda@Edge function here, custom ALB rules there) would have meant three different places to audit and three different auth models. It also needed to prove Lattice's hybrid story is more than VPN-adjacent: an app should be able to reach on-prem through the mesh itself, not only through the network layer.",
        "what": 'One VPC Lattice service network exposes three services that exercise the three routing primitives Lattice actually offers: a path rule (/v1/* to the EC2 instance target group), a header rule (x-canary:true to the Lambda target group), and a weighted 90/10 canary split — all resolvable by any associated consumer VPC through the same Lattice-managed DNS name, with per-service IAM auth instead of security-group-only trust. A second service running TLS_PASSTHROUGH proves the mesh can broker a connection without terminating TLS itself, leaving certificate ownership at the target. The Resource Gateway gives the mesh its own application-layer path to the same on-prem broker the TGW/VPN reaches at the network layer, so there are two independently-failing hybrid connectivity patterns side by side rather than one.',
        "how": "The service network is created with auth_type=AWS_IAM, so every request is SigV4-signed and evaluated against policy rather than just routed by IP reachability. The three-target service composes listener rules against target groups of type INSTANCE (EC2), LAMBDA, and ALB — the ALB target group points at ThreeTierStack's own app-tier load balancer, registered directly as a Lattice target instead of duplicated infrastructure, so the mesh fronts what the three-tier app already runs. Auth policies are attached at both the service-network and the individual-service level, scoped with an aws:SourceVpc condition so only requests originating from the associated consumer VPC are authorized regardless of which network path they arrived on. The Resource Gateway plus a resource configuration (type single), associated to the service network, extend that same IAM-authenticated mesh out to the on-prem broker, and the service network is shared cross-account via AWS RAM. Access logs are enabled on the service network and delivered to both CloudWatch Logs and S3 for the audit trail.",
        "pattern_name": 'Hub-and-spoke L7 service mesh with heterogeneous target-group federation',
        "pattern_desc": "A single service network is the hub every consumer VPC associates into, standing in for what would otherwise be a full mesh of PrivateLink endpoints or peering connections per producer/consumer pair, while target groups abstract away whether the backend is an EC2 instance, a Lambda function, or someone else's ALB. The trade-off is that the service network becomes the blast-radius boundary for the auth policy — getting the aws:SourceVpc condition right matters more here than it would in a point-to-point design, because one misconfigured policy is now shared exposure for every service on the mesh.",
    },
    'security-governance': {
        "eyebrow": 'IAM Ceiling & Closed-Loop Remediation',
        "why": "CDK enforces intent at synth time, but nothing stops a role's identity policy from drifting wider across feature branches, or a console user from making an out-of-band change the pipeline never sees. This layer bounds both failure modes with hard limits rather than hope: an IAM ceiling no identity policy can punch through, and a detection plane wired to CloudTrail so a manual mutation is caught in seconds instead of waiting for the next Config evaluation cycle. It also has to answer for itself operationally -- Config, Security Hub, and GuardDuty each surface findings in their own console, but none of them publish a CloudWatch metric you can alarm or trend on.",
        "what": 'SecurityStack gives every project-authored role (network_stack.py, inspection_stack.py, threetier_stack.py, privatelink_stack.py) a permissions boundary that\'s an explicit intersection with its identity policy, not an allow-list layered on top -- plus 5 purpose-separated CMKs and account-level IAM Access Analyzer. GovernanceStack turns CloudTrail into the single event source feeding AWS Config, Security Hub, and GuardDuty, so "who changed what" and "is the account compliant" share one record. Two independent, narrowly-scoped response loops close the loop: DriftRemediationStack catches manual changes made outside the pipeline, and AutoHealStack recovers from 3 known failure signatures without ever touching more than the one resource an alarm names.',
        "how": "The permissions boundary (lattice-lab-permissions-boundary) pairs a curated Allow list with an explicit Deny on iam:CreateRole/PutRolePolicy/AttachRolePolicy/PassRole (excepted only where iam:PassedToService=sagemaker.amazonaws.com) and on removing the boundary from itself -- so a role can't self-escalate even if its own policy is later widened. CloudTrail (lattice-lab-trail, multi-region, log-validated) writes to a CMK-encrypted S3 bucket and CloudWatch Logs, and that log group is what DriftRemediationStack's EventBridge rule filters: mutating API calls whose userIdentity.arn doesn't start with the cdk-hnb659fds-cfn-exec-role prefix trigger the drift-remediator Lambda, which re-checks BreakGlass=true on the caller and ManagedBy=cdk on the resource before acting, against a small explicit action allow-list only -- DRY_RUN flips it to alert-only account-wide. AutoHealStack wires 3 named CloudWatch alarms (VPN TunnelState on the Site-to-Site connection, EC2 StatusCheckFailed on the Lattice INSTANCE-type target host, Bedrock orchestrator Lambda errors) each through its own EventBridge rule into one Step Functions state machine (auto-heal) that runs NotifyBefore -> RemediateTask -> NotifyAfter against the single instance ID baked into that alarm's remediation_type, publishing both ends to the same governance-alerts SNS topic drift-remediation already owns. A third loop, GovernanceMetricsPublisher, polls config:DescribeComplianceByConfigRule / securityhub:GetFindings / guardduty:ListFindings every 15 minutes into a custom LatticeLab/Governance namespace so those three services get a trendable metric they don't natively expose.",
        "pattern_name": 'Hard permissions-boundary ceiling with tag-gated, single-resource auto-remediation',
        "pattern_desc": "A permissions boundary is only a real ceiling if it's evaluated as an intersection, not an allow-list -- a role whose own policy grows too permissive during later development still can't act outside it or grant itself the boundary's own removal. The remediation side commits to the same discipline in a different form: both loops act on exactly one resource identified by the triggering event or alarm, never a fleet-wide sweep, trading blast-radius risk for narrower coverage (3 named failure modes, one explicit action allow-list) over a generic auto-healer that could delete or restart something it shouldn't.",
    },
    'pipeline': {
        "eyebrow": 'Self-Mutating CDK Pipeline',
        "why": 'Two real failure modes drove this design. First, the original source was GitHub via CodeConnections — but that requires the GitHub App to be separately *installed* on the repo, not just OAuth-authorized, a step that\'s easy to miss and left the Source stage failing with "No Branch [main] found" even after the connection itself showed AVAILABLE. Second, this project\'s pipeline definition isn\'t static — it grows a new stack (or a new feature flag) almost every iteration, and a pipeline that can\'t redeploy its own definition means every change to pipeline_stack.py itself requires a manual `cdk deploy PipelineStack`, which is exactly the kind of step that gets forgotten and causes pipeline/code drift.',
        "what": "A push to the CodeCommit repo's main branch (config.CODECOMMIT_BRANCH) is the only trigger needed end to end. CodeCommit authenticates with plain IAM credentials, so there's no install-vs-authorize handshake to get wrong. Because `self_mutation=True`, if that push changed pipeline_stack.py itself, the pipeline redeploys its own CodePipeline/CodeBuild definition first, before touching anything downstream — so the pipeline that runs stage N+1 is never running on stage N's stale definition. Everything in LandingZoneStage — 12 always-on stacks (Security, Network, Inspection, PrivateLink, ThreeTier, Lattice, ResourceGroups, Diagram, Governance, DriftRemediation, Observability, AutoHeal) plus up to five feature-flagged ones (Kafka, CloudWan, Region2, SageMaker, AgenticAi) spanning us-east-1 and us-east-2 — only deploys after a human clears the ManualApprovalStep, so nothing with real cost moves without an explicit click.",
        "how": '`pipelines.CodePipeline` wraps a `CodePipelineSource.code_commit()` source built from `codecommit.Repository.from_repository_name(config.CODECOMMIT_REPO_NAME)`, feeding a `ShellStep("Synth")` whose install phase pins the CodeBuild image\'s pyenv-managed interpreter (`pyenv global 3.12.13`) before creating a venv and running `pip install -r requirements.txt`, then `npx cdk synth`. `pipeline.add_stage(landing_zone, pre=[pipelines.ManualApprovalStep("PromoteToLandingZone")])` is the real-money gate. `pipeline.build_pipeline()` is called explicitly, after `add_stage()`, to force the synth CodeBuild project and its role to materialize immediately rather than lazily during CDK\'s synth pass — that ordering matters, since calling it before `add_stage()` would lock the LandingZone stage out of the already-built pipeline. That materialized role then gets one narrowly-scoped policy statement, `ec2:DescribeAvailabilityZones`, added directly via `pipeline.synth_project.add_to_role_policy()` — needed because cdk.context.json is deliberately not committed, so every synth (including CodeBuild\'s, with no local cache) resolves each VPC\'s AZs with a live API call instead of a stale checked-in file.',
        "pattern_name": 'Self-mutating CDK Pipeline with a manual promotion gate',
        "pattern_desc": "This is CDK Pipelines' self-mutation feature used as intended: the pipeline is treated as just another CDK-managed resource, so it updates itself as a normal step in its own execution rather than requiring an out-of-band `cdk deploy` whenever its definition changes. The tradeoff is that the pipeline's own CodeBuild role necessarily holds broad permissions to modify pipeline infrastructure — acceptable here because the blast radius on the expensive side is capped by a separate, non-self-mutating control: the manual approval step sits between synth and any stack that actually spends money.",
    },
    'cloudwan': {
        "eyebrow": 'Multi-Region Network Backbone',
        "why": "A Transit Gateway is regional. Once app-vpc, provider-vpc, and a second workload footprint in us-east-2 all need a shared routing fabric with policy that travels with them across regions, TGW's per-region route tables and manual peering stop scaling as a management model. The org also already has vocabulary for this -- FastTrack, SkyPath, SkyTransit, Workload -- so the backbone needed to speak that language natively rather than force a translation layer on top of raw CIDRs and route tables.",
        "what": "cloudwan_stack.py stands up a CfnGlobalNetwork and CfnCoreNetwork spanning us-east-1 and us-east-2 with edge locations and distinct ASNs (64513 / 64514) per region, governed by a single policy document instead of per-region route tables. Four segments -- FastTrack, SkyPath, SkyTransit, Workload -- give each traffic class its own routing domain, and app-vpc, provider-vpc (us-east-1), and region2_stack.py's isolated Workload VPC (us-east-2) all attach into it through CfnVpcAttachment. The whole layer is feature-flagged off by default (ENABLE_CLOUDWAN) because a Cloud WAN core network bills per-attachment-hour even idle, so it's opt-in rather than always-on cost.",
        "how": "Routing is policy-as-code: the POLICY_DOCUMENT dict defines the four segments, and attachment-policies map each VPC into a segment purely by matching its `segment` tag -- no stack code manually wires an attachment to a segment. Workload is declared with isolate-attachments=true, so app-vpc and the us-east-2 Workload VPC can both be in the same segment yet cannot reach each other without an explicit share action -- that's the isolation half of the demo. SkyTransit is explicitly shared into FastTrack via a segment-action, showing the opposite pattern: controlled cross-segment reachability for hybrid/on-prem paths. The pre-existing Transit Gateway from network_stack.py is registered into the same CfnGlobalNetwork and peered with the core network via CfnTransitGatewayPeering -- both consistently reach AVAILABLE. What isn't included, and is documented rather than hidden, is the route-table-level CfnTransitGatewayRouteTableAttachment that would make routes actually exchange between the TGW and a segment: it failed with an opaque InvalidRequest across 6+ attempts ruling out the plausible causes (an ASN collision was real and fixed -- the edge ASNs start at 64513 specifically to stay clear of the TGW's own 64512 AmazonSideAsn -- plus live-vs-dedicated route tables and propagation timing were all ruled out).",
        "pattern_name": 'Segmented core network with incremental TGW-to-CloudWAN migration',
        "pattern_desc": "This is the standard path for an org migrating off Transit Gateway without a big-bang cutover: register and peer the legacy TGW into Cloud WAN's Global Network first, keep it serving existing attachments, and move new attachments (the two us-east-1 VPCs, the us-east-2 Workload VPC) onto the core network's segments directly. The tradeoff is that until route-table-level attachment works, the TGW and Cloud WAN sides are peered but not route-exchanging -- a real, currently-blocked AWS API gap, not a design choice, and the honest move is to say so rather than paper over it with a diagram that implies more than what's deployed.",
    },
    'agentic-ai': {
        "eyebrow": 'Governed Agentic Tool Access',
        "why": "A network landing zone is exactly the kind of surface you don't want an LLM improvising against — a hallucinated VPN status or a plausible-sounding routing change applied straight to production is a real failure mode, not a hypothetical one. At the same time, an agent with read access to actual signal (Lattice resource gateways, ALB target health, Security Hub findings) is genuinely useful for on-call triage, and Bedrock AgentCore's Gateway/Memory/Runtime/Workload Identity primitives are worth wiring up for real rather than mocked. Because AgentCore, S3 Vectors, and a Bedrock Knowledge Base all bill meaningfully even sitting idle, the whole layer sits behind config.ENABLE_AI and stays off by default.",
        "what": "It gives two Cognito-authenticated personas — network-operator (read-only) and connectivity-planner (read-only plus one proposal tool) — a working Bedrock Converse tool-use loop with real operational context: Kafka resource-gateway status, ALB/VPN health, Security Hub and Config compliance state, and semantic recall over prior sessions via a Titan-embedded Knowledge Base. It also proves the same 7 tools are reachable as a genuine MCP surface, not just wiring internal to one Lambda — so the tool layer isn't hard-coded to a single orchestrator implementation.",
        "how": "API Gateway's agentic-ai-api HTTP API sits behind a Cognito user pool and HttpJwtAuthorizer on every route, POSTing to one of two Lambda orchestrators (agentic-ai-orchestrator-network-operator / -connectivity-planner) that each run the Bedrock Converse loop against config.BEDROCK_MODEL_ID. Memory is three explicit tiers: DynamoDB (agentic-ai-working-memory — TTL'd, CMK-encrypted, PITR on) for the live conversation, an append-only S3 JSONL transcript per session for durable audit, and an S3 Vectors bucket/index behind a Bedrock Knowledge Base (Titan Embed Text v2, 1024 dims) for semantic search. The 7 MCP tool Lambdas are each least-privilege — query_kafka gets only vpc-lattice:Get*/ec2:DescribeInstanceStatus, query_governance only securityhub:GetFindings/config:Describe* — and are wired two ways: invoked directly by the orchestrators via lambda:InvokeFunction, and registered as CfnGatewayTarget entries on a real CfnGateway (agentic-ai-mcp-gateway, protocol MCP) under a CfnWorkloadIdentity. The persona split is enforced in IAM, not the prompt: only the connectivity-planner orchestrator role is granted invoke on propose_connectivity, and that Lambda's own role is scoped to nothing but codecommit:CreateBranch/CreateCommit/CreatePullRequest against config.CODECOMMIT_REPO_NAME — it can open a PR, it cannot touch a single piece of live infrastructure.",
        "pattern_name": 'Least-privilege MCP tool broker with human-gated GitOps writes',
        "pattern_desc": "Instead of trusting a system prompt to keep an agent read-only, each persona gets its own IAM role wired to an explicit tool allow-list, so the boundary holds even against a confused or adversarially-prompted model rather than the model's own judgment. The one tool capable of any state change is deliberately shaped as a CodeCommit pull request instead of a direct mutation — slower iteration (a human still has to merge) in exchange for every proposed change being reviewable, revertible, and never silently applied.",
    },
    'sagemaker': {
        "eyebrow": 'Cost-Gated ML Anomaly Pipeline',
        "why": "VPC Flow Logs carry exactly the traffic-pattern signal a zero-trust network needs to police, but SageMaker training and transform jobs have no CloudFormation resource type -- CreateTrainingJob, CreateModel, CreateEndpoint are one-shot boto3 calls, not declarative infrastructure. A naive implementation either pays for an always-on inference endpoint around the clock or loses IaC's guarantee that `cdk destroy` actually removes everything it created. That cost/lifecycle tension is why this whole layer is feature-flagged off by default -- it only exists when ENABLE_SAGEMAKER is explicitly turned on.",
        "what": "Continuous, unattended model refresh: an ec2.FlowLog streams app-vpc traffic into the SageMaker data bucket's raw/ prefix, a daily Lambda flattens it into numeric-feature CSV under processed/, and a weekly training job produces a new Random Cut Forest generation with no one running a notebook. Two independent paths then consume whichever model is currently promoted -- a scale-to-zero Async Inference endpoint for on-demand scoring, and a 6-hourly batch-transform sweep for systematic bulk coverage of everything that accumulated. Findings that clear the anomaly-score threshold land in DynamoDB, S3, and SNS -- the same DynamoDB table the Agentic AI layer's detect_anomalies MCP tool reads, so that agent queries real RCF output instead of a mock.",
        "how": "The model promoter Lambda is wired to an EventBridge rule on SageMaker Training Job State Change, filtered to the rcf-flowlog-training- name prefix: on the first successful training run it calls CreateModel/CreateEndpointConfig/CreateEndpoint and registers Application Auto Scaling down to zero instances; every run after that is an UpdateEndpoint against the same endpoint plus a delete of the previous generation's model and config, so exactly one RCF generation is ever live. Inference is Async, not Serverless, because Serverless Inference can't take a vpc_config at all -- scoring live flow-log data from an endpoint sitting outside app-vpc's boundary would break the project's zero-trust posture -- and Async is the only endpoint type that's both VPC-attached, through the sagemaker.api and sagemaker.runtime interface endpoints, and cost-bounded at rest. The batch scorer runs independently on its own 6-hour schedule, calling CreateTransformJob against processed/ with whichever model is currently promoted, deliberately decoupled so bulk scoring never competes with or depends on live inference capacity. The findings processor triggers off the matching Transform Job State Change rule, applies an anomaly-score threshold, and writes qualifying results to a DynamoDB table with on-demand billing, customer-managed-key encryption, and point-in-time recovery, plus S3 and an SNS topic. Because none of those Create/Update/Delete calls are CloudFormation resources, a teardown Lambda behind a Custom Resource Provider lists and deletes every name-prefix-matching endpoint, model, endpoint config, and autoscaling target on cdk destroy, so nothing this pipeline creates at runtime outlives the stack and keeps billing.",
        "pattern_name": 'Event-driven model promotion onto a scale-to-zero async inference endpoint',
        "pattern_desc": "A training job's completion event -- not a human, not a CI pipeline -- triggers the swap of a new model generation onto the serving endpoint, and Application Auto Scaling's scale-to-zero keeps idle-endpoint cost at zero between invocations. The tradeoff is first-request latency: a scaled-down Async endpoint has to spin an instance back up before it answers, which is acceptable for this workload precisely because the 6-hourly batch-transform path exists to absorb the systematic, non-interactive scoring that can't pay that tax.",
    },
}

PANELS = [
    ("Core Network &amp; Traffic Inspection", "network_stack.py &middot; inspection_stack.py",
     "network-inspection", build_svg),
    ("Application Path -- Three-Tier + PrivateLink", "privatelink_stack.py &middot; threetier_stack.py",
     "application-path", build_application_path_svg),
    ("Blog Read Analytics", "blog_analytics_stack.py &middot; API Gateway + Lambda + DynamoDB behind the same CloudFront distribution",
     "blog-analytics", build_blog_analytics_svg),
    ("Blog Chat Assistant", "blog_assistant_stack.py &middot; Bedrock Knowledge Base (S3 Vectors) + Claude + S3 memory",
     "blog-assistant", build_blog_assistant_svg),
    ("VPC Lattice Service Network", "lattice_stack.py",
     "lattice", build_lattice_svg),
    ("Security &amp; Governance", "security_stack.py &middot; governance_stack.py &middot; drift_remediation_stack.py",
     "security-governance", build_governance_svg),
    ("CI/CD -- Self-Mutating Pipeline", "pipeline_stack.py &middot; deploys every stack on this page",
     "pipeline", build_pipeline_svg),
    ("Multi-Region AWS Cloud WAN", "cloudwan_stack.py &middot; region2_stack.py &middot; gated behind ENABLE_CLOUDWAN (see README.md's cost warning)",
     "cloudwan", build_cloudwan_svg),
    ("Agentic AI Layer", "agentic_ai_stack.py &middot; gated behind ENABLE_AI (see README.md's cost warning)",
     "agentic-ai", build_agentic_ai_svg),
    ("SageMaker Anomaly Detection", "sagemaker_stack.py &middot; gated behind ENABLE_SAGEMAKER (see README.md's cost warning)",
     "sagemaker", build_sagemaker_svg),
]


# One-line hook per layer, used in the sidebar and nowhere else -- the full
# story is the WHY/WHAT/HOW/PATTERN prose under each diagram.
DEK = {
    "network-inspection": "Every spoke's traffic transits one centralized, auto-scaled firewall tier before reaching its destination.",
    "application-path": "Zero public compute -- CloudFront and PrivateLink do all the boundary-crossing.",
    "lattice": "One mesh, three routing primitives -- EC2, Lambda, and an existing ALB -- unified behind IAM-authenticated rules.",
    "security-governance": "A permissions boundary as a hard ceiling, plus three independent detection-and-remediation loops.",
    "pipeline": "The pipeline redeploys its own definition before it deploys anything else.",
    "cloudwan": "An incremental migration path onto Cloud WAN, run alongside the existing Transit Gateway.",
    "agentic-ai": "Two AI personas with real, gateway-registered tool access -- one can propose changes, none can make them.",
    "sagemaker": "Unsupervised anomaly detection on live VPC Flow Logs, promoted automatically on every retrain.",
}

SHELL_CSS = """
:root {
  --line: #232a35;
  --surface-raised: #171c25;
  --accent: #d68c4a;
  --accent-strong: #f0a866;
  --accent-soft: rgba(214, 140, 74, 0.14);
  --mono: ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Consolas, monospace;
  --sidebar-w: 272px;
  --good: #3fb950;
  --bad: #f85149;
}
:root[data-theme="light"] {
  --bg: #f7f6f2; --surface: #ffffff; --surface-tint: #efece5; --surface-raised: #efece5;
  --text: #1a1d23; --text-muted: #5c6270; --boundary-stroke: #c9c3b8; --box-stroke: #6b7280;
  --line: #e2ddd2; --accent: #b8631f; --accent-strong: #9a5119; --accent-soft: rgba(184, 99, 31, 0.1);
  --good: #1f883d; --bad: #cf222e;
}
:root[data-theme="dark"] {
  --bg: #0a0d12; --surface: #12161d; --surface-tint: #171c25; --surface-raised: #171c25;
  --text: #e9edf3; --text-muted: #8892a0; --boundary-stroke: #3a4250; --box-stroke: #4a5568;
  --line: #232a35; --accent: #d68c4a; --accent-strong: #f0a866; --accent-soft: rgba(214, 140, 74, 0.14);
  --good: #3fb950; --bad: #f85149;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0a0d12; --surface: #12161d; --surface-tint: #171c25; --surface-raised: #171c25;
    --text: #e9edf3; --text-muted: #8892a0; --boundary-stroke: #3a4250; --box-stroke: #4a5568;
    --line: #232a35; --accent: #d68c4a; --accent-strong: #f0a866; --accent-soft: rgba(214, 140, 74, 0.14);
    --good: #3fb950; --bad: #f85149;
  }
}

.shell { display: grid; grid-template-columns: var(--sidebar-w) minmax(0, 1fr); align-items: start; max-width: 1400px; margin: 0 auto; }
.nav-toggle-input { display: none; }
.nav-toggle-label { display: none; }

.sidebar {
  position: sticky; top: 0; height: 100vh; overflow-y: auto;
  padding: 28px 20px; border-right: 1px solid var(--line);
  display: flex; flex-direction: column; gap: 2px;
}
.brand { display: flex; align-items: baseline; gap: 8px; margin: 0 0 28px; text-decoration: none; color: inherit; }
.brand-mark { color: var(--accent); font-size: 18px; }
.brand-name { font-family: var(--mono); font-weight: 600; font-size: 14px; letter-spacing: -0.01em; }
.sidebar-label { font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-muted); margin: 20px 0 8px; padding: 0 10px; }
.sidebar a.sidebar-link {
  display: flex; align-items: baseline; gap: 10px; padding: 7px 10px; border-radius: 6px;
  color: var(--text-muted); text-decoration: none; font-size: 13.5px; line-height: 1.3;
}
.sidebar a.sidebar-link:hover { background: var(--surface-tint); color: var(--text); }
.sidebar a.sidebar-link .num { font-family: var(--mono); font-size: 11px; color: var(--text-dim, var(--text-muted)); opacity: 0.7; flex: none; }
.sidebar a.overview-link { color: var(--text); font-weight: 600; }
.sidebar-footer { margin-top: auto; padding: 10px; font-size: 12px; color: var(--text-muted); border-top: 1px solid var(--line); padding-top: 16px; }
.sidebar-footer a { color: var(--accent); text-decoration: none; }

.main { min-width: 0; padding: 0 40px 80px; }

.hero { padding: 56px 0 40px; border-bottom: 1px solid var(--line); margin-bottom: 48px; }
.hero-eyebrow { font-family: var(--mono); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent); margin: 0 0 16px; }
.hero h1 { font-size: 40px; line-height: 1.12; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 10px; text-wrap: balance; }
.hero-subtitle { font-size: 18px; line-height: 1.4; font-weight: 500; color: var(--accent-strong); margin: 0 0 20px; text-wrap: balance; max-width: 62ch; }
.hero-dek { font-size: 17px; line-height: 1.65; color: var(--text-muted); max-width: 68ch; margin: 0 0 32px; }
.vitals { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 1px; background: var(--line); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; max-width: 760px; }
.vitals > div { background: var(--surface); padding: 16px 18px; }
.vitals dt { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin: 0 0 6px; }
.vitals dd { font-family: var(--mono); font-size: 24px; font-weight: 600; margin: 0; font-variant-numeric: tabular-nums; }

.status-row { display: flex; align-items: center; gap: 12px; margin: 24px 0 0; font-size: 13px; }
.status-pill { display: inline-flex; align-items: center; gap: 7px; padding: 6px 14px; border-radius: 999px; font-weight: 600; font-size: 13px; background: var(--surface-tint); color: var(--text-muted); border: 1px solid var(--line); }
.status-pill .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.status-pill[data-state="ok"] { color: var(--good); border-color: color-mix(in srgb, var(--good) 40%, var(--line)); }
.status-pill[data-state="error"] { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); }
.status-caption { color: var(--text-muted); font-family: var(--mono); font-size: 12px; }

.legend { list-style: none; padding: 0; margin: 0 0 48px; }
.legend ul { list-style: none; padding: 0; margin: 0; display: flex; flex-wrap: wrap; gap: 12px 24px; }
.legend li { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--text-muted); }

.layer { padding: 56px 0; border-bottom: 1px solid var(--line); scroll-margin-top: 24px; }
.layer:last-of-type { border-bottom: none; }
.layer-eyebrow { font-family: var(--mono); font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--accent); margin: 0 0 10px; }
.layer h2 { font-size: 28px; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 8px; text-wrap: balance; }
.layer-files { font-family: var(--mono); font-size: 12.5px; color: var(--text-muted); margin: 0 0 28px; }

.diagram-scroll { overflow-x: auto; overflow-y: hidden; -webkit-overflow-scrolling: touch; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); position: relative; margin-bottom: 32px; }
svg { display: block; width: 100%; min-width: 900px; height: auto; }

.prose-row { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
.prose-card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 20px 22px; }
.prose-card h3 { font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-muted); margin: 0 0 10px; font-weight: 600; }
.prose-card p { font-size: 14.5px; line-height: 1.65; margin: 0; color: var(--text); }
.prose-card--how p { font-size: 14.5px; }
.prose-card--pattern { background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 35%, var(--line)); border-left: 3px solid var(--accent); }
.prose-card--pattern h3 { color: var(--accent-strong); }
.pattern-name { font-weight: 700; font-size: 15.5px; margin: 0 0 8px !important; }

footer.page-footer { margin-top: 16px; font-size: 12.5px; color: var(--text-muted); line-height: 1.6; max-width: 68ch; }

@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  .nav-toggle-label {
    display: flex; align-items: center; justify-content: center; width: 36px; height: 36px;
    border: 1px solid var(--line); border-radius: 8px; cursor: pointer; font-size: 16px;
    position: fixed; top: 16px; right: 16px; z-index: 20; background: var(--surface);
  }
  .sidebar {
    position: fixed; inset: 0 20% 0 0; z-index: 15; transform: translateX(-100%);
    transition: transform 0.2s ease; background: var(--bg); box-shadow: 2px 0 24px rgba(0,0,0,0.3);
  }
  .nav-toggle-input:checked ~ .shell .sidebar { transform: translateX(0); }
  .main { padding: 0 20px 60px; }
  .hero { padding-top: 72px; }
  .hero h1 { font-size: 32px; }
  .prose-row { grid-template-columns: 1fr; }
}
"""


def build_sidebar() -> str:
    links = []
    for i, (title, _sub, anchor, _fn) in enumerate(PANELS, start=1):
        links.append(f'<a class="sidebar-link" href="#{anchor}"><span class="num">{i:02d}</span>{title}</a>')
    return f'''<nav class="sidebar" aria-label="Sections">
  <a class="brand" href="#top"><span class="brand-mark">&#9671;</span><span class="brand-name">lattice-lab</span></a>
  <a class="sidebar-link overview-link" href="#top">Overview</a>
  <p class="sidebar-label">Architecture</p>
  {"".join(links)}
  <div class="sidebar-footer">Deployed through the self-mutating CDK Pipeline &mdash; see the CI/CD panel.</div>
</nav>'''


def build_layer_section(title: str, sub: str, anchor: str, svg: str) -> str:
    p = PROSE.get(anchor)
    dek = DEK.get(anchor, "")
    prose_html = ""
    if p:
        prose_html = f'''  <div class="prose-row">
    <div class="prose-card prose-card--why"><h3>Why</h3><p>{esc(p["why"])}</p></div>
    <div class="prose-card prose-card--what"><h3>What it solves</h3><p>{esc(p["what"])}</p></div>
  </div>
  <div class="prose-row" style="grid-template-columns: 1fr;">
    <div class="prose-card prose-card--how"><h3>How</h3><p>{esc(p["how"])}</p></div>
  </div>
  <div class="prose-row" style="grid-template-columns: 1fr;">
    <div class="prose-card prose-card--pattern"><h3>Architecture pattern</h3><p class="pattern-name">{esc(p["pattern_name"])}</p><p>{esc(p["pattern_desc"])}</p></div>
  </div>'''
    eyebrow = f'<p class="layer-eyebrow">{esc(p["eyebrow"])}</p>' if p else ""
    return f'''<section class="layer" id="{anchor}">
  {eyebrow}
  <h2>{title}</h2>
  <p class="layer-files">{sub}{" -- " + esc(dek) if dek else ""}</p>
  <div class="diagram-scroll">
    {svg}
  </div>
{prose_html}
</section>
'''


def _render_panel_sections() -> str:
    return "".join(build_layer_section(title, sub, anchor, build_fn()) for title, sub, anchor, build_fn in PANELS)


def _legend_nav() -> str:
    return '''<nav class="legend" aria-label="Diagram legend">
  <ul>
    <li><span class="swatch swatch-ns"></span> North-South &mdash; Internet ingress/egress</li>
    <li><span class="swatch swatch-ew"></span> East-West &mdash; Inter-VPC via Transit Gateway</li>
    <li><span class="swatch dashed swatch-mgmt"></span> Management / Control Plane</li>
    <li><span class="swatch dashed swatch-meta"></span> Page Delivery (CloudFront + S3)</li>
    <li><span class="swatch dashed swatch-boundary"></span> VPC / AZ boundary</li>
  </ul>
</nav>'''


STACK_COUNT_NOTE = "12 core + 5 optional"
VITALS = [
    ("VPCs", "4"),
    ("Availability Zones", str(AZ_COUNT)),
    ("CDK Stacks", STACK_COUNT_NOTE),
    ("Architecture Layers", str(len(PANELS))),
]

PAGE_TITLE = (
    "Hybrid VPC Lattice Landing Zone: A Multi-Region AWS Reference Architecture for "
    "Cloud WAN, VPC Lattice Service Mesh, Transit Gateway, and Agentic AI/ML-Driven Network Operations"
)
BROWSER_TITLE = "Hybrid VPC Lattice Landing Zone -- Cloud WAN, VPC Lattice, Transit Gateway & Agentic AI/ML | lattice-lab"

HERO_THESIS = (
    "A hands-on, enterprise-style landing zone built to demonstrate real infrastructure judgment, not a tutorial "
    "checklist: a hub-and-spoke network with mandatory centralized inspection, a zero-trust VPC Lattice service "
    "mesh running three different routing primitives behind one auth model, a permissions boundary enforced as a "
    "hard ceiling rather than an allow-list, a self-mutating CDK Pipeline that redeploys its own definition "
    "before it deploys anything else, a multi-region AWS Cloud WAN backbone layered onto the existing Transit "
    "Gateway, and an Agentic AI / machine-learning layer -- Bedrock AgentCore tool-calling agents plus a "
    "SageMaker anomaly-detection pipeline -- that can read and propose changes to the network but never make "
    "one unsupervised. Every diagram on this page reflects what is actually defined in code, not an "
    "aspirational architecture."
)


def build_html() -> str:
    icon_defs_svg = f'<svg width="0" height="0" style="position:absolute" aria-hidden="true">{aws_icon_defs()}</svg>'
    sections = _render_panel_sections()
    vitals_html = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in VITALS)
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{BROWSER_TITLE}</title>
<style>{CSS}
{SHELL_CSS}</style>
</head>
<body>
{icon_defs_svg}
<input type="checkbox" id="nav-toggle" class="nav-toggle-input">
<label for="nav-toggle" class="nav-toggle-label">&#9776;</label>
<div class="shell">
{build_sidebar()}
<div class="main" id="top">
<header class="hero">
  <p class="hero-eyebrow">AWS CDK &middot; Python &middot; Self-Mutating Pipeline</p>
  <h1>Hybrid VPC Lattice Landing Zone</h1>
  <p class="hero-subtitle">A Multi-Region AWS Reference Architecture for Cloud WAN, VPC Lattice Service Mesh, Transit Gateway, and Agentic AI/ML-Driven Network Operations</p>
  <p class="hero-dek">{HERO_THESIS}</p>
  <dl class="vitals">{vitals_html}</dl>
</header>
{_legend_nav()}
<main>
{sections}</main>
<footer class="page-footer">
  The CloudFront/S3 panel inside the first diagram shows how this page is delivered to your browser &mdash; it is not part of the depicted network. Service icons are the official AWS Architecture Icons where AWS ships one for that service, and hand-built icons in the identical visual language (same category colors) where it does not -- see diagram-site/aws_icons.py.
</footer>
</div>
</div>
</body>
</html>
'''


def write_standalone_panel_svgs(out_dir: Path) -> None:
    """Each panel's SVG, fully self-contained (its own embedded icon defs
    AND its own embedded copy of the page CSS), for embedding as a plain
    <img> outside this page -- the blog post at blog-hybrid-cloud-airport-
    story/ and app/frontend-next/public/diagrams/ both reference these.
    An <img>-loaded SVG is its own isolated document: it has no access to
    the parent page's <style> block, so every class (.box-service etc) and
    CSS custom property (--bg, --surface, ...) this file's markup uses has
    to be defined again right here, or the diagram renders with no colors
    or strokes at all. :root inside an SVG's own <style> refers to the
    SVG's own root when parsed standalone, and @media
    (prefers-color-scheme) still evaluates against the real OS/browser
    setting, so the same CSS constant works unmodified."""
    out_dir.mkdir(parents=True, exist_ok=True)
    defs = aws_icon_defs()
    for _title, _sub, anchor, build_fn in PANELS:
        svg = build_fn()
        insert_at = svg.index(">") + 1
        standalone = svg[:insert_at] + f"<defs>{defs}</defs><style>{CSS}</style>" + svg[insert_at:]
        path = out_dir / f"{anchor}.svg"
        path.write_text(standalone)
        print(f"Wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    out_path = Path(__file__).parent / "index.html"
    out_path.write_text(build_html())
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")

    write_standalone_panel_svgs(Path(__file__).parent / "technical-diagrams")
