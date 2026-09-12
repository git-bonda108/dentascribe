"""Texas-aware record retention sweep.

22 TAC §108.8 in Texas requires:
  - Adult patient records: retained for at least 5 years from last treatment.
  - Minor records: retained until the patient turns 21 (age of majority 18 + 3),
    and at least 5 years after the last treatment.

This module exposes two functions:
  - `flag_due_for_purge(db_path)` — returns a list of encounter_ids past retention.
    DOES NOT delete anything. Surfaces a list for the Admin page.
  - `purge_flagged(encounter_ids, db_path, dry_run=True)` — hard-deletes encounters
    confirmed by the admin. Dry-run by default; pass `dry_run=False` to commit.

We are deliberately conservative — purge is two-step, never automatic.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from storage.db import DEFAULT_DB_PATH, connect

ADULT_RETENTION_YEARS = 5
MINOR_AGE_OF_MAJORITY_YEARS = 21
MINOR_RETENTION_YEARS = 5


def _parse_date(s: str | None) -> datetime | None:
    """Parse an ISO-ish date string and ALWAYS return an offset-aware datetime
    (UTC). Subtractions against `datetime.now(timezone.utc)` would otherwise
    raise on plain 'YYYY-MM-DD' inputs in Python 3.11+, which return naive.
    """
    if not s:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            parsed = datetime.fromisoformat(s + "T00:00:00+00:00")
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def flag_due_for_purge(*, db_path: str | Path = DEFAULT_DB_PATH,
                      now: datetime | None = None) -> list[dict]:
    """Returns encounters that are past their retention window."""
    now = now or datetime.now(timezone.utc)
    due: list[dict] = []
    with connect(db_path) as conn:
        rows = conn.execute("""
            SELECT e.encounter_id, e.patient_id, e.date_of_service, e.is_minor,
                   (SELECT MAX(date_of_service) FROM encounters
                    WHERE patient_id = e.patient_id) AS last_dos
            FROM encounters e WHERE e.deleted_at IS NULL
        """).fetchall()

        # Also try to derive patient DOB from any SOAP we have on file
        for r in rows:
            last_dos = _parse_date(r["last_dos"]) or _parse_date(r["date_of_service"])
            if last_dos is None:
                continue
            years_since = (now - last_dos).days / 365.25
            patient_dob = _latest_patient_dob(conn, r["patient_id"])

            if r["is_minor"] and patient_dob:
                age_now = (now - patient_dob).days / 365.25
                if age_now >= MINOR_AGE_OF_MAJORITY_YEARS and years_since >= MINOR_RETENTION_YEARS:
                    due.append({**dict(r), "reason": "minor_majority_plus_5"})
            else:
                if years_since >= ADULT_RETENTION_YEARS:
                    due.append({**dict(r), "reason": "adult_5_years"})
    return due


def _latest_patient_dob(conn: sqlite3.Connection, patient_id: str) -> datetime | None:
    row = conn.execute("""
        SELECT s.soap_json FROM soap_notes s
        JOIN encounters e ON e.encounter_id = s.encounter_id
        WHERE e.patient_id = ? ORDER BY s.created_at DESC LIMIT 1
    """, (patient_id,)).fetchone()
    if not row:
        return None
    try:
        soap = json.loads(row["soap_json"])
        dob = soap.get("metadata", {}).get("patient", {}).get("dob")
        return _parse_date(dob)
    except Exception:
        return None


def purge_flagged(encounter_ids: list[str], *, dry_run: bool = True,
                  db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """Hard-deletes the given encounters (and dependent rows) after admin confirm."""
    if not encounter_ids:
        return {"dry_run": dry_run, "deleted": 0}
    placeholders = ",".join(["?"] * len(encounter_ids))
    if dry_run:
        with connect(db_path) as conn:
            n = conn.execute(
                f"SELECT COUNT(*) AS c FROM encounters WHERE encounter_id IN ({placeholders})",
                encounter_ids).fetchone()["c"]
        return {"dry_run": True, "would_delete": n, "ids": encounter_ids}

    with connect(db_path) as conn:
        for table in ["exports", "attestations", "audit_log", "soap_notes", "transcripts"]:
            conn.execute(f"DELETE FROM {table} WHERE encounter_id IN ({placeholders})",
                         encounter_ids)
        conn.execute(f"DELETE FROM encounters WHERE encounter_id IN ({placeholders})",
                     encounter_ids)
    return {"dry_run": False, "deleted": len(encounter_ids), "ids": encounter_ids}
