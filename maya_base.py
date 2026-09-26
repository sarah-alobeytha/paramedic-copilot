"""Maya-only persistence. Calls teammates' functions without rewriting them."""
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone
from uuid import uuid4
from database_upgrade import upgrade, store_readings, read_measurements


def now():
    return datetime.now(timezone.utc).isoformat()


def load_team_modules(dania_path, vitals_path):
    """Explicit paths support downloaded filenames with (1). No file is edited."""
    def load(name, path):
        path = Path(path).resolve()
        existing = sys.modules.get(name)
        if existing:
            if Path(existing.__file__).resolve() != path:
                raise RuntimeError(f'{name} already loaded from a different file; start a fresh process.')
            return existing
        spec = importlib.util.spec_from_file_location(name, path)
        if not spec or not spec.loader:
            raise ValueError(f'Cannot load {path}')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module
    dania = load('dania_protocol_flow', dania_path)
    vitals = load('vitals_monitor', vitals_path)
    return dania, vitals


def sbar(snapshot):
    data = snapshot['data']
    match = snapshot.get('protocol') or {}
    show = lambda key: str(data.get(key) if data.get(key) is not None else 'unknown')
    lines = ['SBAR HANDOFF — SIMULATION', 'Case: '+snapshot['case_id'],
        'S — Situation', 'Symptoms: '+json.dumps(data.get('symptoms', []), ensure_ascii=False),
        'Life status: '+show('patient_status'), 'Consciousness: '+show('consciousness_status'),
        'B — Background', 'Age: '+show('age'), 'Gender: '+show('gender'),
        'Cause: '+show('cause_mentioned'), 'Duration: '+show('duration_mentioned'),
        'A — Reported assessment', 'Protocol: '+str(match.get('matched_protocol') or 'No supported match'),
        'Breathing: '+show('is_breathing'),
        'Source monitor deterioration flag: '+str(bool(data.get('deteriorating'))),
        'Flag uses the unchanged team monitor rule; it is not a validated clinical assessment.',
        'Vitals timeline (source timestamps retained as supplied):']
    for entry in snapshot['vitals']:
        lines.append(json.dumps(entry, ensure_ascii=False))
    lines.append('Protocol history:')
    for event in snapshot['events']:
        if event['kind'] == 'protocol':
            lines.append(event['recorded_at']+' '+json.dumps(event['payload'], ensure_ascii=False))
    lines += ['Reported interventions: Not collected by this adapter.', 'R — Handoff request',
        'Request: '+show('handoff_request'), 'Destination: '+show('destination'),
        'ETA minutes: '+show('eta_minutes'),
        'Draft suggested actions are not evidence of performed treatment.']
    return '\n'.join(lines)


class MayaAdapter:
    def __init__(self, db_path, dania, vitals):
        self.db_path = str(db_path)
        self.dania, self.monitor_module = dania, vitals
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, data_json TEXT NOT NULL,
                    protocol_json TEXT NOT NULL, sbar_report TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS vitals_log (
                    case_id TEXT NOT NULL REFERENCES cases(case_id),
                    sequence INTEGER NOT NULL, data_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL, PRIMARY KEY(case_id,sequence));
                CREATE TABLE IF NOT EXISTS case_events (
                    id INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(case_id),
                    kind TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at TEXT NOT NULL);
            ''')

        with self.connect() as db:
            upgrade(db, self.db_path)
            db.execute("PRAGMA journal_mode=WAL")

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        return db

    def start_case(self, data):
        if not isinstance(data, dict):
            raise ValueError('Intake must be a flat JSON object.')
        if data.get('vitals_timeline') or data.get('protocol_history'):
            raise ValueError('New intake must not contain an existing timeline/history.')
        cid = str(uuid4())
        with self.connect() as db:
            timestamp = now()
            db.execute('INSERT INTO cases VALUES(?,?,?,?,?,?)',
                (cid, timestamp, timestamp, json.dumps(data, ensure_ascii=False), '{}', ''))
            self._save(db, cid, copy.deepcopy(data), self.dania.process_case(data), 'intake')
        return self.get_case(cid)

    def _save(self, db, cid, data, protocol, kind):
        # Serialize before writes; invalid data cannot leave a partially written check.
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False)
        protocol_raw = json.dumps(protocol, ensure_ascii=False, allow_nan=False)
        old = db.execute('SELECT protocol_json FROM cases WHERE case_id=?', (cid,)).fetchone()
        if old is None:
            raise KeyError('Case not found')
        old_protocol = json.loads(old['protocol_json'])
        timestamp = now()
        for index, entry in enumerate(data.get('vitals_timeline', [])):
            payload = json.dumps(entry, ensure_ascii=False, allow_nan=False)
            previous = db.execute('SELECT data_json FROM vitals_log WHERE case_id=? AND sequence=?',
                                  (cid,index)).fetchone()
            if previous and previous['data_json'] != payload:
                raise ValueError('Previously saved vitals cannot be overwritten.')
            if previous is None:
                db.execute('INSERT INTO vitals_log VALUES(?,?,?,?)',(cid,index,payload,timestamp))
                store_readings(db,cid,index,entry,timestamp)
        stored_count = db.execute('SELECT COUNT(*) FROM vitals_log WHERE case_id=?',(cid,)).fetchone()[0]
        if stored_count != len(data.get('vitals_timeline', [])):
            raise ValueError('Previously saved vitals cannot be removed.')
        db.execute('UPDATE cases SET updated_at=?,data_json=?,protocol_json=? WHERE case_id=?',
                   (timestamp,raw,protocol_raw,cid))
        db.execute('INSERT INTO case_events(case_id,kind,payload_json,recorded_at) VALUES(?,?,?,?)',
                   (cid,kind,raw,timestamp))
        if old_protocol.get('matched_protocol') != protocol.get('matched_protocol') or kind == 'intake':
            payload = {'previous': old_protocol.get('matched_protocol'),
                       'matched_protocol': protocol.get('matched_protocol'), 'priority': protocol.get('priority')}
            db.execute('INSERT INTO case_events(case_id,kind,payload_json,recorded_at) VALUES(?,?,?,?)',
                       (cid,'protocol',json.dumps(payload),timestamp))
        report = sbar(self._snapshot(db,cid))
        db.execute('UPDATE cases SET sbar_report=? WHERE case_id=?',(report,cid))

    def _snapshot(self, db, cid):
        row = db.execute('SELECT * FROM cases WHERE case_id=?',(cid,)).fetchone()
        if row is None:
            raise KeyError('Case not found')
        result = dict(row)
        result['data'] = json.loads(result.pop('data_json'))
        result['protocol'] = json.loads(result.pop('protocol_json'))
        result['vitals'] = [json.loads(r['data_json']) for r in db.execute(
            'SELECT data_json FROM vitals_log WHERE case_id=? ORDER BY sequence',(cid,))]
        result['events'] = [{'kind':r['kind'], 'payload':json.loads(r['payload_json']),
            'recorded_at':r['recorded_at']} for r in db.execute(
            'SELECT * FROM case_events WHERE case_id=? ORDER BY id',(cid,))]
        result['structured_vitals'] = read_measurements(db,cid)
        return result

    def get_case(self, cid):
        with self.connect() as db:
            db.execute('BEGIN')
            return self._snapshot(db,cid)

    def run_followups(self, cid, ask_answer):
        case = self.get_case(cid)['data']
        result = self.dania.run_protocol_flow(case, ask_answer)
        with self.connect() as db:
            self._save(db,cid,result['data'],self.dania.process_case(result['data']),'followups')
        return result

    def monitor(self, cid, ask, speak, interval_s=120, max_checks=None):
        """One original monitor cycle per call; commit after it returns, then repeat."""
        if interval_s < 0 or (max_checks is not None and max_checks < 0):
            raise ValueError('Intervals/check counts must be nonnegative.')
        count = 0
        while max_checks is None or count < max_checks:
            snapshot = self.get_case(cid)
            case = snapshot['data']
            before = len(case.get('vitals_timeline', []))
            try:
                self.monitor_module.run_vitals_monitor(case, ask, speak,
                    interval_s=interval_s, max_checks=1)
            finally:
                if len(case.get('vitals_timeline', [])) > before:
                    # Save complete source output even if a later speak callback failed.
                    with self.connect() as db:
                        self._save(db,cid,case,self.dania.process_case(case),'monitor_check')
            if len(case.get('vitals_timeline', [])) == before:
                break  # Original monitor catches Ctrl+C before a complete check.
            count += 1
        return self.get_case(cid)
