#!/usr/bin/env python3
"""Generate a self-contained HTML report from an extract_mychart.py data dir.

Usage:
    python3 generate_report.py [data/<timestamp>]

Defaults to the most recent extraction under data/. Writes report.html into
the same folder (kept out of git — it contains PHI).
"""

import html
import json
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
    """Human text for a FHIR CodeableConcept."""
    if not cc:
        return ""
    return cc.get("text") or next(
        (c.get("display") for c in cc.get("coding", []) if c.get("display")), ""
    )


def fmt_date(iso, with_time=False):
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %Y" + (" %-I:%M %p" if with_time else ""))
    except ValueError:
        return iso[:10]


def strip_html(s):
    return re.sub(r"<[^>]+>", " ", s or "").strip()


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


def ref_range(o):
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


def interp_flag(o):
    """(label, css_class) for an abnormal-result flag."""
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


def sparkline(points):
    """Inline SVG sparkline for [(iso_date, value)] sorted ascending."""
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
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="transparent">'
        f"<title>{fmt_date(d)}: {v:g}</title></circle>"
        for (x, y), (d, v) in zip(xy, points)
    )
    lx, ly = xy[-1]
    return (
        f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="trend of {n} values">'
        f'<polyline points="{path}" fill="none" stroke="var(--series-1)" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="var(--series-1)"/>'
        f"{dots}</svg>"
    )


# ---------------------------------------------------------------- sections

def section(anchor, title, count, body, note=""):
    note_html = f'<p class="note">{note}</p>' if note else ""
    return (
        f'<section id="{anchor}"><h2>{esc(title)} '
        f'<span class="count">{count}</span></h2>{note_html}{body}</section>'
    )


def table(headers, rows, cls=""):
    if not rows:
        return '<p class="empty">None on record.</p>'
    thead = "".join(f"<th>{h}</th>" for h in headers)
    tbody = "".join(f"<tr>{''.join(f'<td>{c}</td>' for c in r)}</tr>" for r in rows)
    return (
        f'<div class="tablewrap"><table class="{cls}">'
        f"<thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>"
    )


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
    telecom = ", ".join(
        f"{t.get('value')}" for t in patient.get("telecom", []) if t.get("value")
    )
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
        f'<header><h1>{esc(name)}</h1>'
        f'<p class="subtitle">Health record extracted {esc(extracted_when)} · '
        f"University of Utah Healthcare · via Epic patient-access FHIR API</p>"
        f'<div class="meta">{meta}</div><nav class="tiles">{tiles}</nav></header>'
    )


def conditions_section(conds):
    def row(c):
        status = cc_text(c.get("clinicalStatus")) or "—"
        badge = f'<span class="badge badge-active">{esc(status)}</span>' if status == "Active" else esc(status)
        return [
            f"<strong>{esc(cc_text(c.get('code')))}</strong>",
            badge,
            esc(cc_text(c.get("severity")) or "—"),
            esc(fmt_date(c.get("onsetDateTime") or c.get("recordedDate"))),
        ]

    conds = sorted(conds, key=lambda c: c.get("onsetDateTime") or c.get("recordedDate") or "", reverse=True)
    problems = [c for c in conds if any(
        cd.get("code") == "problem-list-item"
        for cat in c.get("category", []) for cd in cat.get("coding", []))]
    diagnoses = [c for c in conds if c not in problems]
    body = ""
    if problems:
        body += "<h3>Problem list</h3>" + table(
            ["Condition", "Status", "Severity", "Onset/recorded"], [row(c) for c in problems])
    body += "<h3>Encounter diagnoses</h3>" + table(
        ["Condition", "Status", "Severity", "Onset/recorded"], [row(c) for c in diagnoses])
    return section("conditions", "Conditions", len(conds), body)


def medications_section(meds):
    def row(m):
        name = cc_text(m.get("medicationCodeableConcept")) or m.get(
            "medicationReference", {}).get("display", "—")
        dose = ""
        for d in m.get("dosageInstruction", []):
            dose = d.get("patientInstruction") or d.get("text") or ""
            break
        reason = ", ".join(cc_text(r) for r in m.get("reasonCode", []) if cc_text(r))
        return [
            f"<strong>{esc(name)}</strong>",
            esc(dose or "—"),
            esc(reason or "—"),
            esc(fmt_date(m.get("authoredOn"))),
        ]

    meds = sorted(meds, key=lambda m: m.get("authoredOn") or "", reverse=True)
    active = [m for m in meds if m.get("status") == "active"]
    other = [m for m in meds if m.get("status") != "active"]
    body = f"<h3>Active ({len(active)})</h3>" + table(
        ["Medication", "Instructions", "Reason", "Ordered"], [row(m) for m in active])
    if other:
        body += f"<h3>Stopped / past ({len(other)})</h3>" + table(
            ["Medication", "Instructions", "Reason", "Ordered"], [row(m) for m in other])
    return section("medications", "Medications", len(meds), body)


def allergies_section(allergies):
    rows = [[
        f"<strong>{esc(cc_text(a.get('code')))}</strong>",
        esc(cc_text(a.get("clinicalStatus")) or "—"),
        esc(fmt_date(a.get("recordedDate"))),
    ] for a in allergies]
    return section("allergies", "Allergies", len(allergies),
                   table(["Allergen", "Status", "Recorded"], rows))


def grouped_obs_section(anchor, title, observations, note=""):
    """Group observations by test name; sparkline numeric series."""
    groups = {}
    for o in observations:
        groups.setdefault(cc_text(o.get("code")) or "Unnamed", []).append(o)

    def latest_date(items):
        return max(i.get("effectiveDateTime") or "" for i in items)

    blocks = []
    for name, items in sorted(groups.items(), key=lambda kv: latest_date(kv[1]), reverse=True):
        items.sort(key=lambda o: o.get("effectiveDateTime") or "", reverse=True)
        numeric = [(o.get("effectiveDateTime"), obs_numeric(o))
                   for o in reversed(items) if obs_numeric(o) is not None
                   and o.get("effectiveDateTime")]
        spark = sparkline(numeric)
        latest = items[0]
        flag = interp_flag(latest)
        flag_html = f' <span class="flag {flag[1]}">▲ {esc(flag[0])}</span>' if flag else ""
        rr = ref_range(latest)
        rows = []
        for o in items:
            f = interp_flag(o)
            fh = f' <span class="flag {f[1]}">▲ {esc(f[0])}</span>' if f else ""
            note_txt = "; ".join(n.get("text", "") for n in o.get("note", []) if n.get("text"))
            rows.append([
                esc(fmt_date(o.get("effectiveDateTime"))),
                f'<span class="num">{esc(obs_value(o))}</span>{fh}',
                esc(ref_range(o) or "—"),
                esc(note_txt or ""),
            ])
        detail = table(["Date", "Value", "Reference range", "Notes"], rows, cls="obs")
        blocks.append(
            f'<details class="obs-group"><summary>'
            f'<span class="obs-name">{esc(name)}</span>'
            f'<span class="obs-latest"><span class="num">{esc(obs_value(latest))}</span>{flag_html}</span>'
            f'<span class="obs-range">{esc(rr)}</span>'
            f'<span class="obs-spark">{spark}</span>'
            f'<span class="obs-n">{len(items)} result{"s" if len(items) > 1 else ""}</span>'
            f"</summary>{detail}</details>"
        )
    return section(anchor, title, len(observations), "".join(blocks), note=note)


def immunizations_section(imms):
    groups = {}
    for i in imms:
        groups.setdefault(cc_text(i.get("vaccineCode")) or "Unknown", []).append(i)
    rows = []
    for name, items in sorted(groups.items(), key=lambda kv: max(
            i.get("occurrenceDateTime") or "" for i in kv[1]), reverse=True):
        dates = sorted((i.get("occurrenceDateTime") or "" for i in items), reverse=True)
        rows.append([
            f"<strong>{esc(name)}</strong>",
            str(len(items)),
            esc(", ".join(fmt_date(d) for d in dates if d)),
        ])
    return section("immunizations", "Immunizations", len(imms),
                   table(["Vaccine", "Doses", "Dates"], rows))


def reports_section(reports):
    rows = []
    for r in sorted(reports, key=lambda x: x.get("effectiveDateTime") or "", reverse=True):
        n_results = len(r.get("result", []))
        conclusion = ", ".join(cc_text(c) for c in r.get("conclusionCode", []) if cc_text(c))
        rows.append([
            f"<strong>{esc(cc_text(r.get('code')))}</strong>",
            esc(fmt_date(r.get("effectiveDateTime"))),
            esc(", ".join(p.get("display", "") for p in r.get("performer", []) if p.get("display")) or "—"),
            f"{n_results} result{'s' if n_results != 1 else ''}"
            + (f" · {esc(conclusion)}" if conclusion else ""),
        ])
    return section("reports", "Diagnostic reports", len(reports),
                   table(["Report", "Date", "Performer", "Contents"], rows))


def notes_section(docs):
    rows = []
    for d in sorted(docs, key=lambda x: x.get("date") or "", reverse=True):
        authors = ", ".join(a.get("display", "") for a in d.get("author", []) if a.get("display"))
        rows.append([
            f"<strong>{esc(cc_text(d.get('type')))}</strong>",
            esc(fmt_date(d.get("date"))),
            esc(authors or "—"),
            esc(d.get("docStatus", d.get("status", "—"))),
        ])
    return section(
        "notes", "Clinical notes & documents", len(docs),
        table(["Type", "Date", "Author", "Status"], rows),
        note="Note text is stored behind the health system's authenticated Binary "
             "endpoint; this extract holds the metadata. Pulling note bodies needs "
             "a fresh authorization (access tokens last one hour).")


def encounters_section(encs):
    rows = []
    for e in sorted(encs, key=lambda x: x.get("period", {}).get("start") or "", reverse=True):
        etype = ", ".join(cc_text(t) for t in e.get("type", []) if cc_text(t))
        reason = ", ".join(cc_text(r) for r in e.get("reasonCode", []) if cc_text(r))
        loc = ", ".join(l.get("location", {}).get("display", "")
                        for l in e.get("location", []) if l.get("location", {}).get("display"))
        provider = ", ".join(p.get("individual", {}).get("display", "")
                             for p in e.get("participant", [])
                             if p.get("individual", {}).get("display"))
        rows.append([
            esc(fmt_date(e.get("period", {}).get("start"))),
            f"<strong>{esc(etype or cc_text(e.get('serviceType')) or '—')}</strong>",
            esc(reason or "—"),
            esc(provider or "—"),
            esc(loc or "—"),
        ])
    return section("encounters", "Visits", len(encs),
                   table(["Date", "Type", "Reason", "Provider", "Location"], rows))


def careplan_section(plans, teams):
    body = ""
    if plans:
        rows = []
        for p in plans:
            narrative = strip_html(p.get("text", {}).get("div", ""))
            notes = "; ".join(n.get("text", "") for n in p.get("note", []) if n.get("text"))
            rows.append([
                f"<strong>{esc(', '.join(cc_text(c) for c in p.get('category', []) if cc_text(c)) or 'Care plan')}</strong>",
                esc(p.get("status", "—")),
                esc(notes or narrative or "—"),
            ])
        body += "<h3>Care plans</h3>" + table(["Plan", "Status", "Details"], rows)
    if teams:
        rows = []
        for t in teams:
            for part in t.get("participant", []):
                member = part.get("member", {}).get("display", "")
                role = ", ".join(cc_text(r) for r in part.get("role", []) if cc_text(r))
                if member:
                    rows.append([f"<strong>{esc(member)}</strong>", esc(role or "—"),
                                 esc(t.get("status", "—"))])
        body += "<h3>Care team</h3>" + table(["Member", "Role", "Status"], rows)
    return section("care", "Care plans & team", len(plans) + len(teams), body)


# ---------------------------------------------------------------- page

CSS = """
:root {
  --surface-1: #fcfcfb; --page: #f9f9f7;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
  --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6;
  --flag-high: #d03b3b; --flag-low: #1c5cab; --flag-abn: #b35309;
  --badge-bg: #e7f0db; --badge-ink: #006300;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --flag-high: #e66767; --flag-low: #6da7ec; --flag-abn: #eda100;
    --badge-bg: #1e3320; --badge-ink: #7ed87e;
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
.meta-value { color: var(--text-primary); }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(108px, 1fr)); gap: 10px; }
.tile {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 14px; text-decoration: none; color: inherit; display: flex; flex-direction: column;
}
.tile:hover { border-color: var(--series-1); }
.tile-num { font-size: 24px; font-weight: 650; }
.tile-label { font-size: 12px; color: var(--text-secondary); }
section { margin-top: 44px; }
h2 { font-size: 20px; margin: 0 0 6px; padding-bottom: 8px; border-bottom: 1px solid var(--grid); }
h3 { font-size: 14px; color: var(--text-secondary); margin: 22px 0 8px; }
.count { color: var(--muted); font-weight: 400; font-size: 15px; }
.note, .empty { color: var(--text-secondary); font-size: 13.5px; }
.tablewrap { overflow-x: auto; background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th { text-align: left; font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em;
     color: var(--muted); font-weight: 550; padding: 10px 14px; border-bottom: 1px solid var(--grid); }
td { padding: 9px 14px; border-bottom: 1px solid var(--grid); vertical-align: top; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: color-mix(in srgb, var(--series-1) 5%, transparent); }
.num { font-variant-numeric: tabular-nums; }
.badge { background: var(--badge-bg); color: var(--badge-ink); font-size: 12px;
         padding: 2px 8px; border-radius: 999px; font-weight: 550; }
.flag { font-size: 12px; font-weight: 600; }
.flag-high { color: var(--flag-high); } .flag-low { color: var(--flag-low); }
.flag-abn { color: var(--flag-abn); }
.obs-group { background: var(--surface-1); border: 1px solid var(--border);
             border-radius: 10px; margin-bottom: 8px; }
.obs-group[open] { padding-bottom: 4px; }
.obs-group summary {
  display: grid; grid-template-columns: minmax(180px, 2.4fr) minmax(110px, 1.4fr) minmax(90px, 1.2fr) 130px 78px;
  gap: 14px; align-items: center; padding: 10px 14px; cursor: pointer; list-style: none;
}
.obs-group summary::-webkit-details-marker { display: none; }
.obs-group summary:hover { background: color-mix(in srgb, var(--series-1) 5%, transparent); border-radius: 10px; }
.obs-name { font-weight: 550; }
.obs-latest { font-variant-numeric: tabular-nums; }
.obs-range, .obs-n { color: var(--muted); font-size: 12.5px; }
.obs-n { text-align: right; }
.spark { display: block; }
.obs-group .tablewrap { border: none; border-top: 1px solid var(--grid); border-radius: 0; margin: 0 4px; }
@media (max-width: 720px) {
  .obs-group summary { grid-template-columns: 1fr auto; }
  .obs-range, .obs-spark, .obs-n { display: none; }
}
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
                            note="Grouped by test. Click a row for full history; "
                                 "trend lines appear for tests with 3+ numeric results."),
        grouped_obs_section("vitals", "Vital signs", vitals),
        grouped_obs_section("social", "Social history", social),
        immunizations_section(imms),
        reports_section(reports),
        notes_section(docs),
        encounters_section(encs),
        careplan_section(plans, teams),
    ])

    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Health Record</title>"
        f"<style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )
    out = data_dir / "report.html"
    out.write_text(page)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
