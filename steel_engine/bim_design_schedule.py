"""The design schedule the HR report prints, as data for the drawings and the IFC.

One rule decides the governing D/C of a member or connection record and one rule prints it. The
report tables, the handoff written for openBIM and the report read-back all call this module, so a
value on a drawing schedule or in an IFC property is the value in the report, character for character.

The module reads calculation records only. It runs no analysis and derives no capacity.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import math
import re

VERSION = 1

DETAIL_LIMIT_RE = re.compile(
    r"plate length within the beam's flat web depth|rotational ductility|plate within the coped web|"
    r"within the (?:beam's )?flat web|\bL_?p\s*(?:/|<=|&le;|≤)\s*T\b|bracing spacing (?:ratio|limit)|"
    r"dimensional limit|detailing limit", re.I)


def dc_num(x):
    """A demand/capacity ratio: a number, or text that starts with one. A status such as 'OK' is not a ratio."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        m = re.match(r"\s*(-?\d*\.?\d+)", x)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def fixed(v, nd):
    """'%.{nd}f' with round-half-up (0.125 -> '0.13')."""
    try:
        q = Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-nd) if nd > 0 else Decimal(1), rounding=ROUND_HALF_UP)
        return format(q, "f")
    except Exception:
        return "%.*f" % (nd, v)


def dc_text(v):
    """A D/C as printed: three decimals with trailing zeros dropped, never fewer than two ('0.777', '0.50')."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    s = fixed(float(v), 3).rstrip("0")
    return fixed(float(v), 2) if len(s.split(".")[1]) < 2 else s


def is_detailing_limit(check):
    """A pass/fail dimensional or detailing limit: printed as a limit, never as the governing strength D/C."""
    return isinstance(check, dict) and (bool(check.get("detailing_limit")) or
                                        bool(DETAIL_LIMIT_RE.search(str(check.get("limit_state", "")))))


def governing_dc(record):
    """(governing D/C, governing check or None, detailing limits present) of a member or connection record.

    The headline is the worst strength check. A recorded headline that only equals a detailing-limit
    ratio is set aside for the worst strength check.
    """
    checks = [k for k in (record.get("checks") or []) if isinstance(k, dict)]
    top = dc_num(record.get("DC"))
    strength = [(dc_num(k.get("DC")), k) for k in checks if not is_detailing_limit(k)]
    strength = [t for t in strength if t[0] is not None]
    limits = [dc_num(k.get("DC")) for k in checks if is_detailing_limit(k)]
    limits = [x for x in limits if x is not None]
    best = max(strength, key=lambda t: t[0]) if strength else (None, None)
    if best[0] is None:
        return (top if top is not None else (max(limits) if limits else None)), None, bool(limits)
    if top is None or top <= best[0] + 1e-9:
        return best[0], best[1], bool(limits)
    if limits and any(abs(top - x) <= 1e-3 for x in limits) and max(limits) <= 1.0 + 1e-9:
        return best[0], best[1], True
    return top, None, bool(limits)


def anchor(prefix, identity):
    """The HTML id of a record's calculation block in the report ('m-...' member, 'c-...' connection)."""
    return prefix + "-" + re.sub(r"[^A-Za-z0-9_-]", "_", str(identity))


def _value_text(value, unit):
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, float):
        text = format(value, ".10g")
    else:
        text = str(value)
    return text + (" " + unit if unit in ("in", "ksi", "kip", "degree") else "")


def selection_lines(rows):
    """Typed connection selections as printed lines, one per physical role and scope, in source order.

    ``rows`` are ``bim_selection`` entries or handoff fields: role, field, value, unit, optional applicability.
    'bolt: count 5; diameter_in 0.75 in; material ASTM F3125 Gr A325-N'
    """
    groups = {}
    for row in rows:
        scope = row.get("applicability") or {}
        label = "; ".join("%s %s" % (key, ",".join(map(str, scope[key])) if isinstance(scope[key], list) else scope[key])
                          for key in sorted(scope))
        key = (row["role"], label)
        groups.setdefault(key, []).append("%s %s" % (row["field"], _value_text(row["value"], row.get("unit"))))
    return ["%s%s: %s" % (role, " [" + label + "]" if label else "", "; ".join(items)) for (role, label), items in groups.items()]


def _row(table, index, record):
    dc, check, limits = governing_dc(record)
    limit_state = check.get("limit_state") if isinstance(check, dict) and check.get("limit_state") else record.get("limit_state")
    identity = str(record.get("id", "@" + str(index)))
    return {"record_id": table + "/" + identity, "id": identity, "dc": dc, "dc_text": dc_text(dc),
            "limit_state": str(limit_state) if limit_state else None, "detailing_limits": limits,
            "waived": bool(record.get("waived"))}


def design_schedule(calc):
    """Every designed member and connection record as the report prints it."""
    members, connections = [], []
    for index, record in enumerate(calc.get("members") or []):
        if not isinstance(record, dict):
            continue
        inputs = record.get("inputs") or {}
        if not inputs.get("section"):
            continue
        row = _row("members", index, record)
        row.update(section=str(inputs["section"]), kind=inputs.get("kind"), role=inputs.get("role"),
                   governing_combination=inputs.get("governing_combo") or None, anchor=anchor("m", row["id"]))
        members.append(row)
    rows = calc.get("connections")
    for index, record in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(record, dict):
            continue
        row = _row("connections", index, record)
        typed = record.get("bim_selection")
        row.update(type=str(record.get("type") or ""), anchor=anchor("c", row["id"]),
                   selection=selection_lines(typed) if isinstance(typed, list) and typed and all(
                       isinstance(v, dict) and all(k in v for k in ("role", "field", "value")) for v in typed) else [])
        connections.append(row)
    return {"version": VERSION, "members": members, "connections": connections}


def record_tables(design_records):
    """The member and connection tables of the calculation package, rebuilt from exported design records."""
    tables = {"members": {}, "connections": {}}
    for record in design_records:
        if record.get("table") in tables:
            tables[record["table"]][int(record["source_pointer"].rsplit("/", 1)[1])] = record["data"]
    return {name: [rows.get(i) for i in range(max(rows) + 1)] if rows else [] for name, rows in tables.items()}


def schedule_findings(schedule, design_records):
    """Differences between a stored schedule and the design records it is said to print."""
    if not isinstance(schedule, dict) or schedule.get("version") != VERSION:
        return ["The design schedule is missing or has another version"]
    return [] if schedule == design_schedule(record_tables(design_records)) else [
        "The design schedule differs from the calculation records"]
