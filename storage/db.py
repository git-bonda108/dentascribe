"""SQLite schema + access for DentaScribe.

Tables:
  encounters   — one row per visit
  transcripts  — full diarized transcript JSON per encounter
  soap_notes   — current + historical SOAP JSON
  audit_log    — every agent LLM call (model, tokens, latency, prompt hash)
  attestations — provider sign-off events
  exports      — exported PDF / DOCX / JSON files produced for an encounter

Design notes:
- All JSON columns store TEXT. The wrapper handles json.dumps/loads.
- `created_at` everywhere as ISO8601 UTC.
- Soft delete on encounters via `deleted_at` + retention sweep in storage/retention.py.
- Postgres migration path: keep all column names ANSI; avoid SQLite-specific types.
- DO NOT store PHI here without disk-level encryption. See README HIPAA note.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path("data/dentascribe.db")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS encounters (
    encounter_id     TEXT PRIMARY KEY,
    patient_id       TEXT NOT NULL,
    provider_name    TEXT,
    provider_license TEXT,
    visit_type       TEXT,
    date_of_service  TEXT,
    clinic_city      TEXT DEFAULT 'Dallas',
    clinic_state     TEXT DEFAULT 'TX',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    deleted_at       TEXT,
    is_minor         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id    TEXT PRIMARY KEY,
    encounter_id     TEXT NOT NULL,
    source           TEXT NOT NULL,      -- 'deepgram_file' | 'deepgram_live' | 'demo_paste'
    transcript_json  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    FOREIGN KEY(encounter_id) REFERENCES encounters(encounter_id)
);

CREATE TABLE IF NOT EXISTS soap_notes (
    soap_id          TEXT PRIMARY KEY,
    encounter_id     TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    soap_json        TEXT NOT NULL,
    validation_json  TEXT,
    signability_score INTEGER,
    created_at       TEXT NOT NULL,
    is_current       INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(encounter_id) REFERENCES encounters(encounter_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id         TEXT PRIMARY KEY,
    encounter_id     TEXT,
    run_id           TEXT,
    agent            TEXT NOT NULL,
    model            TEXT,
    status           TEXT,
    llm_status       TEXT,
    prompt_hash      TEXT,
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    latency_ms       INTEGER DEFAULT 0,
    duration_ms      INTEGER DEFAULT 0,
    status_message   TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attestations (
    attestation_id   TEXT PRIMARY KEY,
    encounter_id     TEXT NOT NULL,
    soap_id          TEXT NOT NULL,
    provider_name    TEXT NOT NULL,
    provider_license TEXT NOT NULL,
    signed_at        TEXT NOT NULL,
    signed_text      TEXT NOT NULL,
    signature_hash   TEXT NOT NULL,
    FOREIGN KEY(encounter_id) REFERENCES encounters(encounter_id),
    FOREIGN KEY(soap_id)      REFERENCES soap_notes(soap_id)
);

CREATE TABLE IF NOT EXISTS exports (
    export_id        TEXT PRIMARY KEY,
    encounter_id     TEXT NOT NULL,
    soap_id          TEXT NOT NULL,
    format           TEXT NOT NULL,     -- 'pdf' | 'docx' | 'json'
    file_path        TEXT NOT NULL,
    sha256           TEXT,
    created_at       TEXT NOT NULL,
    FOREIGN KEY(encounter_id) REFERENCES encounters(encounter_id)
);

CREATE INDEX IF NOT EXISTS idx_encounters_dos     ON encounters(date_of_service);
CREATE INDEX IF NOT EXISTS idx_audit_encounter    ON audit_log(encounter_id);
CREATE INDEX IF NOT EXISTS idx_soap_encounter     ON soap_notes(encounter_id);
CREATE INDEX IF NOT EXISTS idx_soap_current       ON soap_notes(encounter_id, is_current);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: str | Path = DEFAULT_DB_PATH):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


# ---------- encounter ----------

def create_encounter(
    *, patient_id: str, provider_name: str, provider_license: str,
    visit_type: str, date_of_service: str,
    clinic_city: str = "Dallas", clinic_state: str = "TX",
    is_minor: bool = False, db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    eid = str(uuid.uuid4())
    now = _now()
    with connect(db_path) as conn:
        conn.execute("""
            INSERT INTO encounters(encounter_id, patient_id, provider_name, provider_license,
                                   visit_type, date_of_service, clinic_city, clinic_state,
                                   created_at, updated_at, is_minor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, patient_id, provider_name, provider_license, visit_type,
             date_of_service, clinic_city, clinic_state, now, now, int(is_minor)),
        )
    return eid


def list_encounters(*, include_deleted: bool = False, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict]:
    q = "SELECT * FROM encounters"
    if not include_deleted:
        q += " WHERE deleted_at IS NULL"
    q += " ORDER BY date_of_service DESC, created_at DESC"
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(q).fetchall()]


def soft_delete_encounter(encounter_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE encounters SET deleted_at = ?, updated_at = ? WHERE encounter_id = ?",
            (_now(), _now(), encounter_id),
        )


# ---------- transcript ----------

def save_transcript(encounter_id: str, transcript: dict, *, source: str = "demo_paste",
                    db_path: str | Path = DEFAULT_DB_PATH) -> str:
    tid = str(uuid.uuid4())
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO transcripts(transcript_id, encounter_id, source, transcript_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, encounter_id, source, json.dumps(transcript), _now()),
        )
    return tid


def get_latest_transcript(encounter_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT transcript_json FROM transcripts WHERE encounter_id = ? "
            "ORDER BY created_at DESC LIMIT 1", (encounter_id,),
        ).fetchone()
        return json.loads(row["transcript_json"]) if row else None


# ---------- SOAP ----------

def save_soap_note(encounter_id: str, soap: dict, validation: dict | None = None,
                   signability_score: int | None = None,
                   db_path: str | Path = DEFAULT_DB_PATH) -> str:
    sid = str(uuid.uuid4())
    with connect(db_path) as conn:
        prev_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM soap_notes WHERE encounter_id = ?",
            (encounter_id,)).fetchone()["v"]
        conn.execute("UPDATE soap_notes SET is_current = 0 WHERE encounter_id = ?", (encounter_id,))
        conn.execute(
            "INSERT INTO soap_notes(soap_id, encounter_id, version, soap_json, validation_json, "
            "signability_score, created_at, is_current) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (sid, encounter_id, prev_version + 1, json.dumps(soap),
             json.dumps(validation) if validation else None,
             signability_score, _now()),
        )
        conn.execute("UPDATE encounters SET updated_at = ? WHERE encounter_id = ?",
                     (_now(), encounter_id))
    return sid


def get_current_soap(encounter_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM soap_notes WHERE encounter_id = ? AND is_current = 1 LIMIT 1",
            (encounter_id,)).fetchone()
        if not row:
            return None
        return {
            "soap_id": row["soap_id"], "version": row["version"],
            "soap": json.loads(row["soap_json"]),
            "validation": json.loads(row["validation_json"]) if row["validation_json"] else None,
            "signability_score": row["signability_score"],
            "created_at": row["created_at"],
        }


# ---------- audit ----------

def append_audit_records(records: list[dict], encounter_id: str | None = None,
                         db_path: str | Path = DEFAULT_DB_PATH) -> None:
    if not records:
        return
    with connect(db_path) as conn:
        for r in records:
            conn.execute(
                "INSERT INTO audit_log(audit_id, encounter_id, run_id, agent, model, status, "
                "llm_status, prompt_hash, input_tokens, output_tokens, latency_ms, duration_ms, "
                "status_message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), encounter_id, r.get("run_id"), r.get("agent"),
                 r.get("model"), r.get("status"), r.get("llm_status"), r.get("prompt_hash"),
                 r.get("input_tokens", 0), r.get("output_tokens", 0),
                 r.get("latency_ms", 0), r.get("duration_ms", 0),
                 r.get("status_message"), _now()),
            )


def audit_log_for_encounter(encounter_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE encounter_id = ? ORDER BY created_at ASC",
            (encounter_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------- attestation ----------

def save_attestation(*, encounter_id: str, soap_id: str, provider_name: str,
                     provider_license: str, signed_text: str,
                     db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    import hashlib
    aid = str(uuid.uuid4())
    now = _now()
    sig_hash = hashlib.sha256(
        f"{soap_id}|{provider_license}|{signed_text}|{now}".encode("utf-8")
    ).hexdigest()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO attestations(attestation_id, encounter_id, soap_id, provider_name, "
            "provider_license, signed_at, signed_text, signature_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, encounter_id, soap_id, provider_name, provider_license,
             now, signed_text, sig_hash),
        )
    return {"attestation_id": aid, "signed_at": now, "signature_hash": sig_hash}


def get_attestation(encounter_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM attestations WHERE encounter_id = ? ORDER BY signed_at DESC LIMIT 1",
            (encounter_id,)).fetchone()
        return dict(row) if row else None


# ---------- exports ----------

def record_export(*, encounter_id: str, soap_id: str, format: str, file_path: str,
                  sha256: str | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> str:
    eid = str(uuid.uuid4())
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO exports(export_id, encounter_id, soap_id, format, file_path, sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, encounter_id, soap_id, format, file_path, sha256, _now()),
        )
    return eid
