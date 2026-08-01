#!/usr/bin/env python3
"""generate_illustrations.py -- simple, friendly airport-themed SVG
illustrations for the blog post (blog-hybrid-cloud-airport-story/POST.md).

Deliberately NOT the technical AWS-icon diagrams from diagram-site/ -- those
are for engineers; these are for a reader who has never opened the AWS
console, so they use plain airport iconography (planes, terminals, a
control tower, a checkpoint) instead of service icons. Each function
writes one standalone .svg file, referenced from the post via normal
markdown image syntax (GitHub renders standalone .svg files fine; it does
not render raw <svg> embedded directly in markdown).

Run: python3 generate_illustrations.py
"""

from pathlib import Path

OUT_DIR = Path(__file__).parent / "images"

# Friendly, warm palette -- distinct from the technical diagram site on
# purpose (this is a different artifact, a blog, not the engineering docs).
SKY = "#eaf4fb"
SKY_DARK = "#0e2a3d"
GROUND = "#2f6f5c"
NAVY = "#1b2a41"
TOWER = "#d6742f"
GOLD = "#e8a33d"
WHITE = "#ffffff"
LINE = "#41546b"
RED = "#c0392b"


def svg_wrap(w, h, body, title="") -> str:
    return (
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
        f'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif" role="img" '
        f'aria-label="{title}">'
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="{SKY}"/>'
        f'{body}</svg>'
    )


def plane(x, y, scale=1.0, color=NAVY, rotate=0) -> str:
    return (f'<g transform="translate({x},{y}) scale({scale}) rotate({rotate})" fill="{color}">'
            '<path d="M0 -3 L4 3 L14 8 L14 11 L2 8 L0 20 L5 24 L5 27 L0 25 L-5 27 L-5 24 L0 20 L-2 8 L-14 11 '
            'L-14 8 L-4 3 Z"/></g>')


def terminal(x, y, w, h, label, color=WHITE, stroke=LINE) -> str:
    roof_h = 14
    return (
        f'<g>'
        f'<rect x="{x}" y="{y+roof_h}" width="{w}" height="{h-roof_h}" rx="4" fill="{color}" stroke="{stroke}" stroke-width="2"/>'
        f'<polygon points="{x-8},{y+roof_h} {x+w/2},{y-6} {x+w+8},{y+roof_h}" fill="{stroke}"/>'
        f'{"".join(f"<rect x=\"{x+10+i*(w-20)/4}\" y=\"{y+roof_h+10}\" width=\"{(w-20)/4-6}\" height=\"14\" rx=\"2\" fill=\"{SKY}\" stroke=\"{stroke}\"/>" for i in range(4))}'
        f'<text x="{x+w/2}" y="{y+h+20}" text-anchor="middle" font-size="13" font-weight="700" fill="{NAVY}">{label}</text>'
        f'</g>'
    )


def tower(x, y, label) -> str:
    return (
        f'<g>'
        f'<rect x="{x-6}" y="{y+30}" width="12" height="50" fill="{TOWER}"/>'
        f'<ellipse cx="{x}" cy="{y+22}" rx="22" ry="16" fill="{TOWER}"/>'
        f'<ellipse cx="{x}" cy="{y+20}" rx="16" ry="10" fill="{SKY}" stroke="{NAVY}" stroke-width="1.5"/>'
        f'<circle cx="{x}" cy="{y+2}" r="4" fill="{RED}"/>'
        f'<text x="{x}" y="{y+98}" text-anchor="middle" font-size="13" font-weight="700" fill="{NAVY}">{label}</text>'
        f'</g>'
    )


def shield(x, y, size, color=RED) -> str:
    s = size
    return (f'<path transform="translate({x-s/2},{y-s/2})" fill="{color}" '
            f'd="M{s/2} 0 L{s} {s*0.18} V{s*0.5} C{s} {s*0.78} {s*0.72} {s*0.94} {s/2} {s} '
            f'C{s*0.28} {s*0.94} 0 {s*0.78} 0 {s*0.5} V{s*0.18} Z"/>')


def taxiway(points, color=LINE, width=4, dashed=False) -> str:
    d = f"M {points[0][0]} {points[0][1]} " + " ".join(f"L {p[0]} {p[1]}" for p in points[1:])
    dash = ' stroke-dasharray="10 6"' if dashed else ""
    return f'<path d="{d}" stroke="{color}" stroke-width="{width}" fill="none"{dash}/>'


def label(x, y, text, size=12, color=NAVY, weight=600, anchor="middle") -> str:
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" font-weight="{weight}" fill="{color}">{text}</text>'


# ---------------------------------------------------------------------------
# 1. Master "airport map" -- the whole analogy in one picture
# ---------------------------------------------------------------------------
def master_map():
    w, h = 1400, 940
    body = []

    defs = (
        '<defs>'
        f'<linearGradient id="mm-sky" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="#cfe8f7"/>'
        f'<stop offset="55%" stop-color="{SKY}"/>'
        f'<stop offset="100%" stop-color="#fbe8d3"/>'
        '</linearGradient>'
        f'<radialGradient id="mm-sun" cx="50%" cy="50%" r="50%">'
        f'<stop offset="0%" stop-color="#fff6df" stop-opacity="0.95"/>'
        f'<stop offset="45%" stop-color="{GOLD}" stop-opacity="0.55"/>'
        f'<stop offset="100%" stop-color="{GOLD}" stop-opacity="0"/>'
        '</radialGradient>'
        f'<radialGradient id="mm-beacon" cx="50%" cy="50%" r="50%">'
        f'<stop offset="0%" stop-color="{RED}" stop-opacity="0.75"/>'
        f'<stop offset="100%" stop-color="{RED}" stop-opacity="0"/>'
        '</radialGradient>'
        f'<linearGradient id="mm-ground" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="#3a4a5c"/>'
        f'<stop offset="100%" stop-color="#2a3745"/>'
        '</linearGradient>'
        '<filter id="mm-shadow" x="-30%" y="-30%" width="160%" height="160%">'
        '<feDropShadow dx="0" dy="6" stdDeviation="7" flood-color="#1b2a41" flood-opacity="0.22"/>'
        '</filter>'
        '</defs>'
    )
    body.append(defs)
    body.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="url(#mm-sky)"/>')
    body.append(f'<circle cx="{w-160}" cy="90" r="130" fill="url(#mm-sun)"/>')
    body.append(f'<circle cx="{w-160}" cy="90" r="30" fill="{WHITE}" opacity="0.85"/>')

    # soft clouds
    for cx, cy, s in [(220, 70, 1.0), (760, 55, 0.75), (1040, 130, 0.6), (430, 130, 0.55)]:
        body.append(
            f'<g transform="translate({cx},{cy}) scale({s})" fill="{WHITE}" opacity="0.75">'
            '<ellipse cx="0" cy="0" rx="46" ry="18"/><ellipse cx="34" cy="-6" rx="30" ry="15"/>'
            '<ellipse cx="-32" cy="4" rx="26" ry="13"/></g>'
        )

    # distant hills for depth
    body.append(f'<path d="M0 {h-190} Q {w*0.18} {h-260} {w*0.36} {h-195} T {w*0.7} {h-205} T {w} {h-185} '
                 f'V{h-150} H0 Z" fill="{GROUND}" opacity="0.14"/>')

    # tarmac / apron ground band with centerline + hold-short chevrons
    apron_y = h - 150
    body.append(f'<rect x="0" y="{apron_y}" width="{w}" height="150" fill="url(#mm-ground)"/>')
    body.append(f'<line x1="40" y1="{h-70}" x2="{w-40}" y2="{h-70}" stroke="{GOLD}" stroke-width="4" '
                f'stroke-dasharray="26 18" opacity="0.85"/>')
    for cx in range(120, w - 60, 160):
        body.append(f'<path d="M{cx} {h-40} l14 -16 l14 16 Z" fill="{GOLD}" opacity="0.55"/>')

    # compass rose + airport code badge, top-left
    body.append(
        f'<g transform="translate(90,86)">'
        f'<circle r="46" fill="{WHITE}" opacity="0.9" stroke="{NAVY}" stroke-width="2"/>'
        f'<path d="M0 -36 L8 0 L0 36 L-8 0 Z" fill="{TOWER}"/>'
        f'<path d="M-36 0 L0 -8 L36 0 L0 8 Z" fill="{NAVY}" opacity="0.55"/>'
        f'<text x="0" y="-52" text-anchor="middle" font-size="12" font-weight="800" fill="{NAVY}">N</text>'
        '</g>'
    )
    body.append(
        f'<g transform="translate(40,150)">'
        f'<rect x="0" y="0" width="112" height="34" rx="8" fill="{NAVY}"/>'
        f'<text x="56" y="23" text-anchor="middle" font-size="15" font-weight="800" fill="{WHITE}" '
        f'letter-spacing="2">AWS-1</text></g>'
    )

    def hangar(x, y, w_, h_, label_top, label_bottom, accent=TOWER):
        roof_h = 16
        body.append(f'<g filter="url(#mm-shadow)">')
        body.append(f'<rect x="{x}" y="{y+roof_h}" width="{w_}" height="{h_-roof_h}" rx="6" fill="{WHITE}" stroke="{LINE}" stroke-width="2"/>')
        body.append(f'<polygon points="{x-10},{y+roof_h} {x+w_/2},{y-8} {x+w_+10},{y+roof_h}" fill="{accent}"/>')
        body.append(f'<rect x="{x+w_/2-3}" y="{y-24}" width="6" height="18" fill="{LINE}"/>')
        body.append(f'<circle cx="{x+w_/2}" cy="{y-28}" r="4" fill="{RED}"/>')
        rows, cols = 2, 4
        cell_w = (w_ - 24) / cols
        for r in range(rows):
            for c in range(cols):
                cx = x + 12 + c * cell_w
                cy = y + roof_h + 14 + r * 26
                body.append(f'<rect x="{cx}" y="{cy}" width="{cell_w-8}" height="16" rx="2" fill="{SKY}" stroke="{accent}" stroke-width="1.2"/>')
        body.append(f'<rect x="{x+w_/2-16}" y="{y+h_-4}" width="32" height="14" fill="{LINE}" opacity="0.5"/>')
        body.append('</g>')
        body.append(label(x + w_/2, y + h_ + 30, label_top, 15, NAVY, 800))
        if label_bottom:
            body.append(label(x + w_/2, y + h_ + 48, label_bottom, 11.5, LINE, 600))

    cx_mid = w / 2

    # planes in flight (internet traffic), with light contrails -- drawn
    # early and confined to sky pockets clear of the title, compass, sun,
    # and every building below (checked against each of those boxes).
    def flying_plane(px, py, scale, rot, trail_dx, trail_dy):
        body.append(f'<path d="M{px} {py} q {trail_dx*0.5} {trail_dy*0.2} {trail_dx} {trail_dy}" '
                     f'stroke="{WHITE}" stroke-width="3" fill="none" opacity="0.7" stroke-linecap="round" stroke-dasharray="1 10"/>')
        body.append(plane(px, py, scale, NAVY, rotate=rot))

    flying_plane(985, 150, 1.15, 20, 60, -55)
    flying_plane(235, 230, 1.0, -30, -75, 45)
    flying_plane(1155, 380, 0.9, 150, 70, -60)

    # title, clear of every plane above
    body.append(label(cx_mid, 42, "The Airport", 27, NAVY, 800, "middle"))
    body.append(label(cx_mid, 67, "your AWS account, seen from the tower", 14.5, LINE, 600, "middle"))

    # control tower, centered above the checkpoint
    tower_x, tower_y = cx_mid, 178
    body.append(f'<circle cx="{tower_x}" cy="{tower_y+30}" r="68" fill="url(#mm-beacon)"/>')
    body.append(tower(tower_x, tower_y, "Control Tower (Transit Gateway)"))

    # row 1 -- the three main terminals
    row1_y, row1_h = 330, 150
    ck_x, ck_w = cx_mid - 115, 230
    app_x, app_w = 110, 220
    prov_x, prov_w = w - 330, 220

    hangar(app_x, row1_y, app_w, row1_h, "App Terminal", "on-demand gate desk, no standing crew", accent=TOWER)
    hangar(ck_x, row1_y, ck_w, row1_h, "Inspection Checkpoint", "every flight path, one firewall tier", accent=RED)
    hangar(prov_x, row1_y, prov_w, row1_h, "Provider Terminal", "a courier hatch, one-way only", accent="#5a7ea6")
    body.append(shield(ck_x + ck_w / 2, row1_y + 90, 44))

    taxi_y = row1_y + 95
    body.append(taxiway([(app_x + app_w, taxi_y), (ck_x, taxi_y)], width=7))
    body.append(taxiway([(ck_x + ck_w, taxi_y), (prov_x, taxi_y)], width=7))
    body.append(taxiway([(cx_mid, row1_y - 8), (cx_mid, tower_y + 78)], width=7))

    # row 2 -- three supporting facilities, each tied to a row-1 terminal
    row2_y, row2_h = 610, 100
    vault_x, vault_w = 130, 190
    reg_x, reg_w = 400, 230
    arr_x, arr_w = 720, 210

    body.append(f'<g filter="url(#mm-shadow)">')
    body.append(f'<rect x="{vault_x}" y="{row2_y}" width="{vault_w}" height="{row2_h}" rx="10" fill="{WHITE}" stroke="{LINE}" stroke-width="2"/>')
    body.append(f'<circle cx="{vault_x+vault_w/2}" cy="{row2_y+row2_h/2}" r="28" fill="none" stroke="{TOWER}" stroke-width="7"/>')
    body.append(f'<circle cx="{vault_x+vault_w/2}" cy="{row2_y+row2_h/2}" r="6" fill="{TOWER}"/>')
    body.append('</g>')
    body.append(label(vault_x + vault_w / 2, row2_y + row2_h + 26, "Baggage Vault", 14, NAVY, 800))
    body.append(label(vault_x + vault_w / 2, row2_y + row2_h + 44, "DynamoDB, gate access only", 11.5, LINE, 600))
    body.append(taxiway([(vault_x + vault_w / 2, row2_y), (app_x + app_w / 2, row1_y + row1_h)],
                         color=TOWER, width=3, dashed=True))

    hangar(reg_x, row2_y, reg_w, row2_h, "Regional Airstrip", "on-prem, reached by charter", accent=GOLD)
    body.append(taxiway([(reg_x + reg_w / 2, row2_y), (ck_x + ck_w / 2 - 10, row1_y + row1_h)],
                         color=TOWER, width=4, dashed=True))
    body.append(label(reg_x + reg_w / 2 + 90, row2_y - 16, "chartered VPN flight", 11.5, TOWER, 700))

    hangar(arr_x, row2_y, arr_w, row2_h, "Arrivals Hall", "the public site, front doors only", accent=GOLD)
    body.append(taxiway([(arr_x + arr_w / 2, row2_y), (app_x + app_w - 20, row1_y + row1_h)],
                         color=GOLD, width=4))

    body.append(f'<rect x="6" y="6" width="{w-12}" height="{h-12}" rx="18" fill="none" stroke="{NAVY}" stroke-width="2" opacity="0.15"/>')
    return svg_wrap(w, h, "".join(body), "Airport map of the whole architecture")


# ---------------------------------------------------------------------------
# 2. Checkpoint (network + inspection)
# ---------------------------------------------------------------------------
def checkpoint():
    w, h = 900, 420
    body = []
    body.append(terminal(340, 140, 220, 140, "Inspection Terminal"))
    for i, dx in enumerate((-70, 0, 70)):
        body.append(shield(450 + dx, 220, 40, RED if i != 1 else GOLD))
    body.append(label(450, 300, "2 screening lanes per zone -- always-on, auto-scaled", 12, NAVY, 600))
    body.append(plane(120, 100, 1.3, NAVY, rotate=90))
    body.append(taxiway([(160, 100), (330, 210)], width=5))
    body.append(plane(780, 340, 1.3, NAVY, rotate=-135))
    body.append(taxiway([(560, 210), (760, 330)], width=5))
    body.append(label(450, 40, "Every flight, every gate, one checkpoint first", 17, NAVY, 800))
    return svg_wrap(w, h, "".join(body), "Airport checkpoint illustration")


# ---------------------------------------------------------------------------
# 3. Arrivals hall + ops desk (application path)
# ---------------------------------------------------------------------------
def arrivals():
    w, h = 900, 420
    body = []
    body.append(terminal(80, 140, 240, 140, "Arrivals Hall (public)"))
    body.append(terminal(560, 140, 260, 140, "Ops Desk (private back office)"))
    body.append(plane(80, 60, 1.1, NAVY, rotate=-90))
    body.append(taxiway([(120, 90), (150, 150)], width=4))
    body.append(taxiway([(320, 230), (560, 230)], color=GOLD, width=5))
    body.append(label(440, 210, "one hallway only", 11, GOLD, 700))
    body.append(shield(560, 260, 30, GOLD))
    body.append(label(450, 40, "One public counter, one private hallway to the back office", 16, NAVY, 800))
    return svg_wrap(w, h, "".join(body), "Arrivals hall illustration")


# ---------------------------------------------------------------------------
# 4. Concierge walkways (VPC Lattice)
# ---------------------------------------------------------------------------
def concierge():
    w, h = 900, 420
    body = []
    body.append(terminal(80, 200, 180, 120, "Terminal A"))
    body.append(terminal(650, 200, 180, 120, "Terminal C"))
    body.append(terminal(360, 60, 180, 120, "Terminal B"))
    # private walkway (not through main concourse)
    body.append(taxiway([(260, 250), (650, 250)], color=TOWER, width=4, dashed=True))
    body.append(shield(455, 250, 28, TOWER))
    body.append(label(455, 300, "badge check every time (IAM auth)", 11, TOWER, 700))
    body.append(taxiway([(260, 250), (360, 150)], color=TOWER, width=4, dashed=True))
    body.append(label(450, 40, "A private walkway between specific gates -- no full concourse needed", 15, NAVY, 800))
    return svg_wrap(w, h, "".join(body), "Concierge walkway illustration")


# ---------------------------------------------------------------------------
# 5. Security operations center (governance)
# ---------------------------------------------------------------------------
def soc():
    w, h = 420, 420
    body = []
    body.append(f'<rect x="60" y="80" width="300" height="220" rx="10" fill="{WHITE}" stroke="{LINE}" stroke-width="2"/>')
    body.append(label(210, 60, "Security Operations Center", 15, NAVY, 800))
    for i, txt in enumerate(["Cameras (CloudTrail)", "Inspector (Config)", "Threat desk (GuardDuty)", "Auto-response team"]):
        y = 120 + i * 44
        body.append(f'<circle cx="90" cy="{y}" r="6" fill="{RED}"/>')
        body.append(label(110, y+4, txt, 12, NAVY, 600, "start"))
    return svg_wrap(w, h, "".join(body), "Security operations center illustration")


# ---------------------------------------------------------------------------
# 6. Construction crew (CI/CD pipeline)
# ---------------------------------------------------------------------------
def construction():
    w, h = 900, 300
    body = []
    steps = ["Blueprint\n(CodeCommit)", "Build plan\n(Synth)", "Update own\npermits first", "Director\nsign-off", "Break\nground"]
    x0, gap = 60, 168
    for i, s in enumerate(steps):
        x = x0 + i * gap
        body.append(f'<rect x="{x}" y="120" width="140" height="70" rx="10" fill="{WHITE}" stroke="{LINE}" stroke-width="2"/>')
        lines = s.split("\n")
        for j, ln in enumerate(lines):
            body.append(label(x+70, 150 + j*18, ln, 12, NAVY, 700))
        if i < len(steps) - 1:
            body.append(taxiway([(x+140, 155), (x+gap, 155)], width=4))
    body.append(label(450, 50, "The crew updates its own permit process before touching a runway", 16, NAVY, 800))
    return svg_wrap(w, h, "".join(body), "Construction crew pipeline illustration")


# ---------------------------------------------------------------------------
# 7. Alliance network (Cloud WAN)
# ---------------------------------------------------------------------------
def alliance():
    w, h = 500, 420
    body = []
    body.append(tower(140, 140, "This Airport"))
    body.append(tower(360, 140, "Sister Airport"))
    body.append(taxiway([(140, 240), (360, 240)], color=GOLD, width=5))
    body.append(plane(250, 230, 1.2, NAVY, rotate=90))
    body.append(label(250, 60, "New alliance route, existing taxiways untouched", 13, NAVY, 800))
    return svg_wrap(w, h, "".join(body), "Airline alliance network illustration")


# ---------------------------------------------------------------------------
# 8. AI co-pilot (Agentic AI)
# ---------------------------------------------------------------------------
def copilot():
    w, h = 500, 420
    body = []
    body.append(tower(250, 90, "Control Tower"))
    body.append(f'<rect x="190" y="230" width="120" height="90" rx="10" fill="{WHITE}" stroke="{TOWER}" stroke-width="2"/>')
    body.append(label(250, 265, "AI Co-Pilot", 13, NAVY, 800))
    body.append(label(250, 285, "reads everything", 10, NAVY, 500))
    body.append(label(250, 300, "never touches the yoke", 10, NAVY, 500))
    body.append(taxiway([(250, 210), (250, 230)], color=TOWER, width=4, dashed=True))
    body.append(f'<rect x="150" y="350" width="200" height="46" rx="8" fill="{SKY_DARK}"/>')
    body.append(label(250, 378, "drafts a memo -- human signs off", 11, WHITE, 700))
    body.append(taxiway([(250, 320), (250, 350)], color=TOWER, width=4))
    return svg_wrap(w, h, "".join(body), "AI co-pilot illustration")


# ---------------------------------------------------------------------------
# 9. Seismograph (SageMaker anomaly detection)
# ---------------------------------------------------------------------------
def seismograph():
    w, h = 500, 300
    body = []
    body.append(f'<rect x="40" y="80" width="420" height="140" rx="10" fill="{WHITE}" stroke="{LINE}" stroke-width="2"/>')
    pts = [(60, 150)]
    import math
    for i in range(1, 40):
        xx = 60 + i * 10
        yy = 150 + (30 if i in (12, 13, 26) else (math.sin(i/2) * 6))
        pts.append((xx, yy))
    d = "M " + " L ".join(f"{p[0]} {p[1]}" for p in pts)
    body.append(f'<path d="{d}" stroke="{TOWER}" stroke-width="2.5" fill="none"/>')
    body.append(f'<circle cx="180" cy="180" r="6" fill="{RED}"/>')
    body.append(label(180, 165, "anomaly", 10, RED, 700))
    body.append(label(250, 50, "Listening to the runway for anything unusual", 15, NAVY, 800))
    return svg_wrap(w, h, "".join(body), "Seismograph anomaly detection illustration")


# ---------------------------------------------------------------------------
# 10. The badge vault (SecurityStack foundation)
# ---------------------------------------------------------------------------
def vault():
    w, h = 500, 380
    body = []
    body.append(f'<rect x="120" y="80" width="260" height="220" rx="14" fill="{WHITE}" stroke="{LINE}" stroke-width="2"/>')
    body.append(f'<circle cx="250" cy="190" r="46" fill="none" stroke="{TOWER}" stroke-width="8"/>')
    body.append(f'<circle cx="250" cy="190" r="10" fill="{TOWER}"/>')
    body.append(f'<rect x="235" y="190" width="30" height="46" fill="{TOWER}"/>')
    for i, (dx, txt) in enumerate([(-70, "5 separate keys"), (70, "one ceiling badge")]):
        body.append(label(250 + dx, 340, txt, 11, NAVY, 600))
    body.append(label(250, 50, "Five separate keys, one badge policy every door respects", 15, NAVY, 800))
    return svg_wrap(w, h, "".join(body), "Security vault illustration")


# ---------------------------------------------------------------------------
# 11. The control room wall of screens (Observability)
# ---------------------------------------------------------------------------
def control_room():
    w, h = 500, 340
    body = []
    for r in range(2):
        for c in range(3):
            x = 60 + c * 130
            y = 80 + r * 90
            body.append(f'<rect x="{x}" y="{y}" width="112" height="72" rx="8" fill="{SKY_DARK}" stroke="{TOWER}" stroke-width="2"/>')
            body.append(f'<path d="M{x+16} {y+50} L{x+40} {y+24} L{x+58} {y+40} L{x+96} {y+16}" stroke="{GOLD}" stroke-width="3" fill="none"/>')
    body.append(label(250, 50, "One wall of screens, fed by every terminal at once", 15, NAVY, 800))
    return svg_wrap(w, h, "".join(body), "Control room illustration")


# ---------------------------------------------------------------------------
# 12. The visitor ledger (Blog analytics)
# ---------------------------------------------------------------------------
def ledger():
    w, h = 500, 340
    body = []
    body.append(f'<rect x="80" y="60" width="340" height="240" rx="10" fill="{WHITE}" stroke="{LINE}" stroke-width="2"/>')
    for i in range(5):
        y = 100 + i * 38
        body.append(f'<line x1="105" y1="{y}" x2="395" y2="{y}" stroke="{LINE}" stroke-width="1" opacity="0.4"/>')
        body.append(f'<circle cx="118" cy="{y-10}" r="5" fill="{GOLD if i % 2 == 0 else TOWER}"/>')
    body.append(label(250, 40, "A visitor ledger that forgets the details, keeps the count", 14, NAVY, 800))
    return svg_wrap(w, h, "".join(body), "Visitor ledger illustration")


# ---------------------------------------------------------------------------
# 13. The blueprint on the wall (Kafka, planned)
# ---------------------------------------------------------------------------
def blueprint():
    w, h = 500, 340
    body = []
    body.append(f'<rect x="70" y="60" width="360" height="220" rx="10" fill="{SKY_DARK}" stroke="{GOLD}" stroke-width="2" stroke-dasharray="10 6"/>')
    body.append(f'<rect x="100" y="90" width="120" height="70" fill="none" stroke="{GOLD}" stroke-width="1.5"/>')
    body.append(f'<rect x="250" y="90" width="150" height="70" fill="none" stroke="{GOLD}" stroke-width="1.5"/>')
    body.append(f'<rect x="100" y="180" width="300" height="60" fill="none" stroke="{GOLD}" stroke-width="1.5"/>')
    body.append(label(250, 40, "A wing on the blueprint, not built yet, planned on purpose", 14, WHITE, 800))
    return svg_wrap(w, h, "".join(body), "Blueprint illustration")


ILLUSTRATIONS = {
    "map-overview.svg": master_map,
    "checkpoint.svg": checkpoint,
    "arrivals.svg": arrivals,
    "concierge.svg": concierge,
    "soc.svg": soc,
    "construction.svg": construction,
    "alliance.svg": alliance,
    "copilot.svg": copilot,
    "vault.svg": vault,
    "control-room.svg": control_room,
    "ledger.svg": ledger,
    "blueprint.svg": blueprint,
    "seismograph.svg": seismograph,
}

if __name__ == "__main__":
    OUT_DIR.mkdir(exist_ok=True)
    for filename, fn in ILLUSTRATIONS.items():
        path = OUT_DIR / filename
        path.write_text(fn())
        print(f"wrote {path} ({path.stat().st_size} bytes)")
