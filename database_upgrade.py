"""Additive SQLite migration and conservative vital-value normalization.

Raw source values are never rewritten. 'recorded' means parseable, not clinically
validated. No clinical thresholds or treatment decisions are introduced here.
"""
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

FIELDS = {'blood_pressure': 'mmHg', 'heart_rate': 'bpm',
          'temperature': '°C', 'oxygen_saturation': '%'}
MIGRATION = 1
UNKNOWN = {'', 'unknown', 'غير معروف', 'غير معروفة'}
NOT_MEASURED = {'not measured', 'not_measured', 'لم يتم القياس'}
DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫', '01234567890123456789.')


def parse_reading(field, value):
    status, first, second, note = 'recorded', None, None, None
    if value is None or (isinstance(value, str) and value.strip().lower() in UNKNOWN):
        return {'status': 'unknown', 'value': None, 'value_secondary': None, 'note': None}
    if isinstance(value, str) and value.strip().lower() in NOT_MEASURED:
        return {'status': 'not_measured', 'value': None, 'value_secondary': None, 'note': None}
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError('Unsupported format')
        text = str(value).strip().translate(DIGITS)
        suffix = {'blood_pressure': r'\s*mmhg$', 'heart_rate': r'\s*bpm$',
                  'temperature': r'\s*(?:°\s*c|c)$', 'oxygen_saturation': r'\s*%$'}[field]
        text = re.sub(suffix, '', text, flags=re.I).strip()
        if field == 'blood_pressure':
            if not re.fullmatch(r'\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?', text):
                raise ValueError('Expected systolic/diastolic')
            first, second = map(float, text.split('/'))
            if first <= 0 or second <= 0 or first <= second:
                raise ValueError('Verify blood pressure values/order')
        else:
            first = float(text)
            if not math.isfinite(first): raise ValueError('Non-finite value')
            if field == 'oxygen_saturation' and not 0 <= first <= 100:
                raise ValueError('Percentage outside 0–100')
            if field == 'heart_rate' and first < 0: raise ValueError('Negative heart rate')
        if not math.isfinite(first) or (second is not None and not math.isfinite(second)):
            raise ValueError('Non-finite value')
    except (ValueError, TypeError, OverflowError) as exc:
        status, first, second, note = 'needs_confirmation', None, None, str(exc)
    return {'status': status, 'value': first, 'value_secondary': second, 'note': note}


def time_status(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return 'timezone_known' if parsed.tzinfo else 'timezone_unspecified'
    except (ValueError, TypeError, AttributeError):
        return 'unknown' if value in (None, '') else 'invalid'


def store_readings(db, cid, sequence, entry, recorded_at):
    for field, unit in FIELDS.items():
        raw = entry.get(field)
        parsed = parse_reading(field, raw)
        if field not in entry: parsed['status'] = 'not_measured'
        db.execute('''INSERT OR IGNORE INTO vital_measurements
          (case_id,sequence,field,value,value_secondary,unit,status,note,raw_json,source_time_json,time_status,recorded_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
          (cid, sequence, field, parsed['value'], parsed['value_secondary'], unit,
           parsed['status'], parsed['note'], json.dumps(raw, ensure_ascii=False),
           json.dumps(entry.get('timestamp'), ensure_ascii=False), time_status(entry.get('timestamp')), recorded_at))


def upgrade(db, db_path):
    """Back up existing case databases, then atomically add/backfill version 1."""
    table = db.execute("SELECT 1 FROM sqlite_master WHERE name='maya_schema_migrations'").fetchone()
    if table:
        latest = db.execute('SELECT MAX(version) FROM maya_schema_migrations').fetchone()[0]
        if latest is not None and latest > MIGRATION:
            raise RuntimeError('Database schema is newer than this code')
        if latest == MIGRATION: return
    backup = None
    if db.execute('SELECT 1 FROM cases LIMIT 1').fetchone():
        backup = str(Path(db_path).resolve()) + '.before-vitals-v1-' + uuid4().hex + '.bak'
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS maya_schema_migrations
          (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, backup_path TEXT)''')
        if db.execute('SELECT 1 FROM maya_schema_migrations WHERE version=?', (MIGRATION,)).fetchone():
            db.rollback()
            return
        db.execute('''CREATE TABLE vital_measurements (
          case_id TEXT NOT NULL, sequence INTEGER NOT NULL,
          field TEXT NOT NULL CHECK(field IN ('blood_pressure','heart_rate','temperature','oxygen_saturation')),
          value REAL, value_secondary REAL, unit TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('recorded','unknown','not_measured','needs_confirmation')),
          note TEXT, raw_json TEXT NOT NULL, source_time_json TEXT NOT NULL,
          time_status TEXT NOT NULL, recorded_at TEXT NOT NULL,
          PRIMARY KEY(case_id,sequence,field),
          FOREIGN KEY(case_id,sequence) REFERENCES vitals_log(case_id,sequence),
          CHECK((status='recorded' AND value IS NOT NULL) OR (status!='recorded' AND value IS NULL AND value_secondary IS NULL)))''')
        db.execute('CREATE INDEX IF NOT EXISTS events_by_case_kind ON case_events(case_id,kind,id)')
        db.execute('CREATE INDEX IF NOT EXISTS cases_by_created ON cases(created_at DESC)')
        db.execute('CREATE INDEX IF NOT EXISTS measurements_by_field ON vital_measurements(case_id,field,sequence DESC)')
        for row in db.execute('SELECT * FROM vitals_log').fetchall():
            entry = json.loads(row['data_json'])
            if not isinstance(entry, dict): raise ValueError('Legacy vital entry must be an object')
            store_readings(db, row['case_id'], row['sequence'], entry, row['recorded_at'])
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Existing database has broken references; migration rolled back')
        db.execute('INSERT INTO maya_schema_migrations VALUES(?,?,?)',
                   (MIGRATION, datetime.now(timezone.utc).isoformat(), backup))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def read_measurements(db, cid):
    output = []
    for row in db.execute('SELECT * FROM vital_measurements WHERE case_id=? ORDER BY sequence,field', (cid,)):
        item = dict(row)
        item['raw_value'] = json.loads(item.pop('raw_json'))
        item['source_time'] = json.loads(item.pop('source_time_json'))
        output.append(item)
    return output
