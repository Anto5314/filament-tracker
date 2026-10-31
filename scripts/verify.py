#!/usr/bin/env python3
"""Offline smoke checks for Filament Tracker's source and additive DB schema."""
from pathlib import Path
import os
import sqlite3
import tempfile

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
required = ("session-search", "session-status", "stock-alerts", "spool-alerts", "/api/summary",
           "@media print", "Coût absent (bobine non associée)", "Coût estimatif indisponible",
           "sessionDuration", "spoolPricePerGram")
missing = [marker for marker in required if marker not in html]
if missing:
    raise SystemExit(f"missing UI markers: {', '.join(missing)}")

fd, db_path = tempfile.mkstemp(prefix="filament-tracker-", suffix=".db")
os.close(fd)
os.environ["DB_PATH"] = db_path
try:
    import sys
    sys.path.insert(0, str(ROOT))
    import collector
    collector.init_db()
    collector.add_session("verify", "verify.gcode", "completed", "2026-01-01T00:00:00+00:00")
    collector.init_db()
    with sqlite3.connect(db_path) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
        rows = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert rows == 1 and {"id", "file_name", "status", "thumb"} <= columns
    assert "filament_cost" in columns
    assert collector.estimate_filament_cost(100, {"filament": {"price": 20, "weight": 1000}}) == 2
    assert collector.estimate_filament_cost(100, {"filament": {"price": 20}}) is None
    assert collector.estimate_filament_cost(100, {"filament": {"price_per_kg": 20}}) == 2

    collector.add_session("known", "known.gcode", "completed", "2026-01-02T00:00:00+00:00")
    collector.update_session("known", end_time="2026-01-02T01:00:00+00:00",
                             filament_grams=100, filament_cost=2.0)
    collector.add_session("unknown", "unknown.gcode", "completed", "2026-01-02T00:00:00+00:00")
    collector.update_session("unknown", end_time="2026-01-02T00:30:00+00:00",
                             filament_grams=50)
    summary = collector.summary_stats()
    assert summary["total"] == 3 and summary["grams"] == 150
    assert summary["cost_total"] == 2.0 and summary["cost_known_prints"] == 1
    assert summary["avg_cost"] == 2.0 and summary["duration_seconds"] == 5400
    assert summary["avg_duration_seconds"] == 2700
    stats = collector.monthly_stats()
    month = stats["months"][0]
    assert month["cost_total"] == 2.0 and month["cost_known_prints"] == 1
    assert month["avg_cost"] == 2.0 and month["seconds"] == 5400
    assert stats["total"]["cost_total"] == 2.0
finally:
    Path(db_path).unlink(missing_ok=True)
print("Filament Tracker verification: OK")
