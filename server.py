"""Maya compatibility facade for Sadeel's unchanged ui_server.py and JavaScript."""
import html
import json
import math
import os
from pathlib import Path
from datetime import datetime
from hospital_api import create_app as create_api
from maya_adapter import load_team_modules

BASE=Path(__file__).resolve().parent

def numeric(value):
    if value is None or isinstance(value,bool):return None
    try:n=float(value)
    except (TypeError,ValueError):return None
    if not math.isfinite(n):return None
    return int(n) if n.is_integer() else n


def ui_snapshot(snapshot):
    """Add legacy UI aliases; retain the full original source fields separately."""
    result=dict(snapshot);protocol=snapshot['protocol'];confirmation=snapshot['confirmation']['value']
    source_flag=bool(snapshot['data'].get('deteriorating'))
    verification=(source_flag and confirmation in ('unconfirmed','uncertain')) or confirmation=='uncertain'
    reasons=['Dania supplied symptom table; priority then table order.',
        'Source-generated flag: '+str(source_flag)+'; responder confirmation: '+confirmation+'.']
    result.update(match={'protocol_name':protocol.get('matched_protocol'),'protocol_id':protocol.get('matched_protocol'),
        'priority_rank':protocol.get('priority'),'reasons':reasons,
        'missing_fields':[q['answer_field'] for q in protocol.get('missing_followups',[])]},
        rematch_pending=False,source_deteriorating=source_flag,
        deteriorating=confirmation=='worsening')
    observations=[];latest={}
    for index,entry in enumerate(snapshot['vitals']):
        data={}
        for source,target,unit in [('heart_rate','heart_rate','bpm'),('temperature','temperature_c','°C'),('oxygen_saturation','oxygen_saturation','%')]:
            if source in entry:
                data[target]=numeric(entry[source])
        if 'blood_pressure' in entry:
            pieces=str(entry.get('blood_pressure','')).split('/')
            bp=[numeric(x) for x in pieces] if len(pieces)==2 else [None,None]
            data.update(systolic_bp=bp[0],diastolic_bp=bp[1])
        for measurement in snapshot.get('structured_vitals',[]):
            if measurement['sequence'] != index:continue
            field=measurement['field']
            value=measurement['value'] if measurement['status']=='recorded' else None
            if field=='blood_pressure':
                data.update(systolic_bp=value,diastolic_bp=measurement['value_secondary'] if value is not None else None)
            else:data[{'temperature':'temperature_c'}.get(field,field)]=value
        # The unchanged hospital renderer interpolates these values into innerHTML.
        # Numeric display values above and HTML-escaped free text prevent source HTML execution.
        data['status_change']=html.escape(str(entry.get('status_change') or 'unknown'),quote=True)
        timestamp=entry.get('timestamp')
        try:datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        except (ValueError,AttributeError,TypeError):timestamp=None
        observations.append({'id':str(index),'observed_at':timestamp,'data':data})
        for field,value in data.items():latest[field]={'value':value,'observed_at':timestamp}
    verification = verification or any(v['value'] is None for k,v in latest.items() if k!='status_change')
    verification = verification or snapshot['data'].get('is_breathing') in (None,'','unknown')
    result.update(observations=observations,latest_observations=latest,verification_needed=verification)
    return result


def original_path(stem):
    explicit=os.environ.get('MIDAI_'+('DANIA' if stem=='dania_protocol_flow' else 'VITALS')+'_FILE')
    if explicit:return explicit
    root=Path(os.environ.get('MIDAI_TEAM_DIR',str(BASE.parent/'team')))
    direct=root/(stem+'.py')
    if direct.exists():return str(direct)
    found=list(root.glob(stem+'(*).py'))
    if len(found)!=1:raise RuntimeError('Set MIDAI_TEAM_DIR to the exact team files, or MIDAI_DANIA_FILE / MIDAI_VITALS_FILE.')
    return str(found[0])


def create_app(db_path='maya_cases.db',checkin_seconds=120,integrated=True,token=None):
    if not integrated:raise ValueError('Sadeel compatibility requires integrated=True')
    d,v=load_team_modules(original_path('dania_protocol_flow'),original_path('vitals_monitor'))
    app=create_api(db_path,token if token is not None else os.environ.get('MIDAI_API_TOKEN'),d,v)
    app.config['CHECKIN_SECONDS']=checkin_seconds
    @app.after_request
    def compatibility(response):
        if response.is_json and response.status_code<300:
            payload=response.get_json()
            if isinstance(payload,dict) and {'case_id','protocol','vitals','confirmation'}.issubset(payload):
                response.set_data(json.dumps(ui_snapshot(payload),ensure_ascii=False))
        return response
    return app
