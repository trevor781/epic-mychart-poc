#!/usr/bin/env python3
"""Compile an extraction (plus optional Apple Health summary) into one
LLM-ready markdown record.

Usage:
    python3 compile_record.py [data/<timestamp>]

Writes record.md (the full record) and prompt.md (analysis prompt + record)
into the extraction folder. Both contain PHI and stay gitignored.

If data/apple_health/summary.md exists (produced by parse_apple_health.py),
it is appended to the record.
"""

import html as html_mod
import json
import re
import sys
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def load(data_dir, name):
    p = data_dir / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else []


def cc_text(cc):
    if not cc:
        return ""
    return cc.get("text") or next(
        (c.get("display") for c in cc.get("coding", []) if c.get("display")), "")


def d10(iso):
    return (iso or "")[:10] or "?"


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
        return "; ".join(f"{cc_text(c.get('code'))} {obs_value(c)}" for c in o["component"])
    return ""


def ref_range(o):
    for r in o.get("referenceRange", []):
        if r.get("text"):
            return r["text"]
        low, high = r.get("low", {}).get("value"), r.get("high", {}).get("value")
        if low is not None and high is not None:
            return f"{low:g}-{high:g}"
        if high is not None:
            return f"<={high:g}"
        if low is not None:
            return f">={low:g}"
    return ""


def flag(o):
    for i in o.get("interpretation", []):
        t = cc_text(i)
        if t and t.lower() not in ("normal",):
            return t
    return ""


def html_to_text(raw):
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<br[^>]*>|</(p|div|tr|li|h[1-6])>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html_mod.unescape(txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r" ?\n ?", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def obs_category(o):
    for cat in o.get("category", []):
        for c in cat.get("coding", []):
            if c.get("code"):
                return c["code"]
    return "other"


def compile_record(data_dir):
    lines = []
    add = lines.append

    patient = (load(data_dir, "Patient") or [{}])[0]
    name = ""
    for n in patient.get("name", []):
        if n.get("use") == "official" or not name:
            name = f"{' '.join(n.get('given', []))} {n.get('family', '')}".strip()
    add(f"# Health record: {name}")
    add(f"- Born: {patient.get('birthDate', '?')} · Sex: {patient.get('gender', '?')}")
    gp = ", ".join(g.get("display", "") for g in patient.get("generalPractitioner", []) if g.get("display"))
    if gp:
        add(f"- Primary care: {gp}")
    add(f"- Source: University of Utah Healthcare via Epic FHIR patient-access API, "
        f"extracted {data_dir.name}")

    # Conditions
    conds = load(data_dir, "Condition")
    add(f"\n## Conditions ({len(conds)})")
    for c in sorted(conds, key=lambda x: x.get("onsetDateTime") or x.get("recordedDate") or "", reverse=True):
        status = cc_text(c.get("clinicalStatus"))
        cat = "problem-list" if any(
            cd.get("code") == "problem-list-item"
            for cat_ in c.get("category", []) for cd in cat_.get("coding", [])) else "encounter-dx"
        bits = [d10(c.get("onsetDateTime") or c.get("recordedDate")), cat]
        if status:
            bits.append(status)
        sev = cc_text(c.get("severity"))
        if sev:
            bits.append(sev)
        add(f"- {cc_text(c.get('code'))} ({', '.join(bits)})")

    # Medications
    meds = load(data_dir, "MedicationRequest")
    add(f"\n## Medications ({len(meds)})")
    for m in sorted(meds, key=lambda x: (x.get("status") != "active", x.get("authoredOn") or ""), ):
        nm = cc_text(m.get("medicationCodeableConcept")) or m.get(
            "medicationReference", {}).get("display", "?")
        dose = next((d.get("patientInstruction") or d.get("text", "")
                     for d in m.get("dosageInstruction", [])), "")
        reason = ", ".join(cc_text(r) for r in m.get("reasonCode", []) if cc_text(r))
        line = f"- [{m.get('status', '?')}] {nm}"
        if dose:
            line += f" — {dose}"
        if reason:
            line += f" (for: {reason})"
        add(line)

    # Allergies
    allergies = load(data_dir, "AllergyIntolerance")
    add(f"\n## Allergies ({len(allergies)})")
    for a in allergies:
        add(f"- {cc_text(a.get('code'))} ({cc_text(a.get('clinicalStatus'))}, "
            f"recorded {d10(a.get('recordedDate'))})")

    # Observations: labs / vitals / social — full time series grouped by test
    obs = load(data_dir, "Observation")
    for cat_code, title in (("laboratory", "Lab results"),
                            ("vital-signs", "Vital signs"),
                            ("social-history", "Social history")):
        items = [o for o in obs if obs_category(o) == cat_code]
        groups = {}
        for o in items:
            groups.setdefault(cc_text(o.get("code")) or "Unnamed", []).append(o)
        add(f"\n## {title} ({len(items)} results, {len(groups)} distinct)")
        for gname, gitems in sorted(groups.items(), key=lambda kv: max(
                i.get("effectiveDateTime") or "" for i in kv[1]), reverse=True):
            gitems.sort(key=lambda o: o.get("effectiveDateTime") or "")
            rr = next((ref_range(o) for o in reversed(gitems) if ref_range(o)), "")
            series = []
            for o in gitems:
                v = obs_value(o)
                f = flag(o)
                note = "; ".join(n.get("text", "") for n in o.get("note", []) if n.get("text"))
                entry = f"{d10(o.get('effectiveDateTime'))}: {v}"
                if f:
                    entry += f" [{f}]"
                if note and cat_code != "laboratory":
                    entry += f" ({note})"
                series.append(entry)
            line = f"- **{gname}**"
            if rr:
                line += f" (ref {rr})"
            add(line + ": " + " | ".join(series))

    # Immunizations (condensed)
    imms = load(data_dir, "Immunization")
    groups = {}
    for i in imms:
        groups.setdefault(cc_text(i.get("vaccineCode")) or "?", []).append(
            d10(i.get("occurrenceDateTime")))
    add(f"\n## Immunizations ({len(imms)})")
    for gname, dates in sorted(groups.items()):
        add(f"- {gname}: {', '.join(sorted(dates, reverse=True))}")

    # Diagnostic reports with resolved results
    reports = load(data_dir, "DiagnosticReport")
    obs_by_id = {o.get("id"): o for o in obs}
    add(f"\n## Diagnostic reports ({len(reports)})")
    for r in sorted(reports, key=lambda x: x.get("effectiveDateTime") or "", reverse=True):
        add(f"- **{cc_text(r.get('code'))}** ({d10(r.get('effectiveDateTime'))})")
        concl = ", ".join(cc_text(c) for c in r.get("conclusionCode", []) if cc_text(c))
        if concl:
            add(f"  - Conclusion: {concl}")
        for ref in r.get("result", []):
            o = obs_by_id.get((ref.get("reference") or "").split("/")[-1])
            if o:
                f = flag(o)
                rr = ref_range(o)
                add(f"  - {cc_text(o.get('code'))}: {obs_value(o)}"
                    + (f" [{f}]" if f else "") + (f" (ref {rr})" if rr else ""))

    # Encounters
    encs = load(data_dir, "Encounter")
    add(f"\n## Visits ({len(encs)})")
    for e in sorted(encs, key=lambda x: x.get("period", {}).get("start") or "", reverse=True):
        etype = ", ".join(cc_text(t) for t in e.get("type", []) if cc_text(t))
        reason = ", ".join(cc_text(r) for r in e.get("reasonCode", []) if cc_text(r))
        prov = next((p.get("individual", {}).get("display", "")
                     for p in e.get("participant", [])
                     if p.get("individual", {}).get("display")), "")
        line = f"- {d10(e.get('period', {}).get('start'))}: {etype or '?'}"
        if reason:
            line += f" — reason: {reason}"
        if prov:
            line += f" ({prov})"
        add(line)

    # Care plans & team
    plans = load(data_dir, "CarePlan")
    teams = load(data_dir, "CareTeam")
    if plans or teams:
        add(f"\n## Care plans & team")
        for p in plans:
            notes = "; ".join(n.get("text", "") for n in p.get("note", []) if n.get("text"))
            narrative = html_to_text(p.get("text", {}).get("div", ""))
            add(f"- Plan [{p.get('status')}]: "
                f"{', '.join(cc_text(c) for c in p.get('category', []) if cc_text(c))}"
                + (f" — {notes or narrative}" if (notes or narrative) else ""))
        for t in teams:
            for part in t.get("participant", []):
                member = part.get("member", {}).get("display", "")
                role = ", ".join(cc_text(r) for r in part.get("role", []) if cc_text(r))
                if member:
                    add(f"- Team [{t.get('status')}]: {member}" + (f" ({role})" if role else ""))

    # Coverage
    cov = load(data_dir, "Coverage")
    if cov:
        add(f"\n## Insurance coverage ({len(cov)})")
        for c in cov:
            payor = ", ".join(p.get("display", "") for p in c.get("payor", []) if p.get("display"))
            period = c.get("period", {})
            add(f"- {payor or cc_text(c.get('type')) or '?'} [{c.get('status', '?')}]"
                + (f" ({d10(period.get('start'))} – {d10(period.get('end'))})"
                   if period.get("start") else ""))

    # Clinical note bodies
    docs = load(data_dir, "DocumentReference")
    att_dir = data_dir / "attachments"
    add(f"\n## Clinical notes — full text ({len(docs)})")
    for d in sorted(docs, key=lambda x: x.get("date") or "", reverse=True):
        typ = cc_text(d.get("type"))
        authors = ", ".join(a.get("display", "") for a in d.get("author", []) if a.get("display"))
        add(f"\n### {typ} — {d10(d.get('date'))}" + (f" — {authors}" if authors else ""))
        body = ""
        for ext in ("html", "txt"):
            p = att_dir / f"DocumentReference_{d.get('id')}.{ext}"
            if p.exists():
                raw = p.read_text(errors="replace")
                body = html_to_text(raw) if ext == "html" else raw.strip()
                break
        if not body or len(body) < 5:
            body = "(no text content available)"
        if len(body) > 20000:
            body = body[:20000] + f"\n[… truncated, {len(body)} chars total]"
        add(body)

    # Report attachments (e.g. imaging narrative)
    for r in reports:
        p = att_dir / f"DiagnosticReport_{r.get('id')}.html"
        if p.exists():
            body = html_to_text(p.read_text(errors="replace"))
            if body:
                add(f"\n### Report narrative: {cc_text(r.get('code'))} — "
                    f"{d10(r.get('effectiveDateTime'))}")
                add(body[:20000])

    # Apple Health (optional, produced by parse_apple_health.py)
    ah = DATA_DIR / "apple_health" / "summary.md"
    if ah.exists():
        add("\n\n" + ah.read_text().strip())

    return "\n".join(lines) + "\n"


PROMPT = """\
# Role

You are a preventive-medicine physician and health coach reviewing a patient's
complete available health record (below): clinical data extracted from their
health system via FHIR{apple} . The patient is asking: **"Based on everything
here, what should I do to best improve my health?"**

# Instructions

1. **Current state.** Summarize their health status in plain language: active
   problems, medication picture, and notable trends in labs and vitals (cite
   the actual values and dates). Distinguish what is well-controlled from what
   is not.
2. **What stands out.** Identify the highest-signal items: abnormal or
   borderline results (especially trending the wrong way), gaps between
   conditions and treatment, anything in the notes that deserves follow-up,
   and relevant screening or immunizations that appear overdue given age/sex.
3. **Prioritized plan.** Give a ranked action list — most impactful first.
   For each: what to do, why (tie it to specific data), and how to measure
   progress. Separate (a) discuss-with-doctor items from (b) self-directed
   lifestyle changes.
4. **What's missing.** Name the data that would most improve this assessment.

Be direct and specific to THIS record — no generic advice that ignores the
data. Note where evidence is strong vs. suggestive. This is informational,
not a substitute for the patient's own clinicians.

---

{record}
"""


def main():
    if len(sys.argv) > 1:
        data_dir = Path(sys.argv[1])
    else:
        runs = sorted(d for d in DATA_DIR.iterdir()
                      if d.is_dir() and d.name != "apple_health")
        if not runs:
            sys.exit("No extractions found under data/.")
        data_dir = runs[-1]

    record = compile_record(data_dir)
    (data_dir / "record.md").write_text(record)
    apple = " and their Apple Health export" if (
        DATA_DIR / "apple_health" / "summary.md").exists() else ""
    (data_dir / "prompt.md").write_text(PROMPT.format(apple=apple, record=record))
    words = len(record.split())
    print(f"Wrote {(data_dir / 'record.md').resolve().as_uri()} ({words:,} words)")
    print(f"Wrote {(data_dir / 'prompt.md').resolve().as_uri()} (paste into any LLM)")


if __name__ == "__main__":
    main()
