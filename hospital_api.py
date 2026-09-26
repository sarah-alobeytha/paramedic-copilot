"""Read-only hospital API over the adapter database; no teammate imports needed."""
import argparse
import os
import hmac
import sqlite3
from flask import Flask, jsonify, request
from maya_adapter import MayaAdapter, Conflict, load_team_modules


def create_app(db_path='maya_cases.db', token=None, dania=None, vitals=None):
    app = Flask(__name__)
    adapter = MayaAdapter(db_path,dania,vitals)
    app.config["MAX_CONTENT_LENGTH"]=600000
    @app.before_request
    def authorize():
        if token and (request.path=='/health' or request.path.startswith('/cases')) and not hmac.compare_digest(request.headers.get('Authorization',''), 'Bearer '+token):
            return jsonify(error='unauthorized'),401
    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        return response
    @app.errorhandler(KeyError)
    def missing(error):
        return jsonify(error='case_not_found'),404
    @app.errorhandler(sqlite3.OperationalError)
    def unavailable(error):
        return jsonify(error='database_unavailable'),503
    @app.errorhandler(Conflict)
    def conflict(error):
        return jsonify(error='conflict',message=str(error)),409
    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error='invalid_input',message=str(error)),400
    def body(allowed):
        data=request.get_json()
        if not isinstance(data,dict) or set(data)-allowed:raise ValueError('Invalid request fields')
        if dania is None:raise ValueError('Start with --dania and --vitals to enable writes')
        return data
    @app.post('/cases')
    def create():
        data=body({'data','request_id'})
        return jsonify(adapter.create_case(data.get('data'),data.get('request_id'))),201
    @app.post('/cases/<cid>/<operation>')
    def write(cid,operation):
        mapping={'source':'source','corrections':'correction','observations':'observation','interventions':'intervention','confirmation':'confirmation','finish':'finish'}
        if operation not in mapping:return jsonify(error='unknown_operation'),404
        data=body({'payload','reason','expected_version','request_id'})
        return jsonify(adapter.change(cid,mapping[operation],data.get('payload',{}),data.get('reason'),data.get('expected_version'),data.get('request_id')))
    @app.get('/health')
    def health():
        return jsonify(status='ok',mode='simulation')
    @app.get('/cases')
    def cases():
        with adapter.connect() as db:
            return jsonify(cases=[dict(r) for r in db.execute('SELECT case_id,created_at,updated_at FROM cases ORDER BY created_at DESC')])
    @app.get('/cases/<cid>')
    def case(cid):
        return jsonify(adapter.get_case(cid))
    @app.get('/cases/<cid>/report')
    def report(cid):
        return app.response_class(adapter.get_case(cid)['sbar_report'],mimetype='text/plain')
    return app

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--db',default='maya_cases.db')
    p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=5000)
    p.add_argument('--dania');p.add_argument('--vitals')
    args=p.parse_args()
    if bool(args.dania)!=bool(args.vitals):p.error('Provide both --dania and --vitals')
    d,v=load_team_modules(args.dania,args.vitals) if args.dania else (None,None)
    token=os.environ.get('MIDAI_API_TOKEN')
    if args.host not in ('127.0.0.1','localhost','::1') and not token:
        p.error('Set MIDAI_API_TOKEN before network access.')
    create_app(args.db,token,d,v).run(host=args.host,port=args.port,debug=False,use_reloader=False)
