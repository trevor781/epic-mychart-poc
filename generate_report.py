#!/usr/bin/env python3
"""Generate a self-contained HTML report from an extract_mychart.py data dir.

Usage:
    python3 generate_report.py [data/<timestamp>]

Defaults to the most recent extraction under data/. Writes report.html into
the same folder (kept out of git — it contains PHI).

Every row that has more underneath shows a chevron and expands. Any numeric
measurement with 2+ dated values renders a time chart with reference range
and hover tooltips when expanded.
"""

import html
import json
import math
import re
import sys
from datetime import datetime, date
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


# ---------------------------------------------------------------- helpers

def esc(s):
    return html.escape(str(s)) if s is not None else ""


def load(data_dir, name):
    p = data_dir / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else []


def cc_text(cc):
    if not cc:
        return ""
    return cc.get("text") or next(
        (c.get("display") for c in cc.get("coding", []) if c.get("display")), ""
    )


def parse_dt(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def fmt_date(iso, with_time=False):
    dt = parse_dt(iso)
    if not dt:
        return iso[:10] if iso else "—"
    return dt.strftime("%b %-d, %Y" + (" %-I:%M %p" if with_time else ""))


def strip_html(s):
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def html_to_text(raw):
    """Attachment HTML -> readable plain text with line breaks preserved."""
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<br[^>]*>|</(p|div|tr|li|h[1-6])>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r" ?\n ?", "\n", txt)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def obs_category(o):
    for cat in o.get("category", []):
        for c in cat.get("coding", []):
            if c.get("code"):
                return c["code"]
    return "other"


def obs_value(o):
    if "valueQuantity" in o:
        q = o["valueQuantity"]
        v = q.get("value")
        v = f"{v:g}" if isinstance(v, (int, float)) else v
        return f"{v} {q.get('unit', '')}".strip()
    if "valueString" in o:
        return o["valueString"]
    if "valueCodeableConcept" in o:
        return cc_text(o["valueCodeableConcept"])
    if "component" in o:
        return "; ".join(
            f"{cc_text(c.get('code'))}: {obs_value(c)}" for c in o["component"]
        )
    return "—"


def obs_numeric(o):
    q = o.get("valueQuantity", {})
    return q.get("value") if isinstance(q.get("value"), (int, float)) else None


def obs_unit(o):
    return o.get("valueQuantity", {}).get("unit", "")


def ref_range_text(o):
    for r in o.get("referenceRange", []):
        if r.get("text"):
            return r["text"]
        low, high = r.get("low", {}).get("value"), r.get("high", {}).get("value")
        unit = r.get("low", {}).get("unit") or r.get("high", {}).get("unit") or ""
        if low is not None and high is not None:
            return f"{low:g}–{high:g} {unit}".strip()
        if high is not None:
            return f"≤ {high:g} {unit}".strip()
        if low is not None:
            return f"≥ {low:g} {unit}".strip()
    return ""


def ref_range_bounds(o):
    for r in o.get("referenceRange", []):
        low, high = r.get("low", {}).get("value"), r.get("high", {}).get("value")
        if low is not None or high is not None:
            return (low, high)
    return None


def interp_flag(o):
    for i in o.get("interpretation", []):
        code = next((c.get("code") for c in i.get("coding", [])), None)
        label = cc_text(i)
        if code in ("H", "HH", "HU"):
            return (label or "High", "flag-high")
        if code in ("L", "LL"):
            return (label or "Low", "flag-low")
        if code and code != "N":
            return (label or "Abnormal", "flag-abn")
    return None


def dl(pairs):
    items = "".join(
        f"<div><dt>{esc(k)}</dt><dd>{v}</dd></div>" for k, v in pairs if v and v != "—"
    )
    return f'<dl class="kv">{items}</dl>' if items else ""


# ---------------------------------------------------------------- charts

def sparkline(points):
    if len(points) < 3:
        return ""
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    w, h, pad = 120, 30, 4
    n = len(points)
    xy = [
        (pad + i * (w - 2 * pad) / (n - 1), h - pad - (v - lo) / span * (h - 2 * pad))
        for i, (_, v) in enumerate(points)
    ]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    lx, ly = xy[-1]
    return (
        f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="trend of {n} values">'
        f'<polyline points="{path}" fill="none" stroke="var(--series-1)" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="var(--series-1)"/></svg>'
    )


def nice_ticks(lo, hi, target=4):
    if hi <= lo:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag)
    start = math.ceil(lo / step) * step
    ticks = []
    t = start
    while t <= hi + 1e-9:
        ticks.append(round(t, 10))
        t += step
    return ticks


def fmt_num(v):
    return f"{v:g}"


def timechart(series, unit="", ref=None):
    """series: [(label, [(iso_date, value), ...] ascending)] — 1 or 2 series."""
    all_pts = [(d, v) for _, pts in series for d, v in pts]
    if len(all_pts) < 2:
        return ""
    times = [parse_dt(d).timestamp() for d, _ in all_pts]
    vals = [v for _, v in all_pts]
    t_lo, t_hi = min(times), max(times)
    if t_hi == t_lo:
        t_lo, t_hi = t_lo - 1, t_hi + 1
    v_lo, v_hi = min(vals), max(vals)
    if ref:
        if ref[0] is not None:
            v_lo = min(v_lo, ref[0])
        if ref[1] is not None:
            v_hi = max(v_hi, ref[1])
    pad_v = (v_hi - v_lo) * 0.12 or abs(v_hi) * 0.1 or 1
    v_lo, v_hi = v_lo - pad_v, v_hi + pad_v

    W, H = 660, 230
    ml, mr, mt, mb = 52, 100 if len(series) > 1 else 20, 14, 30
    iw, ih = W - ml - mr, H - mt - mb

    def X(t):
        return ml + (t - t_lo) / (t_hi - t_lo) * iw

    def Y(v):
        return mt + ih - (v - v_lo) / (v_hi - v_lo) * ih

    parts = []
    # reference range band
    if ref:
        lo_b = ref[0] if ref[0] is not None else v_lo
        hi_b = ref[1] if ref[1] is not None else v_hi
        y1, y2 = Y(hi_b), Y(lo_b)
        parts.append(
            f'<rect x="{ml}" y="{y1:.1f}" width="{iw}" height="{y2 - y1:.1f}" '
            f'fill="var(--band)"><title>reference range</title></rect>'
        )
    # y gridlines + labels
    for tv in nice_ticks(v_lo, v_hi):
        if tv < v_lo or tv > v_hi:
            continue
        y = Y(tv)
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + iw}" y2="{y:.1f}" stroke="var(--grid)"/>'
            f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick">{fmt_num(tv)}</text>'
        )
    # x ticks: evenly spaced positions labeled with interpolated dates
    n_x = 4
    span_days = (t_hi - t_lo) / 86400
    xfmt = "%b %-d" if span_days < 300 else ("%b %Y" if span_days < 1200 else "%Y")
    for i in range(n_x + 1):
        t = t_lo + (t_hi - t_lo) * i / n_x
        x = X(t)
        label = datetime.fromtimestamp(t).strftime(xfmt)
        anchor = "start" if i == 0 else ("end" if i == n_x else "middle")
        parts.append(
            f'<text x="{x:.1f}" y="{H - 8}" text-anchor="{anchor}" class="tick">{label}</text>'
        )
    parts.append(
        f'<line x1="{ml}" y1="{mt + ih}" x2="{ml + iw}" y2="{mt + ih}" stroke="var(--baseline)"/>'
    )
    # series lines + hover points
    for si, (label, pts) in enumerate(series):
        color = f"var(--series-{si + 1})"
        xy = [(X(parse_dt(d).timestamp()), Y(v), d, v) for d, v in pts]
        if len(xy) > 1:
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in xy)
            parts.append(
                f'<polyline points="{path}" fill="none" stroke="{color}" '
                f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for x, y, d, v in xy:
            tip = f"{fmt_date(d)} · {fmt_num(v)} {unit}".strip()
            if label:
                tip = f"{esc(label)} · {tip}"
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="transparent" '
                f'class="pt" data-tip="{esc(tip)}"/>'
            )
        if len(series) > 1 and xy:
            lx, ly, _, _ = xy[-1]
            parts.append(
                f'<text x="{lx + 8:.1f}" y="{ly + 4:.1f}" class="endlabel">{esc(label)}</text>'
            )
    # unit label
    if unit:
        parts.append(f'<text x="{ml - 8}" y="{mt - 2}" text-anchor="end" class="tick">{esc(unit)}</text>')

    legend = ""
    if len(series) > 1:
        legend = '<div class="legend">' + "".join(
            f'<span class="leg"><i style="background:var(--series-{i + 1})"></i>{esc(lbl)}</span>'
            for i, (lbl, _) in enumerate(series)
        ) + "</div>"
    return (
        f'<div class="chart">{legend}'
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="values over time">'
        + "".join(parts) + "</svg></div>"
    )


# ---------------------------------------------------------------- components

def section(anchor, title, count, body, note=""):
    note_html = f'<p class="note">{note}</p>' if note else ""
    return (
        f'<section id="{anchor}"><h2>{esc(title)} '
        f'<span class="count">{count}</span></h2>{note_html}{body}</section>'
    )


def rowlist(grid_class, headers, rows):
    """rows: [(cells:list[str], detail_html:str)] — detail '' → flat row."""
    if not rows:
        return '<p class="empty">None on record.</p>'
    out = [f'<div class="rl"><div class="hd {grid_class}">'
           + "".join(f"<span>{h}</span>" for h in headers) + "</div>"]
    for cells, detail in rows:
        cells_html = "".join(f"<span>{c}</span>" for c in cells)
        if detail:
            out.append(
                f'<details class="row"><summary class="{grid_class}">{cells_html}</summary>'
                f'<div class="detail">{detail}</div></details>'
            )
        else:
            out.append(f'<div class="row flat {grid_class}">{cells_html}</div>')
    out.append("</div>")
    return "".join(out)


def history_table(items):
    rows = []
    for o in items:
        f = interp_flag(o)
        fh = f' <span class="flag {f[1]}">▲ {esc(f[0])}</span>' if f else ""
        note_txt = "; ".join(n.get("text", "") for n in o.get("note", []) if n.get("text"))
        rows.append(
            "<tr>"
            f"<td>{esc(fmt_date(o.get('effectiveDateTime')))}</td>"
            f'<td><span class="num">{esc(obs_value(o))}</span>{fh}</td>'
            f"<td>{esc(ref_range_text(o) or '—')}</td>"
            f"<td>{esc(note_txt)}</td></tr>"
        )
    return (
        '<div class="tablewrap"><table><thead><tr><th>Date</th><th>Value</th>'
        "<th>Reference range</th><th>Notes</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


# ---------------------------------------------------------------- sections

def patient_header(patient, extracted_when, counts):
    name = ""
    for n in patient.get("name", []):
        if n.get("use") == "official" or not name:
            name = f"{' '.join(n.get('given', []))} {n.get('family', '')}".strip()
    birth = patient.get("birthDate", "")
    age = ""
    if birth:
        b = date.fromisoformat(birth)
        today = date.today()
        age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
        age = f" ({age} yrs)"
    telecom = ", ".join(t.get("value", "") for t in patient.get("telecom", []) if t.get("value"))
    addr = ""
    for a in patient.get("address", []):
        parts = [", ".join(a.get("line", [])), a.get("city"), a.get("state"), a.get("postalCode")]
        addr = ", ".join(p for p in parts if p)
        break
    gp = ", ".join(g.get("display", "") for g in patient.get("generalPractitioner", []) if g.get("display"))
    org = patient.get("managingOrganization", {}).get("display", "")

    meta_rows = [
        ("Born", f"{fmt_date(birth)}{age}"),
        ("Sex", (patient.get("gender") or "").capitalize()),
        ("Contact", telecom),
        ("Address", addr),
        ("Primary care", gp),
        ("Health system", org),
    ]
    meta = "".join(
        f'<div class="meta-item"><span class="meta-label">{esc(k)}</span>'
        f'<span class="meta-value">{esc(v)}</span></div>'
        for k, v in meta_rows if v
    )
    tiles = "".join(
        f'<a class="tile" href="#{anchor}"><span class="tile-num">{n}</span>'
        f'<span class="tile-label">{esc(label)}</span></a>'
        for label, n, anchor in counts
    )
    return (
        f"<header><h1>{esc(name)}</h1>"
        f'<p class="subtitle">Health record extracted {esc(extracted_when)} · '
        f"University of Utah Healthcare · via Epic patient-access FHIR API</p>"
        f'<div class="meta">{meta}</div><nav class="tiles">{tiles}</nav>'
        f'<p class="hint">Rows marked with <span class="chev-demo">▸</span> expand — '
        f"click to see history, charts, and details.</p></header>"
    )


def conditions_section(conds):
    def build(c):
        status = cc_text(c.get("clinicalStatus")) or "—"
        badge = (f'<span class="badge badge-active">{esc(status)}</span>'
                 if status == "Active" else esc(status))
        evidence = ", ".join(
            cc_text(code) for e in c.get("evidence", []) for code in e.get("code", []) if cc_text(code))
        detail = dl([
            ("Verification", cc_text(c.get("verificationStatus"))),
            ("Category", ", ".join(cc_text(cat) for cat in c.get("category", []) if cc_text(cat))),
            ("Severity", cc_text(c.get("severity"))),
            ("Onset", fmt_date(c.get("onsetDateTime")) if c.get("onsetDateTime") else ""),
            ("Recorded", fmt_date(c.get("recordedDate")) if c.get("recordedDate") else ""),
            ("Evidence", esc(evidence)),
        ])
        cells = [
            f"<strong>{esc(cc_text(c.get('code')))}</strong>",
            badge,
            esc(cc_text(c.get("severity")) or "—"),
            esc(fmt_date(c.get("onsetDateTime") or c.get("recordedDate"))),
        ]
        return (cells, detail)

    conds = sorted(conds, key=lambda c: c.get("onsetDateTime") or c.get("recordedDate") or "", reverse=True)
    problems = [c for c in conds if any(
        cd.get("code") == "problem-list-item"
        for cat in c.get("category", []) for cd in cat.get("coding", []))]
    diagnoses = [c for c in conds if c not in problems]
    headers = ["Condition", "Status", "Severity", "Onset/recorded"]
    body = ""
    if problems:
        body += "<h3>Problem list</h3>" + rowlist("g-cond", headers, [build(c) for c in problems])
    body += "<h3>Encounter diagnoses</h3>" + rowlist("g-cond", headers, [build(c) for c in diagnoses])
    return section("conditions", "Conditions", len(conds), body)


def medications_section(meds):
    def build(m):
        name = cc_text(m.get("medicationCodeableConcept")) or m.get(
            "medicationReference", {}).get("display", "—")
        doses = [d.get("patientInstruction") or d.get("text") or ""
                 for d in m.get("dosageInstruction", [])]
        dose = next((d for d in doses if d), "")
        reason = ", ".join(cc_text(r) for r in m.get("reasonCode", []) if cc_text(r))
        disp = m.get("dispenseRequest", {})
        qty = disp.get("quantity", {})
        qty_s = f"{qty.get('value', '')} {qty.get('unit', '')}".strip()
        supply = disp.get("expectedSupplyDuration", {})
        supply_s = f"{supply.get('value', '')} {supply.get('unit', '')}".strip()
        detail = dl([
            ("Status", m.get("status", "").capitalize()),
            ("All instructions", esc(" | ".join(d for d in doses if d))),
            ("Course of therapy", cc_text(m.get("courseOfTherapyType"))),
            ("Quantity per fill", esc(qty_s)),
            ("Refills allowed", str(disp["numberOfRepeatsAllowed"])
             if "numberOfRepeatsAllowed" in disp else ""),
            ("Expected supply", esc(supply_s)),
            ("Prescriber", esc(m.get("requester", {}).get("display", ""))),
            ("Ordered", fmt_date(m.get("authoredOn")) if m.get("authoredOn") else ""),
        ])
        cells = [
            f"<strong>{esc(name)}</strong>",
            esc(dose or "—"),
            esc(reason or "—"),
            esc(fmt_date(m.get("authoredOn"))),
        ]
        return (cells, detail)

    meds = sorted(meds, key=lambda m: m.get("authoredOn") or "", reverse=True)
    active = [m for m in meds if m.get("status") == "active"]
    other = [m for m in meds if m.get("status") != "active"]
    headers = ["Medication", "Instructions", "Reason", "Ordered"]
    body = f"<h3>Active ({len(active)})</h3>" + rowlist(
        "g-med", headers, [build(m) for m in active])
    if other:
        body += f"<h3>Stopped / past ({len(other)})</h3>" + rowlist(
            "g-med", headers, [build(m) for m in other])
    return section("medications", "Medications", len(meds), body)


def allergies_section(allergies):
    rows = [([
        f"<strong>{esc(cc_text(a.get('code')))}</strong>",
        esc(cc_text(a.get("clinicalStatus")) or "—"),
        esc(fmt_date(a.get("recordedDate"))),
    ], "") for a in allergies]
    return section("allergies", "Allergies", len(allergies),
                   rowlist("g-allergy", ["Allergen", "Status", "Recorded"], rows))


def obs_group_chart(items):
    """Chart for one test group: single numeric series, or one per component."""
    unit, ref = "", None
    with_comp = [o for o in items if o.get("component")]
    if with_comp:
        comp_series = {}
        for o in sorted(items, key=lambda x: x.get("effectiveDateTime") or ""):
            d = o.get("effectiveDateTime")
            if not d:
                continue
            for c in o.get("component", []):
                v = obs_numeric(c)
                if v is not None:
                    label = cc_text(c.get("code")) or "Value"
                    label = label.replace(" blood pressure", "")
                    comp_series.setdefault(label, []).append((d, v))
                    unit = unit or obs_unit(c)
        series = [(k, v) for k, v in comp_series.items() if len(v) >= 2][:2]
        return timechart(series, unit) if series else ""
    pts = [(o.get("effectiveDateTime"), obs_numeric(o))
           for o in sorted(items, key=lambda x: x.get("effectiveDateTime") or "")
           if obs_numeric(o) is not None and o.get("effectiveDateTime")]
    if len(pts) < 2:
        return ""
    latest = max(items, key=lambda o: o.get("effectiveDateTime") or "")
    return timechart([("", pts)], obs_unit(latest), ref_range_bounds(latest))


def grouped_obs_section(anchor, title, observations, note=""):
    groups = {}
    for o in observations:
        groups.setdefault(cc_text(o.get("code")) or "Unnamed", []).append(o)

    rows = []
    for name, items in sorted(groups.items(), key=lambda kv: max(
            i.get("effectiveDateTime") or "" for i in kv[1]), reverse=True):
        items.sort(key=lambda o: o.get("effectiveDateTime") or "", reverse=True)
        numeric = [(o.get("effectiveDateTime"), obs_numeric(o))
                   for o in reversed(items)
                   if obs_numeric(o) is not None and o.get("effectiveDateTime")]
        latest = items[0]
        flag = interp_flag(latest)
        flag_html = f' <span class="flag {flag[1]}">▲ {esc(flag[0])}</span>' if flag else ""
        cells = [
            f'<span class="obs-name">{esc(name)}</span>',
            f'<span class="num">{esc(obs_value(latest))}</span>{flag_html}'
            f'<span class="when">{esc(fmt_date(latest.get("effectiveDateTime")))}</span>',
            esc(ref_range_text(latest)),
            sparkline(numeric),
            f'{len(items)} result{"s" if len(items) > 1 else ""}',
        ]
        detail = obs_group_chart(items) + history_table(items)
        rows.append((cells, detail))
    return section(anchor, title, len(observations),
                   rowlist("g-obs", ["Test", "Latest", "Reference range", "Trend", ""], rows),
                   note=note)


def immunizations_section(imms):
    groups = {}
    for i in imms:
        groups.setdefault(cc_text(i.get("vaccineCode")) or "Unknown", []).append(i)
    rows = []
    for name, items in sorted(groups.items(), key=lambda kv: max(
            i.get("occurrenceDateTime") or "" for i in kv[1]), reverse=True):
        items.sort(key=lambda i: i.get("occurrenceDateTime") or "", reverse=True)
        dose_rows = []
        has_detail = False
        for i in items:
            extras = [
                esc(fmt_date(i.get("occurrenceDateTime"))),
                esc(i.get("location", {}).get("display", "") or "—"),
                esc(i.get("lotNumber", "") or "—"),
                esc(cc_text(i.get("site")) or "—"),
                esc(cc_text(i.get("route")) or "—"),
            ]
            if any(x != "—" for x in extras[1:]):
                has_detail = True
            dose_rows.append("<tr>" + "".join(f"<td>{x}</td>" for x in extras) + "</tr>")
        detail = (
            '<div class="tablewrap"><table><thead><tr><th>Date</th><th>Location</th>'
            "<th>Lot #</th><th>Site</th><th>Route</th></tr></thead>"
            f"<tbody>{''.join(dose_rows)}</tbody></table></div>"
        ) if (has_detail or len(items) > 3) else ""
        dates = [i.get("occurrenceDateTime") for i in items if i.get("occurrenceDateTime")]
        dates_s = ", ".join(fmt_date(d) for d in dates[:3]) + (" …" if len(dates) > 3 else "")
        cells = [f"<strong>{esc(name)}</strong>", str(len(items)), esc(dates_s)]
        rows.append((cells, detail))
    return section("immunizations", "Immunizations", len(imms),
                   rowlist("g-imm", ["Vaccine", "Doses", "Dates"], rows))


def reports_section(reports, obs_by_id, att_dir):
    rows = []
    for r in sorted(reports, key=lambda x: x.get("effectiveDateTime") or "", reverse=True):
        narrative = ""
        p = att_dir / f"DiagnosticReport_{r.get('id')}.html"
        if p.exists():
            text = html_to_text(p.read_text(errors="replace"))
            if text:
                narrative = f'<div class="notebody">{esc(text[:30000])}</div>'
        results = []
        for ref in r.get("result", []):
            rid = (ref.get("reference") or "").split("/")[-1]
            o = obs_by_id.get(rid)
            if o:
                f = interp_flag(o)
                fh = f' <span class="flag {f[1]}">▲ {esc(f[0])}</span>' if f else ""
                results.append(
                    "<tr>"
                    f"<td>{esc(cc_text(o.get('code')))}</td>"
                    f'<td><span class="num">{esc(obs_value(o))}</span>{fh}</td>'
                    f"<td>{esc(ref_range_text(o) or '—')}</td></tr>"
                )
            elif ref.get("display"):
                results.append(f"<tr><td>{esc(ref['display'])}</td><td>—</td><td>—</td></tr>")
        conclusion = ", ".join(cc_text(c) for c in r.get("conclusionCode", []) if cc_text(c))
        detail = ""
        if results:
            detail = (
                '<div class="tablewrap"><table><thead><tr><th>Result</th><th>Value</th>'
                "<th>Reference range</th></tr></thead>"
                f"<tbody>{''.join(results)}</tbody></table></div>"
            )
        if conclusion:
            detail = dl([("Conclusion", esc(conclusion))]) + detail
        detail += narrative
        n = len(r.get("result", []))
        cells = [
            f"<strong>{esc(cc_text(r.get('code')))}</strong>",
            esc(fmt_date(r.get("effectiveDateTime"))),
            esc(", ".join(p.get("display", "") for p in r.get("performer", []) if p.get("display")) or "—"),
            f"{n} result{'s' if n != 1 else ''}",
        ]
        rows.append((cells, detail))
    return section("reports", "Diagnostic reports", len(reports),
                   rowlist("g-report", ["Report", "Date", "Performer", "Contents"], rows),
                   note="Expand a report to see the individual results inside it.")


def notes_section(docs, att_dir):
    rows = []
    for d in sorted(docs, key=lambda x: x.get("date") or "", reverse=True):
        authors = ", ".join(a.get("display", "") for a in d.get("author", []) if a.get("display"))
        period = d.get("context", {}).get("period", {})
        body_html = ""
        for ext in ("html", "txt"):
            p = att_dir / f"DocumentReference_{d.get('id')}.{ext}"
            if p.exists():
                raw = p.read_text(errors="replace")
                text = html_to_text(raw) if ext == "html" else raw.strip()
                if len(text) > 30000:
                    text = text[:30000] + f"\n[… truncated, {len(text):,} chars total]"
                if len(text) >= 5:
                    body_html = f'<div class="notebody">{esc(text)}</div>'
                break
        detail = dl([
            ("Category", ", ".join(cc_text(c) for c in d.get("category", []) if cc_text(c))),
            ("Care period", f"{fmt_date(period.get('start'))} – {fmt_date(period.get('end'))}"
             if period.get("start") else ""),
            ("Custodian", esc(d.get("custodian", {}).get("display", ""))),
        ] + ([] if body_html else [(
            "Note body", "Not downloaded in this extraction — re-run "
                         "extract_mychart.py (requires a fresh MyChart login).")]))
        detail += body_html
        cells = [
            f"<strong>{esc(cc_text(d.get('type')))}</strong>",
            esc(fmt_date(d.get("date"))),
            esc(authors or "—"),
            esc(d.get("docStatus", d.get("status", "—"))),
        ]
        rows.append((cells, detail))
    return section("notes", "Clinical notes & documents", len(docs),
                   rowlist("g-note", ["Type", "Date", "Author", "Status"], rows))


def encounters_section(encs):
    rows = []
    for e in sorted(encs, key=lambda x: x.get("period", {}).get("start") or "", reverse=True):
        etype = ", ".join(cc_text(t) for t in e.get("type", []) if cc_text(t))
        reason = ", ".join(cc_text(r) for r in e.get("reasonCode", []) if cc_text(r))
        locs = ", ".join(l.get("location", {}).get("display", "")
                         for l in e.get("location", []) if l.get("location", {}).get("display"))
        participants = "; ".join(
            f"{p.get('individual', {}).get('display', '')}"
            + (f" ({cc_text(p['type'][0])})" if p.get("type") else "")
            for p in e.get("participant", []) if p.get("individual", {}).get("display"))
        period = e.get("period", {})
        disposition = cc_text(e.get("hospitalization", {}).get("dischargeDisposition"))
        detail = dl([
            ("Class", esc(e.get("class", {}).get("display", ""))),
            ("Service type", cc_text(e.get("serviceType"))),
            ("Period", f"{fmt_date(period.get('start'), True)} – "
                       f"{fmt_date(period.get('end'), True) if period.get('end') else 'ongoing'}"),
            ("Providers", esc(participants)),
            ("Location", esc(locs)),
            ("Discharge disposition", esc(disposition)),
        ])
        provider = next((p.get("individual", {}).get("display", "")
                         for p in e.get("participant", [])
                         if p.get("individual", {}).get("display")), "")
        cells = [
            esc(fmt_date(period.get("start"))),
            f"<strong>{esc(etype or cc_text(e.get('serviceType')) or '—')}</strong>",
            esc(reason or "—"),
            esc(provider or "—"),
        ]
        rows.append((cells, detail))
    return section("encounters", "Visits", len(encs),
                   rowlist("g-enc", ["Date", "Type", "Reason", "Provider"], rows))


def careplan_section(plans, teams):
    body = ""
    if plans:
        rows = []
        for p in plans:
            narrative = strip_html(p.get("text", {}).get("div", ""))
            notes = "; ".join(n.get("text", "") for n in p.get("note", []) if n.get("text"))
            addresses = ", ".join(a.get("display", "") for a in p.get("addresses", []) if a.get("display"))
            detail = dl([
                ("Narrative", esc(narrative)),
                ("Notes", esc(notes)),
                ("Addresses", esc(addresses)),
            ]) if (narrative or notes or addresses) else ""
            cells = [
                f"<strong>{esc(', '.join(cc_text(c) for c in p.get('category', []) if cc_text(c)) or 'Care plan')}</strong>",
                esc(p.get("status", "—")),
                esc((notes or narrative or "—")[:110]),
            ]
            rows.append((cells, detail))
        body += "<h3>Care plans</h3>" + rowlist("g-plan", ["Plan", "Status", "Summary"], rows)
    if teams:
        rows = []
        for t in teams:
            for part in t.get("participant", []):
                member = part.get("member", {}).get("display", "")
                role = ", ".join(cc_text(r) for r in part.get("role", []) if cc_text(r))
                if member:
                    rows.append(([f"<strong>{esc(member)}</strong>", esc(role or "—"),
                                  esc(t.get("status", "—"))], ""))
        body += "<h3>Care team</h3>" + rowlist("g-team", ["Member", "Role", "Status"], rows)
    return section("care", "Care plans & team", len(plans) + len(teams), body)


# ---------------------------------------------------------------- page

CSS = """
:root {
  --surface-1: #fcfcfb; --page: #f9f9f7;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
  --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #1baf7a;
  --band: rgba(42,120,214,0.08);
  --flag-high: #d03b3b; --flag-low: #1c5cab; --flag-abn: #b35309;
  --badge-bg: #e7f0db; --badge-ink: #006300;
  --tip-bg: #0b0b0b; --tip-ink: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #199e70;
    --band: rgba(57,135,229,0.12);
    --flag-high: #e66767; --flag-low: #6da7ec; --flag-abn: #eda100;
    --badge-bg: #1e3320; --badge-ink: #7ed87e;
    --tip-bg: #ffffff; --tip-ink: #0b0b0b;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--text-primary);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 1000px; margin: 0 auto; padding: 0 20px 80px; }
header { padding: 40px 0 8px; }
h1 { font-size: 30px; margin: 0 0 4px; }
.subtitle { color: var(--text-secondary); margin: 0 0 20px; }
.meta { display: flex; flex-wrap: wrap; gap: 8px 36px; margin-bottom: 24px; }
.meta-item { display: flex; flex-direction: column; }
.meta-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(108px, 1fr)); gap: 10px; }
.tile {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 14px; text-decoration: none; color: inherit; display: flex; flex-direction: column;
}
.tile:hover { border-color: var(--series-1); }
.tile-num { font-size: 24px; font-weight: 650; }
.tile-label { font-size: 12px; color: var(--text-secondary); }
.hint { color: var(--text-secondary); font-size: 13.5px; margin-top: 18px; }
.chev-demo { color: var(--series-1); font-weight: 700; }
section { margin-top: 44px; }
h2 { font-size: 20px; margin: 0 0 6px; padding-bottom: 8px; border-bottom: 1px solid var(--grid); }
h3 { font-size: 14px; color: var(--text-secondary); margin: 22px 0 8px; }
.count { color: var(--muted); font-weight: 400; font-size: 15px; }
.note, .empty { color: var(--text-secondary); font-size: 13.5px; }

/* row lists */
.rl { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
      margin-bottom: 14px; overflow: hidden; }
.hd { font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
      font-weight: 550; border-bottom: 1px solid var(--grid); }
.hd, .row.flat, .row summary { display: grid; gap: 14px; align-items: center; padding: 10px 14px 10px 34px; }
.row.flat { padding-left: 34px; }
.hd { padding-left: 34px; }
.row { border-bottom: 1px solid var(--grid); }
.rl > .row:last-child, .rl > details.row:last-child { border-bottom: none; }
details.row summary { cursor: pointer; list-style: none; position: relative; }
details.row summary::-webkit-details-marker { display: none; }
details.row summary::before {
  content: "▸"; position: absolute; left: 14px; color: var(--series-1);
  font-weight: 700; transition: transform .15s;
}
details.row[open] summary::before { transform: rotate(90deg); }
details.row summary:hover { background: color-mix(in srgb, var(--series-1) 6%, transparent); }
.row.flat { color: inherit; }
.detail { padding: 4px 14px 14px 34px; }
.g-cond   { grid-template-columns: 2.4fr 1fr .9fr 1.1fr; }
.g-med    { grid-template-columns: 1.8fr 2.2fr 1.2fr .9fr; }
.g-allergy{ grid-template-columns: 2fr 1fr 1fr; }
.g-obs    { grid-template-columns: 2.1fr 1.5fr 1.1fr 130px 72px; }
.g-imm    { grid-template-columns: 2fr .6fr 2.4fr; }
.g-report { grid-template-columns: 2.2fr 1fr 1.6fr .9fr; }
.g-note   { grid-template-columns: 1.8fr 1fr 1.6fr .8fr; }
.g-enc    { grid-template-columns: 1fr 1.8fr 1.6fr 1.4fr; }
.g-plan   { grid-template-columns: 1.4fr .7fr 2.4fr; }
.g-team   { grid-template-columns: 1.6fr 1.6fr .8fr; }

.num { font-variant-numeric: tabular-nums; }
.when { display: block; font-size: 12px; color: var(--muted); }
.obs-name { font-weight: 550; }
.badge { background: var(--badge-bg); color: var(--badge-ink); font-size: 12px;
         padding: 2px 8px; border-radius: 999px; font-weight: 550; justify-self: start; }
.flag { font-size: 12px; font-weight: 600; }
.flag-high { color: var(--flag-high); } .flag-low { color: var(--flag-low); }
.flag-abn { color: var(--flag-abn); }
.spark { display: block; }

/* key-value details */
.kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px 24px; margin: 8px 0 12px; }
.kv div { min-width: 0; }
.kv dt { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.kv dd { margin: 0; font-size: 14px; overflow-wrap: break-word; }

.notebody { white-space: pre-line; font-size: 13.5px; line-height: 1.5;
            background: var(--page); border: 1px solid var(--grid); border-radius: 8px;
            padding: 12px 16px; margin-top: 8px; max-height: 480px; overflow-y: auto; }

/* nested tables */
.tablewrap { overflow-x: auto; border: 1px solid var(--grid); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
     color: var(--muted); font-weight: 550; padding: 8px 12px; border-bottom: 1px solid var(--grid); }
td { padding: 8px 12px; border-bottom: 1px solid var(--grid); vertical-align: top; }
tr:last-child td { border-bottom: none; }

/* charts */
.chart { margin: 6px 0 14px; max-width: 680px; }
.chart svg { width: 100%; height: auto; display: block; }
.tick { font-size: 11px; fill: var(--muted); font-family: inherit; font-variant-numeric: tabular-nums; }
.endlabel { font-size: 12px; fill: var(--text-secondary); font-family: inherit; }
.legend { display: flex; gap: 16px; font-size: 12.5px; color: var(--text-secondary); margin-bottom: 4px; }
.leg i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }
#tip { position: fixed; pointer-events: none; background: var(--tip-bg); color: var(--tip-ink);
       font-size: 12.5px; padding: 5px 9px; border-radius: 6px; z-index: 10; display: none;
       font-variant-numeric: tabular-nums; white-space: nowrap; }

@media (max-width: 720px) {
  .hd, .row.flat, .row summary { grid-template-columns: 1fr auto !important; }
  .hd span:nth-child(n+3), .row.flat span:nth-child(n+3),
  .row summary span:nth-child(n+3) { display: none; }
  .detail { padding-left: 14px; }
}
"""

JS = """
const tip = document.createElement('div'); tip.id = 'tip'; document.body.appendChild(tip);
document.querySelectorAll('.pt').forEach(el => {
  el.addEventListener('mouseenter', () => { tip.textContent = el.dataset.tip; tip.style.display = 'block'; });
  el.addEventListener('mousemove', e => {
    tip.style.left = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8) + 'px';
    tip.style.top = (e.clientY - 34) + 'px';
  });
  el.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
});
"""


def main():
    if len(sys.argv) > 1:
        data_dir = Path(sys.argv[1])
    else:
        runs = sorted(d for d in DATA_DIR.iterdir() if d.is_dir())
        if not runs:
            sys.exit("No extractions found under data/. Run extract_mychart.py first.")
        data_dir = runs[-1]

    patient = (load(data_dir, "Patient") or [{}])[0]
    conds = load(data_dir, "Condition")
    meds = load(data_dir, "MedicationRequest")
    allergies = load(data_dir, "AllergyIntolerance")
    obs = load(data_dir, "Observation")
    imms = load(data_dir, "Immunization")
    reports = load(data_dir, "DiagnosticReport")
    docs = load(data_dir, "DocumentReference")
    encs = load(data_dir, "Encounter")
    plans = load(data_dir, "CarePlan")
    teams = load(data_dir, "CareTeam")

    obs_by_id = {o.get("id"): o for o in obs}
    labs = [o for o in obs if obs_category(o) == "laboratory"]
    vitals = [o for o in obs if obs_category(o) == "vital-signs"]
    social = [o for o in obs if obs_category(o) == "social-history"]

    extracted_when = fmt_date(
        datetime.strptime(data_dir.name, "%Y%m%d_%H%M%S").isoformat()
    ) if re.fullmatch(r"\d{8}_\d{6}", data_dir.name) else data_dir.name

    counts = [
        ("Conditions", len(conds), "conditions"),
        ("Medications", len(meds), "medications"),
        ("Allergies", len(allergies), "allergies"),
        ("Lab results", len(labs), "labs"),
        ("Vitals", len(vitals), "vitals"),
        ("Immunizations", len(imms), "immunizations"),
        ("Reports", len(reports), "reports"),
        ("Notes", len(docs), "notes"),
        ("Visits", len(encs), "encounters"),
    ]

    body = "".join([
        patient_header(patient, extracted_when, counts),
        conditions_section(conds),
        medications_section(meds),
        allergies_section(allergies),
        grouped_obs_section("labs", "Lab results", labs,
                            note="Grouped by test, newest first. Expand for the full "
                                 "history and a chart when there are 2+ numeric results."),
        grouped_obs_section("vitals", "Vital signs", vitals),
        grouped_obs_section("social", "Social history", social),
        immunizations_section(imms),
        reports_section(reports, obs_by_id, data_dir / "attachments"),
        notes_section(docs, data_dir / "attachments"),
        encounters_section(encs),
        careplan_section(plans, teams),
    ])

    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Health Record</title>"
        f"<style>{CSS}</style></head><body><main>{body}</main>"
        f"<script>{JS}</script></body></html>"
    )
    out = data_dir / "report.html"
    out.write_text(page)
    print(f"Wrote {out.resolve().as_uri()}")


if __name__ == "__main__":
    main()
