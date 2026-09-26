"""Read-only execution observer: runs the original script without changing its code.

No patched functions, substituted imports, AST changes or edited teammate files.
Trace only the specified source files. Persistence errors stop execution visibly.
"""
import argparse
import copy
import json
from pathlib import Path
import runpy
import sys
from maya_adapter import MayaAdapter, load_team_modules


class SourceObserver:
    def __init__(self,adapter,voice_path,dania_path,vitals_path):
        self.adapter=adapter
        self.voice=str(Path(voice_path).resolve())
        self.dania=str(Path(dania_path).resolve())
        self.vitals=str(Path(vitals_path).resolve())
        self.case_id=None;self.version=None;self.last=None

    def save(self,data):
        raw=json.dumps(data,sort_keys=True,ensure_ascii=False,allow_nan=False)
        if raw==self.last:return
        if self.case_id is None:
            result=self.adapter.start_case(copy.deepcopy(data))
            self.case_id=result['case_id']
            print('Maya saved case:',self.case_id)
        else:result=self.adapter.sync_source(self.case_id,copy.deepcopy(data),self.version)
        self.version=result['version'];self.last=raw

    def trace(self,frame,event,arg):
        filename=frame.f_code.co_filename
        if filename not in (self.voice,self.dania,self.vitals):return None
        if event not in ('line','return'):return self.trace
        local=frame.f_locals;name=frame.f_code.co_name
        if filename==self.voice and name=='<module>':
            data=local.get('data');result=local.get('dania_result')
            if isinstance(result,dict) and data is not result.get('data'):return self.trace
            if isinstance(data,dict) and 'symptoms' in data:self.save(data)
        elif filename==self.dania and name=='run_protocol_flow':
            data=local.get('case')
            if isinstance(data,dict):self.save(data)
        elif filename==self.vitals and name=='run_vitals_monitor':
            data=local.get('case');entry=local.get('entry')
            # Source changes breathing before completing the check. Do not save that partial state.
            if isinstance(data,dict) and isinstance(entry,dict) and any(e is entry for e in data.get('vitals_timeline',[])):
                self.save(data)
        return self.trace


def execute_original(adapter,voice_path,dania_path,vitals_path):
    if sys.gettrace() is not None:raise RuntimeError('Run from terminal, outside debugger: observer needs the trace hook.')
    observer=SourceObserver(adapter,voice_path,dania_path,vitals_path)
    old_path=list(sys.path)
    try:
        sys.path.insert(0,str(Path(voice_path).resolve().parent))
        sys.settrace(observer.trace)
        runpy.run_path(str(Path(voice_path).resolve()),run_name='__main__')
    finally:
        sys.settrace(None);sys.path[:]=old_path
        if observer.case_id:print('Saved case available for resume:',observer.case_id)
    return observer.case_id

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--voice',required=True);p.add_argument('--dania',required=True);p.add_argument('--vitals',required=True)
    p.add_argument('--db',default='maya_cases.db');p.add_argument('--server');args=p.parse_args()
    d,v=load_team_modules(args.dania,args.vitals)
    if args.server:
        from remote_client import HospitalClient
        adapter=HospitalClient(args.server)
    else:adapter=MayaAdapter(args.db,d,v)
    execute_original(adapter,args.voice,args.dania,args.vitals)
