#!/usr/bin/env python3
"""Load an Apple Health export.xml into a queryable SQLite database.

Usage:
    python3 apple_health_to_sqlite.py [export.xml] [health.db]

Defaults: data/apple_health/raw/apple_health_export/export.xml
       -> data/apple_health/health.db

Tables:
    records(type, day, start, end, value, value_num, unit, source)
    workouts(type, day, start, end, duration_min, kcal)
"""

import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_XML = DATA_DIR / "apple_health" / "raw" / "apple_health_export" / "export.xml"
DEFAULT_DB = DATA_DIR / "apple_health" / "health.db"
BATCH = 20000


def main():
    xml_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_XML
    db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DB
    if not xml_path.exists():
        sys.exit(f"Not found: {xml_path}")
    db_path.unlink(missing_ok=True)

    db = sqlite3.connect(db_path)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE records (
            type TEXT, day TEXT, start TEXT, end TEXT,
            value TEXT, value_num REAL, unit TEXT, source TEXT);
        CREATE TABLE workouts (
            type TEXT, day TEXT, start TEXT, end TEXT,
            duration_min REAL, kcal REAL);
    """)

    rec_batch, wo_batch, n = [], [], 0
    for _, elem in ET.iterparse(str(xml_path), events=("end",)):
        if elem.tag == "Record":
            n += 1
            rtype = (elem.get("type") or "").replace("HKQuantityTypeIdentifier", "") \
                .replace("HKCategoryTypeIdentifier", "") \
                .replace("HKDataType", "")
            value = elem.get("value")
            try:
                value_num = float(value)
            except (TypeError, ValueError):
                value_num = None
            start = elem.get("startDate", "")
            rec_batch.append((rtype, start[:10], start, elem.get("endDate", ""),
                              value, value_num, elem.get("unit"),
                              elem.get("sourceName")))
            if len(rec_batch) >= BATCH:
                db.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)", rec_batch)
                rec_batch.clear()
                if n % 500000 < BATCH:
                    print(f"  ...{n:,} records", file=sys.stderr)
        elif elem.tag == "Workout":
            wtype = (elem.get("workoutActivityType") or "").replace("HKWorkoutActivityType", "")
            kcal = None
            for st in elem.iter("WorkoutStatistics"):
                if st.get("type") == "HKQuantityTypeIdentifierActiveEnergyBurned":
                    try:
                        kcal = float(st.get("sum"))
                    except (TypeError, ValueError):
                        pass
            start = elem.get("startDate", "")
            try:
                dur = float(elem.get("duration", 0))
            except ValueError:
                dur = None
            wo_batch.append((wtype, start[:10], start, elem.get("endDate", ""), dur, kcal))
        if elem.tag in ("Record", "Workout", "Correlation"):
            elem.clear()

    if rec_batch:
        db.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)", rec_batch)
    if wo_batch:
        db.executemany("INSERT INTO workouts VALUES (?,?,?,?,?,?)", wo_batch)
    db.executescript("""
        CREATE INDEX idx_records_type_day ON records(type, day);
        CREATE INDEX idx_workouts_day ON workouts(day);
    """)
    db.commit()
    n_r = db.execute("SELECT count(*) FROM records").fetchone()[0]
    n_w = db.execute("SELECT count(*) FROM workouts").fetchone()[0]
    db.close()
    print(f"Loaded {n_r:,} records + {n_w:,} workouts -> {db_path.resolve().as_uri()}")


if __name__ == "__main__":
    main()
