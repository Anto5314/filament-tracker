#!/usr/bin/env python3
"""Offline smoke checks for Filament Tracker's source and additive DB schema."""
from pathlib import Path
import os
import sqlite3
import tempfile

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
required = ("session-search", "session-status", "stock-alerts", "spool-alerts", "/api/summary", "@media print")
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
finally:
    Path(db_path).unlink(missing_ok=True)
print("Filament Tracker verification: OK")
