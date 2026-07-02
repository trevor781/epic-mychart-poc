#!/usr/bin/env python3
"""Distill an Apple Health export into a compact markdown summary.

Usage:
    python3 parse_apple_health.py [path/to/export.xml]

Defaults to data/apple_health/raw/apple_health_export/export.xml.
Writes data/apple_health/summary.md (monthly trends, workout load, sleep).
Streams the XML — handles multi-GB exports.
"""

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_XML = DATA_DIR / "apple_health" / "raw" / "apple_health_export" / "export.xml"

# point-in-time metrics -> monthly mean of values
POINT_METRICS = {
    "HKQuantityTypeIdentifierBodyMass": ("Weight", 1),
    "HKQuantityTypeIdentifierBodyFatPercentage": ("Body fat %", 100),
    "HKQuantityTypeIdentifierLeanBodyMass": ("Lean mass", 1),
    "HKQuantityTypeIdentifierRestingHeartRate": ("Resting HR", 1),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": ("HRV (SDNN ms)", 1),
    "HKQuantityTypeIdentifierVO2Max": ("VO2 max", 1),
    "HKQuantityTypeIdentifierOxygenSaturation": ("SpO2 %", 100),
    "HKQuantityTypeIdentifierRespiratoryRate": ("Respiratory rate", 1),
    "HKQuantityTypeIdentifierBloodPressureSystolic": ("BP systolic", 1),
    "HKQuantityTypeIdentifierBloodPressureDiastolic": ("BP diastolic", 1),
}
# cumulative metrics -> per-day sum (deduped by source: max source-total per day)
DAILY_METRICS = {
    "HKQuantityTypeIdentifierStepCount": ("Steps/day", 1),
    "HKQuantityTypeIdentifierAppleExerciseTime": ("Exercise min/day", 1),
    "HKQuantityTypeIdentifierActiveEnergyBurned": ("Active kcal/day", 1),
    "HKQuantityTypeIdentifierTimeInDaylight": ("Daylight min/day", 1),
}
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")


def mean(xs):
    return sum(xs) / len(xs) if xs else 0


def monthly_series(by_month, scale=1, fmt="{:.1f}", recent=24):
    months = sorted(by_month)
    if not months:
        return "", ""
    recent_m = months[-recent:]
    older_m = months[:-recent] if len(months) > recent else []
    parts = [f"{m}: {fmt.format(mean(by_month[m]) * scale)}" for m in recent_m]
    yearly = defaultdict(list)
    for m in older_m:
        yearly[m[:4]].extend(by_month[m])
    older = [f"{y} avg: {fmt.format(mean(v) * scale)}" for y, v in sorted(yearly.items())]
    return " | ".join(older), " | ".join(parts)


def main():
    xml_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_XML
    if not xml_path.exists():
        sys.exit(f"Not found: {xml_path}")

    point = defaultdict(lambda: defaultdict(list))       # type -> month -> [values]
    units = {}
    daily = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))  # type -> day -> source -> sum
    sleep_night = defaultdict(lambda: defaultdict(float))  # night -> source -> hours
    workouts = []
    first_date = last_date = None
    export_date = ""

    n = 0
    for _, elem in ET.iterparse(str(xml_path), events=("end",)):
        tag = elem.tag
        if tag == "ExportDate":
            export_date = elem.get("value", "")[:10]
        elif tag == "Record":
            n += 1
            rtype = elem.get("type", "")
            start = elem.get("startDate", "")
            day, month = start[:10], start[:7]
            if day:
                if first_date is None or day < first_date:
                    first_date = day
                if last_date is None or day > last_date:
                    last_date = day
            if rtype in POINT_METRICS:
                try:
                    point[rtype][month].append(float(elem.get("value")))
                    units.setdefault(rtype, elem.get("unit", ""))
                except (TypeError, ValueError):
                    pass
            elif rtype in DAILY_METRICS:
                try:
                    daily[rtype][day][elem.get("sourceName", "?")] += float(elem.get("value"))
                except (TypeError, ValueError):
                    pass
            elif rtype == SLEEP and "Asleep" in (elem.get("value") or ""):
                try:
                    s, e = parse_ts(elem.get("startDate")), parse_ts(elem.get("endDate"))
                    night = e.strftime("%Y-%m-%d")  # attribute night to wake date
                    sleep_night[night][elem.get("sourceName", "?")] += (e - s).total_seconds() / 3600
                except (TypeError, ValueError):
                    pass
        elif tag == "Workout":
            try:
                wtype = elem.get("workoutActivityType", "").replace("HKWorkoutActivityType", "")
                dur = float(elem.get("duration", 0))  # minutes
                start = elem.get("startDate", "")
                kcal = 0
                for st in elem.iter("WorkoutStatistics"):
                    if st.get("type") == "HKQuantityTypeIdentifierActiveEnergyBurned":
                        kcal = float(st.get("sum", 0))
                workouts.append((start[:10], start[:7], wtype, dur, kcal))
            except (TypeError, ValueError):
                pass
        if tag in ("Record", "Workout", "Correlation"):
            elem.clear()
        if n % 500000 == 0 and n:
            print(f"  ...{n:,} records", file=sys.stderr)

    lines = [f"# Apple Health summary",
             f"- Export date: {export_date}; data coverage {first_date} → {last_date}"]

    lines.append("\n## Body & cardio metrics (monthly averages; older years condensed)")
    for rtype, (label, scale) in POINT_METRICS.items():
        if not point[rtype]:
            continue
        unit = units.get(rtype, "")
        unit = "" if scale != 1 else (f" {unit}" if unit else "")
        older, recent = monthly_series(point[rtype], scale)
        lines.append(f"- **{label}{unit}**: " + (f"[{older}] " if older else "") + recent)

    lines.append("\n## Daily activity (monthly average per day; deduped across devices)")
    for rtype, (label, _) in DAILY_METRICS.items():
        if not daily[rtype]:
            continue
        by_month = defaultdict(list)
        for day, sources in daily[rtype].items():
            by_month[day[:7]].append(max(sources.values()))
        older, recent = monthly_series(by_month, fmt="{:,.0f}")
        lines.append(f"- **{label}**: " + (f"[{older}] " if older else "") + recent)

    if sleep_night:
        by_month = defaultdict(list)
        for night, sources in sleep_night.items():
            hours = max(sources.values())
            if 1 <= hours <= 16:
                by_month[night[:7]].append(hours)
        older, recent = monthly_series(by_month)
        lines.append("\n## Sleep (avg hours asleep per night, monthly)")
        lines.append("- " + (f"[{older}] " if older else "") + recent)

    if workouts:
        lines.append(f"\n## Workouts ({len(workouts)} total)")
        by_type = defaultdict(lambda: [0, 0.0])
        for _, _, wtype, dur, _ in workouts:
            by_type[wtype][0] += 1
            by_type[wtype][1] += dur / 60
        lines.append("- Lifetime by type: " + " | ".join(
            f"{t}: {c}x, {h:,.0f}h" for t, (c, h) in
            sorted(by_type.items(), key=lambda kv: -kv[1][1])))
        by_month = defaultdict(lambda: [0, 0.0, 0.0])
        for _, month, _, dur, kcal in workouts:
            by_month[month][0] += 1
            by_month[month][1] += dur / 60
            by_month[month][2] += kcal
        months = sorted(by_month)[-24:]
        lines.append("- Monthly training load (last 24 mo): " + " | ".join(
            f"{m}: {c} workouts, {h:.1f}h, {k:,.0f} kcal"
            for m, (c, h, k) in ((m, by_month[m]) for m in months)))
        recent = sorted(workouts, reverse=True)[:10]
        lines.append("- Most recent workouts: " + " | ".join(
            f"{d} {t} {dur:.0f}min" for d, _, t, dur, _ in recent))

    ecg_dir = xml_path.parent / "electrocardiograms"
    if ecg_dir.exists():
        ecgs = sorted(p.stem.replace("ecg_", "") for p in ecg_dir.glob("*.csv"))
        if ecgs:
            lines.append(f"\n## ECGs recorded: {', '.join(ecgs)}")

    out = DATA_DIR / "apple_health" / "summary.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"Parsed {n:,} records. Wrote {out.resolve().as_uri()}")


if __name__ == "__main__":
    main()
