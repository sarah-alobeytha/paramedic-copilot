"""Maya's transactional update layer. Teammate modules remain untouched."""
import copy
import json
import math
import re
from datetime import datetime
from uuid import uuid4
from database_upgrade import parse_reading, FIELDS
from maya_base import MayaAdapter as Base, load_team_modules, now


class Conflict(ValueError):
    pass


def validate(data):
    if not isinstance(data,dict): raise ValueError('Expected an object')
    json.dumps(data,allow_nan=False)
    if 'symptoms' in data and (not isinstance(data['symptoms'],list) or any(not isinstance(s,str) for s in data['symptoms'])):
        raise ValueError('symptoms must be a list of strings')
    for field,allowed in {'patient_status':['alive','deceased','unknown'],
        'is_breathing':['yes','no','unknown',None], 'consciousness_status':['conscious','unconscious','unknown',None]}.items():
        if field in data and data[field] not in allowed: raise ValueError('Invalid '+field)
    if len(json.dumps(data))>500000:raise ValueError('Case too large')


def readable(s):
    d=s['data'];unknown=lambda x:'Unknown' if x is None or x=='' or x=='unknown' else str(x)
    lines=['SBAR HANDOFF — SIMULATION',f"Case: {s['case_id']} | Version: {s['version']} | Status: {s['status']}",
      'Updated: '+s['updated_at'],'','S — Situation',
      'Reported symptoms: '+(', '.join(d.get('symptoms',[])) or 'Unknown'),
      'Life status: '+unknown(d.get('patient_status')),
      'Consciousness: '+unknown(d.get('consciousness_status')),
      'Breathing: '+unknown(d.get('is_breathing')),'','B — Background',
      'Age: '+unknown(d.get('age'))+' | Gender: '+unknown(d.get('gender')),
      'Cause: '+unknown(d.get('cause_mentioned')),'Duration: '+unknown(d.get('duration_mentioned')),
      'History: '+unknown(d.get('history')),'','A — Recorded assessment',
      'Protocol: '+unknown(s['protocol'].get('matched_protocol')),
      'Source-generated deterioration flag: '+str(bool(d.get('deteriorating'))),
      'Responder confirmation: '+s['confirmation']['value'],
      'Confirmation note: '+unknown(s['confirmation'].get('note')),
      'Source flag and responder confirmation are separate; neither is a validated diagnosis.','Latest reported readings:']
    fields={'blood_pressure':('Blood pressure','mmHg'),'heart_rate':('Heart rate','bpm'),
            'temperature':('Temperature','°C'),'oxygen_saturation':('Oxygen saturation','%')}
    measurements={(v['sequence'],v['field']):v for v in s.get('structured_vitals',[])}
    def display(index,field):
        m=measurements.get((index,field))
        if m is None: return 'Unknown'
        if m['status']!='recorded':
            return {'unknown':'Unknown','not_measured':'Not measured',
                    'needs_confirmation':'Needs confirmation (raw: '+str(m['raw_value'])+')'}[m['status']]
        value=format(m['value'],'g')
        if m['value_secondary'] is not None:value+='/'+format(m['value_secondary'],'g')
        return value+' '+m['unit']
    for field,(label,unit) in fields.items():
        index=next((i for i in range(len(s['vitals'])-1,-1,-1) if field in s['vitals'][i]),None)
        found=s['vitals'][index] if index is not None else None
        lines.append(f"  {label}: "+(display(index,field) if found is not None else 'Not measured')+
            ' | Source time: '+unknown(found.get('timestamp') if found else None))
    flagged=[v for v in s.get('structured_vitals',[]) if v['status']=='needs_confirmation']
    if flagged:
        lines.append('Readings needing verification (raw source retained):')
        for v in flagged:
            lines.append(f"  Check {v['sequence']+1}, {v['field']}: {v['raw_value']} — {v['note']}")
    lines.append('Measurement status: recorded means parseable, not clinically validated.')
    lines+=['Source times are displayed as reported; timezone may be unspecified.','Recent vitals history (last 5 checks):']
    for index in range(max(0,len(s['vitals'])-5),len(s['vitals'])):
        e=s['vitals'][index]
        lines.append(unknown(e.get('timestamp'))+' | '+ '; '.join(f'{fields[k][0]}: {display(index,k)}' for k in fields)+
                     ' | Reported change: '+unknown(e.get('status_change')))
    if not s['vitals']:lines.append('Unknown — no checks recorded.')
    lines+=['Reported interventions and responses:']
    interventions=[e for e in s['events'] if e['kind']=='intervention']
    for e in interventions:lines.append(e['recorded_at']+' | '+e['payload']['action']+' | Response: '+unknown(e['payload'].get('response')))
    if not interventions:lines.append('Unknown — no performed interventions reported.')
    lines+=['Protocol changes:']
    for e in s['events']:
        if e['kind']=='protocol':lines.append(e['recorded_at']+' | '+str(e['payload'].get('previous'))+' → '+str(e['payload'].get('matched_protocol')))
    lines+=['','R — Handoff request','Destination: '+unknown(d.get('destination')),
      'ETA: '+unknown(d.get('eta_minutes'))+' minutes','Request: '+unknown(d.get('handoff_request')),
      'Suggested actions are not recorded as performed interventions.']
    return '\n'.join(lines)


class MayaAdapter(Base):
    def __init__(self,db_path,dania=None,vitals=None):
        super().__init__(db_path,dania,vitals)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS maya_state (
                case_id TEXT PRIMARY KEY REFERENCES cases(case_id), version INTEGER NOT NULL,
                status TEXT NOT NULL, confirmation_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, body TEXT NOT NULL, response TEXT NOT NULL);''')
            db.execute("INSERT OR IGNORE INTO maya_state SELECT case_id,1,'active',? FROM cases",
                (json.dumps({'value':'unconfirmed','note':None}),))

    def _snapshot(self,db,cid):
        s=super()._snapshot(db,cid)
        state=db.execute('SELECT * FROM maya_state WHERE case_id=?',(cid,)).fetchone()
        s.update(version=state['version'] if state else 1,status=state['status'] if state else 'active',
            confirmation=json.loads(state['confirmation_json']) if state else {'value':'unconfirmed','note':None})
        s['sbar_report']=readable(s)
        return s

    def _save(self,db,cid,data,protocol,kind):
        super()._save(db,cid,data,protocol,kind)
        db.execute('UPDATE cases SET sbar_report=? WHERE case_id=?',(readable(self._snapshot(db,cid)),cid))

    def create_case(self,data,request_id):
        validate(data)
        if data.get('vitals_timeline') or data.get('protocol_history'):raise ValueError('Intake cannot contain a timeline')
        return self._transaction(request_id,{'op':'create','data':data},lambda db:self._create(db,data))

    def _create(self,db,data):
        cid=str(uuid4());t=now()
        db.execute('INSERT INTO cases VALUES(?,?,?,?,?,?)',(cid,t,t,'{}','{}',''))
        db.execute('INSERT INTO maya_state VALUES(?,?,?,?)',(cid,1,'active',json.dumps({'value':'unconfirmed','note':None})))
        self._save(db,cid,copy.deepcopy(data),self.dania.process_case(data),'intake')
        return self._snapshot(db,cid)

    def start_case(self,data):return self.create_case(data,str(uuid4()))

    def _transaction(self,key,body,fn):
        if not isinstance(key,str) or not key.strip() or len(key)>200:raise ValueError('Provide a request_id of 1–200 characters')
        body=json.dumps(body,sort_keys=True,allow_nan=False)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM requests WHERE request_id=?',(key,)).fetchone()
            if old:
                if old['body']!=body:raise Conflict('request_id already used with different input')
                return json.loads(old['response'])
            result=fn(db)
            db.execute('INSERT INTO requests VALUES(?,?,?)',(key,body,json.dumps(result,ensure_ascii=False)))
            return result

    def change(self,cid,kind,payload,reason,expected_version,request_id):
        if not isinstance(reason,str) or not reason.strip():raise ValueError('A reason is required')
        if type(expected_version)!=int:raise ValueError('expected_version must be an integer')
        validate(payload)
        def apply(db):
            s=self._snapshot(db,cid)
            if s['version']!=expected_version:raise Conflict('Stale version: reload before updating')
            if s['status']=='finished':raise Conflict('Case handoff is finished')
            data=copy.deepcopy(s['data']);before=copy.deepcopy(data)
            if kind=='correction':
                if set(payload)&{'vitals_timeline','protocol_history','deteriorating'}:raise ValueError('Use the dedicated source/observation route for these fields')
                data.update(payload)
            elif kind=='source':
                if not isinstance(payload.get('vitals_timeline',[]),list) or any(not isinstance(e,dict) for e in payload.get('vitals_timeline',[])):raise ValueError('Invalid source timeline')
                data=copy.deepcopy(payload)
            elif kind=='observation':
                allowed={'timestamp','blood_pressure','heart_rate','temperature','oxygen_saturation','status_change','is_breathing','deteriorating'}
                if set(payload)-allowed:raise ValueError('Unknown observation fields')
                if not set(payload)&(allowed-{'timestamp','deteriorating'}):raise ValueError('Provide a reading')
                if 'deteriorating' in payload and type(payload['deteriorating'])!=bool:raise ValueError('deteriorating must be boolean')
                for field in FIELDS:
                    if field in payload and parse_reading(field,payload[field])['status']=='needs_confirmation':
                        raise ValueError(field+' has an invalid format/value; correct it or use unknown/not_measured')
                if 'timestamp' in payload:
                    try:t=datetime.fromisoformat(payload['timestamp'].replace('Z','+00:00'))
                    except (ValueError,TypeError,AttributeError):raise ValueError('Invalid timestamp')
                    if t.tzinfo is None:raise ValueError('API timestamps require a timezone')
                entry={**payload}
                entry.setdefault('timestamp',now())
                data.setdefault('vitals_timeline',[]).append(copy.deepcopy(entry))
                if 'is_breathing' in payload:data['is_breathing']=payload['is_breathing']
                if 'deteriorating' in payload:data['deteriorating']=payload['deteriorating']
            elif kind=='confirmation':
                if set(payload)-{'value','note'} or payload.get('value') not in ('unconfirmed','worsening','not_worsening','uncertain'):raise ValueError('Invalid confirmation')
                db.execute('UPDATE maya_state SET confirmation_json=? WHERE case_id=?',(json.dumps(payload),cid))
            elif kind=='intervention':
                if set(payload)-{'action','response'} or not isinstance(payload.get('action'),str) or not payload['action'].strip():raise ValueError('Provide reported action')
            elif kind=='finish':
                if payload:raise ValueError('finish payload must be empty')
                db.execute("UPDATE maya_state SET status='finished' WHERE case_id=?",(cid,))
            else:raise ValueError('Unknown operation')
            if kind in ('source','observation') and len(data.get('vitals_timeline',[]))>len(before.get('vitals_timeline',[])):
                db.execute('UPDATE maya_state SET confirmation_json=? WHERE case_id=?',
                    (json.dumps({'value':'unconfirmed','note':'New check requires a fresh confirmation'}),cid))
            db.execute('UPDATE maya_state SET version=version+1 WHERE case_id=?',(cid,))
            db.execute('INSERT INTO case_events(case_id,kind,payload_json,recorded_at) VALUES(?,?,?,?)',
                (cid,'intervention' if kind=='intervention' else 'audit',json.dumps(payload if kind=='intervention' else {'operation':kind,'reason':reason,'before':before,'after':data},ensure_ascii=False),now()))
            self._save(db,cid,data,self.dania.process_case(data),kind+'_saved')
            return self._snapshot(db,cid)
        return self._transaction(request_id,{'cid':cid,'kind':kind,'payload':payload,'reason':reason,'version':expected_version},apply)

    def sync_source(self,cid,data,expected_version):
        return self.change(cid,'source',data,'Observed unchanged teammate output',expected_version,str(uuid4()))

    def run_followups(self,cid,ask_answer):
        initial=self.get_case(cid)
        if initial['status']!='active':raise Conflict('Case handoff is finished')
        def saved_answer(q,f,current):
            value=ask_answer(q,f,current)
            if isinstance(value,str):value=value.strip()
            if value is not None and value!='' and value!='unknown':
                s=self.get_case(cid)
                self.change(cid,'correction',{f:value},'Confirmed follow-up answer',s['version'],str(uuid4()))
            return value
        return self.dania.run_protocol_flow(initial['data'],saved_answer)

    def monitor(self,cid,ask,speak,interval_s=120,max_checks=None):
        count=0
        if interval_s<0 or (max_checks is not None and max_checks<0):raise ValueError('Invalid interval/check count')
        while max_checks is None or count<max_checks:
            s=self.get_case(cid)
            if s['status']!='active':raise Conflict('Case handoff is finished')
            data=copy.deepcopy(s['data']);n=len(data.get('vitals_timeline',[]))
            try:self.monitor_module.run_vitals_monitor(data,ask,speak,interval_s=interval_s,max_checks=1)
            finally:
                if len(data.get('vitals_timeline',[]))>n:self.sync_source(cid,data,s['version'])
            if len(data.get('vitals_timeline',[]))==n:break
            count+=1
        return self.get_case(cid)
