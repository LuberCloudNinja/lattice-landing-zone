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
    w, h = 1200, 640
    body = []
    body.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="{SKY}"/>')
    # ground band
    body.append(f'<rect x="0" y="{h-40}" width="{w}" height="40" fill="{GROUND}" opacity="0.15"/>')

    # inspection checkpoint terminal (center)
    body.append(terminal(500, 260, 220, 130, "Inspection Checkpoint"))
    body.append(shield(610, 320, 46))

    # app terminal (left)
    body.append(terminal(120, 260, 200, 130, "App Terminal"))
    # provider terminal (right)
    body.append(terminal(880, 260, 200, 130, "Provider Terminal"))

    # control tower (taxiway hub) above checkpoint
    body.append(tower(610, 70, "Control Tower (Transit Gateway)"))

    # taxiways connecting terminals through the checkpoint hub
    body.append(taxiway([(320, 340), (500, 340)], width=6))
    body.append(taxiway([(720, 340), (880, 340)], width=6))
    body.append(taxiway([(610, 260), (610, 175)], width=6))

    # regional airstrip (on-prem) bottom-left, connected by dashed puddle-jumper route
    body.append(terminal(60, 470, 160, 90, "Regional Airstrip (on-prem)"))
    body.append(taxiway([(140, 470), (300, 400), (500, 360)], color=TOWER, width=4, dashed=True))
    body.append(label(230, 430, "chartered VPN flight", 11, TOWER, 600, "middle"))

    # arrivals hall (public) below app terminal
    body.append(terminal(80, 480, 200, 100, "Arrivals Hall (public site)"))
    body.append(taxiway([(180, 480), (180, 400)], color=GOLD, width=4))

    # planes in flight (internet traffic)
    body.append(plane(610, 40, 1.4, NAVY))
    body.append(plane(950, 480, 1.1, NAVY, rotate=140))
    body.append(plane(200, 40, 1.1, NAVY, rotate=-30))

    body.append(label(w/2, 30, "The Airport (your AWS account)", 20, NAVY, 800, "middle"))
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
