"""Integrated web UI; original voice/protocol files remain external."""
import os
from pathlib import Path
from flask import send_from_directory
from server import create_app, original_path
from maya_adapter import load_team_modules
BASE=Path(__file__).resolve().parent
app=create_app(os.environ.get('MIDAI_DB',str(BASE/'maya_cases.db')))
@app.get('/ui-config')
def config():
    dania,_=load_team_modules(original_path('dania_protocol_flow'),original_path('vitals_monitor'))
    return {'symptoms':sorted({s for p in dania.PROTOCOLS for s in p.required_symptoms})}
@app.get('/')
def index():return send_from_directory(BASE/'frontend','index.html')
@app.get('/hospital')
def hospital():return send_from_directory(BASE/'frontend','index.html')
@app.get('/maya')
def controls():return send_from_directory(BASE/'frontend','index.html')
@app.get('/ui/<path:name>')
def assets(name):return send_from_directory(BASE/'frontend',name)
if __name__=='__main__':
    host=os.environ.get('MIDAI_HOST','127.0.0.1')
    if host!='127.0.0.1' and not os.environ.get('MIDAI_API_TOKEN'):
        raise SystemExit('Set MIDAI_API_TOKEN before enabling network access.')
    app.run(host=host,port=5000,debug=False,use_reloader=False)
