"""HTTP connection with stable request IDs for one transport retry."""
import json
import os
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from uuid import uuid4

class HospitalClient:
    def __init__(self,url,token=None):
        self.url=url.rstrip('/')
        self.token=token if token is not None else os.environ.get('MIDAI_API_TOKEN')
    def call(self,path,body=None):
        raw=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        headers={'Content-Type':'application/json'}
        if self.token:headers['Authorization']='Bearer '+self.token
        for attempt in range(2):
            try:
                with urlopen(Request(self.url+path,data=raw,headers=headers),timeout=10) as response:
                    return json.load(response)
            except HTTPError as error:
                detail=error.read().decode(errors='replace')
                if error.code==503 and attempt==0:continue
                raise RuntimeError(f'HTTP {error.code}: {detail}') from None
            except (URLError,TimeoutError,OSError):
                if attempt:raise RuntimeError('Server connection failed. Update not confirmed; inspect saved case before continuing.') from None
    def start_case(self,data):return self.call('/cases',{'data':data,'request_id':str(uuid4())})
    def sync_source(self,cid,data,expected_version):
        return self.call('/cases/'+cid+'/source',{'payload':data,'reason':'Observed unchanged teammate output',
            'expected_version':expected_version,'request_id':str(uuid4())})
    def get_case(self,cid):return self.call('/cases/'+cid)
