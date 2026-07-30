#!/usr/bin/env python3
"""generate_nft.py -- renders zone-definitions.yaml + policy.yaml into the
nftables zone-based policy engine ("the PL"), per
docs/inspection-architecture-reference.md Section 5.2.

DEPENDENCY NOTE: stdlib only, no PyYAML. zone-definitions.yaml and
policy.yaml are both simple, fully-controlled-by-this-project YAML subsets
(block mappings, block sequences, one-line flow maps like
`{proto: tcp, port: 443}`, no anchors/multi-doc/tabs) so this file ships a
small purpose-built parser (_load_yaml, below) instead of depending on
PyYAML. This keeps the script runnable unmodified both on a macOS dev
laptop (for `--skip-nft-check` dry runs while authoring policy) and on the
bare Ubuntu 24.04 golden AMI (Section 5.4) with no extra `pip install` in
the boot/pl-sync path. If you would rather depend on PyYAML instead: replace
_load_yaml() with `yaml.safe_load(path.read_text())` and add `python3-yaml`
to the Packer-built golden AMI package list -- do NOT lazy-`pip install` it
from pl-sync.service at apply time, that would violate the "fast
deterministic ASG scale-out with zero external repo dependency at launch
time" principle Section 5.4 sets for the golden-AMI layer.

Pipeline (Section 5.2's numbered "generate_nft.py responsibilities" list):
  1. validate()                    -- reject unknown zones, duplicate rule
                                       names, shadowed rules within a chain.
  2. render_source_zone_maps()     -- cidr_zone_v4 / cidr_zone_v6.   FIX #1
  3. render_dest_zone_sets()       -- zone_trust_v4 / zone_dmz_v4 /
                                       zone_untrust_v4 (+ v6).       FIX #2
                                       Deliberately a SEPARATE function/step
                                       from #2 -- see its docstring.
  4. render_zone_pair_chains() /
     render_zone_policy_chain()   -- policy.yaml -> per-zone-pair chains.
  5. assemble() + write file      -- one combined .nft file.
  6. run_nft_check()              -- `nft -c -f <file>` syntax-only gate.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
DEFAULT_ZONE_DEFS = THIS_DIR / "zone-definitions.yaml"
DEFAULT_POLICY = THIS_DIR / "policy.yaml"
DEFAULT_OUT = THIS_DIR / "pl.nft"

VALID_PROTOS = {"tcp", "udp"}
VALID_ACTIONS = {"allow", "deny"}


# ---------------------------------------------------------------------------
# Minimal YAML-subset parser (see module docstring for scope/rationale).
# ---------------------------------------------------------------------------

def _strip_comment(raw_line: str) -> str:
    # Safe for this project's YAML: CIDRs, ports, zone/rule names and mark
    # hex strings never contain a literal '#', so a naive split is exact.
    return raw_line.split("#", 1)[0]


def _tokenize(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if "\t" in raw:
            raise ValueError("YAML subset parser requires spaces, found a tab")
        before_hash = _strip_comment(raw)
        if not before_hash.strip():
            continue
        indent = len(before_hash) - len(before_hash.lstrip(" "))
        lines.append((indent, before_hash.strip()))
    return lines


_SCALAR_INT_RE = re.compile(r"^-?\d+$")
_KEY_START_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:(\s|$)")


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~", ""):
        return None
    if _SCALAR_INT_RE.match(s):
        return int(s)
    return s


def _parse_flow_map(s: str) -> dict[str, Any]:
    s = s.strip()
    assert s.startswith("{") and s.endswith("}"), f"not a flow map: {s!r}"
    inner = s[1:-1]
    result: dict[str, Any] = {}
    if inner.strip():
        for part in inner.split(","):
            if not part.strip():
                continue
            k, _, v = part.partition(":")
            result[k.strip()] = _parse_scalar(v.strip())
    return result


def _value_after_colon(rest: str, lines: list[tuple[int, str]], i: int,
                        indent: int) -> tuple[Any, int]:
    """Parse the value portion of a `key: <rest>` line. `i` must already
    point past the key:value line itself. `indent` is that line's indent."""
    if rest == "":
        if i < len(lines) and lines[i][0] > indent:
            return _parse_node(lines, i, lines[i][0])
        return None, i
    if rest == "[]":
        return [], i
    if rest.startswith("{"):
        return _parse_flow_map(rest), i
    return _parse_scalar(rest), i


def _parse_mapping_body(lines: list[tuple[int, str]], i: int, indent: int,
                         mapping: dict[str, Any] | None = None
                         ) -> tuple[dict[str, Any], int]:
    if mapping is None:
        mapping = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        line_indent, content = lines[i]
        if ":" not in content:
            raise ValueError(f"malformed mapping line (no ':'): {content!r}")
        key, _, rest = content.partition(":")
        key = key.strip().strip("'\"")
        rest = rest.strip()
        i += 1
        val, i = _value_after_colon(rest, lines, i, line_indent)
        mapping[key] = val
    return mapping, i


def _parse_sequence(lines: list[tuple[int, str]], i: int, indent: int
                     ) -> tuple[list[Any], int]:
    items: list[Any] = []
    content_indent = indent + 2
    while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
        item_indent, item_content = lines[i]
        rest = item_content[2:]
        if rest == "":
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                val, i = _parse_node(lines, i, lines[i][0])
            else:
                val = None
        elif rest.startswith("{"):
            val = _parse_flow_map(rest)
            i += 1
        elif _KEY_START_RE.match(rest):
            # Block-sequence-of-mappings shorthand: "- key: value" starts an
            # inline mapping whose remaining keys are on following lines
            # indented to content_indent (this is how policy.yaml's rule
            # rows -- "- name: ...\n  src_zone: ...\n  ..." -- parse).
            key, _, v = rest.partition(":")
            key = key.strip()
            v = v.strip()
            i += 1
            subval, i = _value_after_colon(v, lines, i, content_indent - 1)
            seed = {key: subval}
            val, i = _parse_mapping_body(lines, i, content_indent, seed)
        else:
            val = _parse_scalar(rest)
            i += 1
        items.append(val)
    return items, i


def _parse_node(lines: list[tuple[int, str]], i: int, indent: int
                 ) -> tuple[Any, int]:
    if i >= len(lines) or lines[i][0] != indent:
        return None, i
    if lines[i][1].startswith("- "):
        return _parse_sequence(lines, i, indent)
    return _parse_mapping_body(lines, i, indent)


def _load_yaml(path: Path) -> Any:
    lines = _tokenize(path.read_text())
    if not lines:
        return None
    value, i = _parse_node(lines, 0, lines[0][0])
    if i != len(lines):
        raise ValueError(f"{path}: trailing unparsed content at line index {i}")
    return value


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

class Zone:
    __slots__ = ("name", "mark", "cidrs", "dest_cidrs")

    def __init__(self, name: str, mark: int, cidrs: list[str], dest_cidrs: list[str]):
        self.name = name
        self.mark = mark
        self.cidrs = cidrs
        self.dest_cidrs = dest_cidrs


def load_zones(path: Path) -> dict[str, Zone]:
    doc = _load_yaml(path) or {}
    raw_zones = doc.get("zones") or {}
    zones: dict[str, Zone] = {}
    for name, body in raw_zones.items():
        body = body or {}
        mark_raw = body.get("mark")
        if mark_raw is None:
            raise ValueError(f"zone {name!r} is missing 'mark'")
        mark = int(str(mark_raw), 16) if str(mark_raw).lower().startswith("0x") else int(mark_raw)
        cidrs = list(body.get("cidrs") or [])
        dest_cidrs = list(body.get("dest_cidrs") or [])
        zones[name] = Zone(name, mark, cidrs, dest_cidrs)
    return zones


def load_rules(path: Path) -> list[dict[str, Any]]:
    doc = _load_yaml(path)
    return list(doc or [])


# ---------------------------------------------------------------------------
# Step 1: validation
# ---------------------------------------------------------------------------

def _assert_no_overlapping_supersets(elements: list[tuple[str, str]], label: str) -> None:
    """FIX #1 enforcement, executable rather than just a convention: within
    ONE interval map/set's element list, no CIDR may be a strict superset of
    another (this is exactly the '0.0.0.0/0 alongside a more-specific
    subnet' shape nftables interval maps reject). Catches the mistake even
    if someone edits zone-definitions.yaml later and reintroduces a
    catch-all."""
    nets = [ipaddress.ip_network(cidr, strict=False) for cidr, _zone in elements]
    for i, a in enumerate(nets):
        for j, b in enumerate(nets):
            if i == j:
                continue
            if a.version == b.version and a != b and b.subnet_of(a):
                raise ValueError(
                    f"{label}: {elements[i][0]} ({elements[i][1]}) is a superset of "
                    f"{elements[j][0]} ({elements[j][1]}) -- FIX #1 forbids a "
                    f"catch-all/superset coexisting with more-specific elements "
                    f"in the same interval map."
                )


def validate(zones: dict[str, Zone], rules: list[dict[str, Any]]) -> None:
    if not zones:
        raise ValueError("zone-definitions.yaml defines no zones")

    # FIX #1, executable check, across BOTH IPv4 and IPv6 source-map elements.
    for family, attr in ((4, "cidrs"), (6, "cidrs")):
        elements = []
        for z in zones.values():
            for cidr in getattr(z, attr):
                net = ipaddress.ip_network(cidr, strict=False)
                if net.version == family:
                    elements.append((cidr, z.name))
        _assert_no_overlapping_supersets(elements, f"cidr_zone_v{family}")

    seen_names: set[str] = set()
    seen_rule_keys: set[tuple[str, str, str, int]] = set()
    for rule in rules:
        name = rule.get("name")
        if not name:
            raise ValueError(f"rule missing 'name': {rule!r}")
        if name in seen_names:
            raise ValueError(f"duplicate rule name: {name!r}")
        seen_names.add(name)

        src = rule.get("src_zone")
        dst = rule.get("dst_zone")
        if src not in zones:
            raise ValueError(f"rule {name!r}: unknown src_zone {src!r}")
        if dst not in zones:
            raise ValueError(f"rule {name!r}: unknown dst_zone {dst!r}")

        action = rule.get("action")
        if action not in VALID_ACTIONS:
            raise ValueError(f"rule {name!r}: action must be one of {VALID_ACTIONS}, got {action!r}")

        services = rule.get("service") or []
        if not services:
            raise ValueError(f"rule {name!r}: 'service' must have at least one entry")
        for svc in services:
            proto = svc.get("proto")
            port = svc.get("port")
            if proto not in VALID_PROTOS:
                raise ValueError(f"rule {name!r}: unknown proto {proto!r} (want one of {VALID_PROTOS})")
            if not isinstance(port, int) or not (1 <= port <= 65535):
                raise ValueError(f"rule {name!r}: invalid port {port!r}")

            # Shadow-rule lint: identical (src, dst, proto, port) tuple
            # defined more than once means the later rule can never fire
            # (the earlier chain entry always matches first).
            key = (src, dst, proto, port)
            if key in seen_rule_keys:
                raise ValueError(
                    f"rule {name!r}: shadowed rule -- ({src}->{dst}, {proto}/{port}) "
                    f"is already matched by an earlier rule in this zone pair"
                )
            seen_rule_keys.add(key)


# ---------------------------------------------------------------------------
# Step 2: cidr_zone_v4 / cidr_zone_v6 SOURCE-zone maps.               FIX #1
# ---------------------------------------------------------------------------

def render_source_zone_maps(zones: dict[str, Zone]) -> str:
    """Renders the interval maps zone-classify uses to stamp meta mark from
    ip/ip6 saddr. FIX #1: only consumes each zone's `cidrs` list -- untrust's
    `cidrs` is empty by construction (zone-definitions.yaml), so NO catch-all
    0.0.0.0/0 / ::/0 element is ever emitted here, and untrust is reached only
    via zone-classify's fallback rule (render_zone_classify_chain, below)."""
    lines = ["    # ---- Zone membership (source of truth: zone-definitions.yaml) ----",
             "    # FIX #1: no catch-all 0.0.0.0/0 (or ::/0) entry -- nftables interval",
             "    # maps require non-overlapping ranges. Unmatched traffic is handled",
             "    # by the fallback rule in zone-classify below, not a map entry."]
    for family, typ in ((4, "ipv4_addr"), (6, "ipv6_addr")):
        elements = []
        for z in zones.values():
            for cidr in z.cidrs:
                if ipaddress.ip_network(cidr, strict=False).version == family:
                    elements.append((f"{cidr} : {_fmt_mark(z.mark)}", f"zone-{z.name}"))
        lines.append(f"    map cidr_zone_v{family} {{")
        lines.append(f"        type {typ} : mark ; flags interval ;")
        if elements:
            lines.append("        elements = {")
            for idx, (entry, comment) in enumerate(elements):
                # NOTE: the separating comma MUST come before the trailing
                # `# comment` -- nft's `#` comment runs to end-of-line, so a
                # comma placed after it would be invisible to the parser and
                # silently break element-list separation. (Caught during
                # local testing -- see this file's summary notes.)
                sep = "," if idx < len(elements) - 1 else ""
                lines.append(f"            {entry}{sep}  # {comment}")
            lines.append("        }")
        lines.append("    }")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 3: DESTINATION-zone sets (zone_trust_v4/zone_dmz_v4/zone_untrust_v4,
# + v6 counterparts).                                                 FIX #2
# ---------------------------------------------------------------------------

def render_dest_zone_sets(zones: dict[str, Zone]) -> str:
    """FIX #2: this is a DELIBERATELY SEPARATE render step from
    render_source_zone_maps() above -- the reference doc calls this out
    explicitly (Section 5.2: 'these must be generated ... alongside
    cidr_zone_v4/cidr_zone_v6; do not leave them referenced-but-undefined').
    zone-policy's dispatch rules do `ip daddr @zone_<name>_v4` for zones with
    a real, enumerable CIDR (trust, dmz), so those zones need their own named
    SET (not a shared map) here.

    A zone whose `cidrs` is empty by FIX #1 design (untrust) still gets a set
    here too (from `dest_cidrs` -- e.g. onprem's 10.100.0.0/16), but note it
    is NOT referenced by zone-policy's default dispatch for that zone as of
    the bugfix in render_zone_policy_chain() above -- untrust dispatches on
    source-zone alone precisely because it has no enumerable set of "the
    entire internet" to match against. This set is retained for a *narrower*,
    still-useful purpose: it names onprem's actual CIDR specifically, in case
    a future policy.yaml rule wants to distinguish "destined for onprem" from
    "destined for the untrust fallback bucket generally" -- it's simply not
    wired into the untrust fallback path itself anymore."""
    lines = ["    # ---- Destination-zone sets, used by zone-policy's dispatch ----",
             "    # FIX #2: separate generation step from the source maps above."]
    for family, typ in ((4, "ipv4_addr"), (6, "ipv6_addr")):
        for z in zones.values():
            source_list = z.cidrs if z.cidrs else z.dest_cidrs
            elements = [c for c in source_list if ipaddress.ip_network(c, strict=False).version == family]
            lines.append(f"    set zone_{z.name}_v{family} {{")
            lines.append(f"        type {typ} ; flags interval ;")
            if elements:
                lines.append("        elements = { " + ", ".join(elements) + " }")
            lines.append("    }")
    return "\n".join(lines)


def _fmt_mark(mark: int) -> str:
    return f"0x{mark:08x}"


# ---------------------------------------------------------------------------
# zone-classify chain (consumes the source maps from step 2)
# ---------------------------------------------------------------------------

def render_zone_classify_chain(zones: dict[str, Zone]) -> str:
    untrust = next((z for z in zones.values() if not z.cidrs), None)
    if untrust is None:
        raise ValueError("no zone with an empty 'cidrs' list found to use as the "
                          "untrust fallback zone (see FIX #1)")
    return "\n".join([
        "    chain zone-classify {",
        "        type filter hook forward priority -10; policy accept;",
        "        ct state new meta mark set ip  saddr map @cidr_zone_v4",
        "        ct state new meta mark set ip6 saddr map @cidr_zone_v6",
        "        # FIX #1 fallback: anything that didn't match a defined zone CIDR is untrust.",
        f"        ct state new meta mark 0x00000000 meta mark set {_fmt_mark(untrust.mark)}",
        "        ct state new ct mark set meta mark",
        "    }",
    ])


# ---------------------------------------------------------------------------
# Step 4: policy.yaml -> zone-policy dispatch + per-zone-pair chains
# ---------------------------------------------------------------------------

def _chain_name(src: str, dst: str) -> str:
    return f"zone-{src}-to-{dst}"


def _ordered_pairs(rules: list[dict[str, Any]]) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for rule in rules:
        pair = (rule["src_zone"], rule["dst_zone"])
        if pair not in seen:
            seen.append(pair)
    return seen


def render_zone_policy_chain(zones: dict[str, Zone], rules: list[dict[str, Any]]) -> str:
    """BUGFIX (found by the adversarial verify pass, not in the original
    design): dispatching EVERY pair -- including *-to-untrust -- via
    `ip daddr @zone_<dst>_v4` silently broke every trust-untrust-* /
    dmz-untrust-* rule for real internet destinations. `zone_untrust_v4` is
    only populated from `dest_cidrs` (onprem's 10.100.0.0/16 -- see
    zone-definitions.yaml), which does NOT and cannot cover arbitrary
    internet addresses (e.g. 8.8.8.8) -- a genuine CIDR catch-all there would
    just reintroduce FIX #1's overlapping-interval problem on the
    destination side instead of the source side. So a trust-zone packet to
    8.8.8.8:443 matched no `ip daddr @zone_*_v4` clause at all and fell
    straight through to the final `PL-DEFAULT-DENY` -- silently defeating
    every rule meant to exercise north-south egress (including the one
    Suricata NFQUEUE rule meant to prove IPS is actually in the path).

    Fix: zones with an empty `cidrs` list (untrust -- see FIX #1) are, by
    definition, "everything not explicitly assigned to a real zone" -- so
    pairs whose DESTINATION is that zone dispatch on ct mark (source zone)
    ALONE, no `ip daddr` condition, mirroring exactly how zone-classify
    already handles untrust on the *source* side (a rule-based fallback, not
    a set element). These rules are emitted LAST, after every specific
    (real-destination-CIDR) pair, so a packet destined for an actual known
    zone (trust/dmz) still matches its specific, more-restrictive rule
    first -- each per-pair chain unconditionally accept/queue/drops before
    control could ever fall back to zone-policy and reach the untrust
    fallback rule, so there is no double-dispatch risk either way."""
    pairs = _ordered_pairs(rules)
    untrust = next((z for z in zones.values() if not z.cidrs), None)
    if untrust is None:
        raise ValueError("no zone with an empty 'cidrs' list found to use as the "
                          "untrust fallback zone (see FIX #1)")

    specific_pairs = [(s, d) for s, d in pairs if d != untrust.name]
    fallback_pairs = [(s, d) for s, d in pairs if d == untrust.name]

    lines = [
        "    chain zone-policy {",
        "        type filter hook forward priority 0; policy drop;",
        "        ct state established,related accept",
        "        ct state invalid drop",
        "",
    ]
    for src, dst in specific_pairs:
        src_mark = _fmt_mark(zones[src].mark)
        lines.append(f"        ct mark {src_mark} ip daddr @zone_{dst}_v4 jump {_chain_name(src, dst)}")
    if fallback_pairs:
        lines.append("")
        lines.append(f"        # {untrust.name}: no enumerable destination CIDR (it's the fallback")
        lines.append("        # zone for everything not explicitly trust/dmz -- see FIX #1).")
        lines.append("        # Dispatch on source zone alone; ordered last so it only catches")
        lines.append("        # what no more specific pair above already claimed.")
        for src, dst in fallback_pairs:
            src_mark = _fmt_mark(zones[src].mark)
            lines.append(f"        ct mark {src_mark} jump {_chain_name(src, dst)}")
    lines.append("")
    lines.append('        log prefix "PL-DEFAULT-DENY: " drop')
    lines.append("    }")
    return "\n".join(lines)


def render_zone_pair_chains(rules: list[dict[str, Any]]) -> str:
    pairs = _ordered_pairs(rules)
    blocks: list[str] = []
    for src, dst in pairs:
        chain = _chain_name(src, dst)
        body = [f"    chain {chain} {{"]
        for rule in rules:
            if (rule["src_zone"], rule["dst_zone"]) != (src, dst):
                continue
            body.extend(_render_rule(rule))
        body.append(f'        log prefix "PL-DENY rule={src}-{dst}-default: " drop')
        body.append("    }")
        blocks.append("\n".join(body))
    return "\n\n".join(blocks)


def _render_rule(rule: dict[str, Any]) -> list[str]:
    name = rule["name"]
    action = rule["action"]
    log = bool(rule.get("log", False))
    inspect = bool(rule.get("inspect", False))
    bypass = bool(rule.get("bypass", True))

    out: list[str] = []
    for svc in rule["service"]:
        proto, port = svc["proto"], svc["port"]
        match = f"{proto} dport {port}"
        log_clause = f'log prefix "PL-{"ALLOW" if action == "allow" else "DENY"} rule={name}: " ' if log else ""

        if action == "deny":
            out.append(f"        {match} {log_clause}drop")
            continue

        # action == allow
        if inspect:
            # `queue` is an nft-terminal statement; it cannot follow `accept`
            # in the same rule, so it REPLACES accept rather than being
            # appended after it (see this file's -- and policy.yaml's --
            # header comment for why, and the reference doc's own worked
            # `chain zone-trust-to-untrust { ... }` example, which does the
            # same thing).
            options = "fanout,bypass" if bypass else "fanout"
            out.append(f"        {match} {log_clause}queue num 0-3 options {options}")
        else:
            out.append(f"        {match} {log_clause}accept")
    return out


# ---------------------------------------------------------------------------
# Step 5: assemble the combined .nft file
# ---------------------------------------------------------------------------

def assemble(zones: dict[str, Zone], rules: list[dict[str, Any]]) -> str:
    parts = [
        "#!/usr/sbin/nft -f",
        "# Generated by generate_nft.py -- DO NOT HAND-EDIT.",
        "# Source of truth: zone-definitions.yaml + policy.yaml (see",
        "# docs/inspection-architecture-reference.md Section 5.2).",
        "",
        "table inet fw {",
        "",
        render_source_zone_maps(zones),
        "",
        render_zone_classify_chain(zones),
        "",
        render_dest_zone_sets(zones),
        "",
        render_zone_policy_chain(zones, rules),
        "",
        render_zone_pair_chains(rules),
        "",
        "}",
        "",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Step 6: `nft -c -f` syntax-only validation gate
# ---------------------------------------------------------------------------

def run_nft_check(nft_path: Path) -> int:
    """Shells out to `nft -c -f <file>` for a syntax-only check (no apply --
    this mirrors the CI gate in Section 5.5 step 2, and pl-sync.service's own
    validate-before-apply step on the instance).

    NOTE: `nft` is a Linux-only binary (netfilter/nftables kernel subsystem)
    and is NOT available on macOS -- this check can only actually execute on
    the Linux EC2 target (or an equivalent Linux CI runner), never on a
    macOS dev machine. When `nft` isn't found on PATH, this function prints a
    warning and returns 0 (does not fail the render) so the script stays
    usable for local policy authoring on macOS.

    What a clean run looks like on the real target:
        $ nft -c -f pl.nft
        $
    i.e. `nft -c` prints NOTHING and exits 0 on success -- silence is the
    success signal for `-c` (syntax-check) mode. On failure it prints a
    pointer at the offending token, e.g.:
        pl.nft:12:9-19: Error: conflicting protocols specified
        map cidr_zone_v4 { type ipv4_addr : mark ; flags interval ;
                ^^^^^^^^^^^
    and exits non-zero, which this function propagates as its return code.
    """
    nft_bin = shutil.which("nft")
    if nft_bin is None:
        print(
            "WARNING: `nft` binary not found on PATH -- skipping `nft -c -f` "
            "syntax check. This is expected on macOS; this gate must be run "
            "on the Linux EC2 target (or Linux CI) before the PL ships. "
            "See run_nft_check()'s docstring for what a clean run looks like.",
            file=sys.stderr,
        )
        return 0

    proc = subprocess.run([nft_bin, "-c", "-f", str(nft_path)], capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"`nft -c -f {nft_path}` FAILED (exit {proc.returncode}):", file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
    else:
        print(f"`nft -c -f {nft_path}` OK (syntax-valid).")
    return proc.returncode


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # NOTE: flag names (--policy / --zone-definitions / --output) match the
    # contract pl-sync.sh (a sibling task's deliverable, Section 5.5) already
    # assumes for this script -- see pl-sync.sh's own "confirm these flag
    # names against the sibling task's real argparse signature" comment.
    # --zone-defs/--out are kept as shorter aliases for local CLI use.
    parser.add_argument("--zone-definitions", "--zone-defs", dest="zone_defs",
                         type=Path, default=DEFAULT_ZONE_DEFS)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", "--out", dest="out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--skip-nft-check", action="store_true",
                         help="Skip the `nft -c -f` step entirely (e.g. for quick local iteration, "
                              "or from pl-sync.sh, which already runs its own `nft -c -f` gate "
                              "as a separate step -- running it here too is harmless but redundant).")
    args = parser.parse_args(argv)

    try:
        zones = load_zones(args.zone_defs)
        rules = load_rules(args.policy)
        validate(zones, rules)
    except ValueError as exc:
        print(f"VALIDATION ERROR: {exc}", file=sys.stderr)
        return 1

    rendered = assemble(zones, rules)
    args.out.write_text(rendered)
    print(f"Wrote {args.out} ({len(rendered.splitlines())} lines).")

    if args.skip_nft_check:
        return 0
    return run_nft_check(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
