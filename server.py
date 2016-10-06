from flask import Flask, request, Response
import logging
import threading
import sys
import os
import site
import traceback
import queue
import datetime
from urllib.parse import urlparse
from threading import Lock, Thread
from collections import defaultdict
global PGCli, need_completion_refresh, need_search_path_refresh
global has_meta_cmd, has_change_path_cmd
from pgcli.main import PGCli, has_meta_cmd, has_change_path_cmd
global PGExecute
from pgcli.pgexecute import PGExecute
global PGCompleter
from pgcli.pgcompleter import PGCompleter
global special
from pgspecial import PGSpecial
special = PGSpecial()
global CompletionRefresher
from pgcli.completion_refresher import CompletionRefresher
global Document
from prompt_toolkit.document import Document
global format_output
from pgcli.main import format_output
global psycopg2
import psycopg2
global sqlparse
import sqlparse
import json
import uuid
import datetime
import time

global serverList# = defaultdict(list)
completers = defaultdict(list)  # Dict mapping urls to pgcompleter objects
completer_lock = Lock()
executors = defaultdict(list)  # Dict mapping buffer ids to pgexecutor objects
executor_lock = Lock()
bufferConnections = defaultdict(str) #Dict mapping bufferids to connectionstrings
queryResults = defaultdict(list)
global config
noticepoll_stop = threading.Event()

def main(args=None):
    global serverList
    global config
    try:
        with open('config.json') as json_data_file:
            config = json.load(json_data_file)
            #Todo: load PGCLI using site-dirs from config file.
            serverList = config['connections']
    except:
        config = dict()
        serverList = dict()


def new_server(alias, url, requiresauthkey):
    global config
    serverList[alias] = {'url':url, 'requiresauthkey':requiresauthkey}
    config['connections'] = serverList
    with open('config.json', mode='w', encoding='utf-8') as configfile:
        json.dump(config, configfile)

def remove_server(alias):
    global config
    if config['connections'].get(alias):
        del config['connections'][alias]
    if serverList.get(alias):
        del serverList[alias]
    with open('config.json', mode='w', encoding='utf-8') as configfile:
        json.dump(config, configfile)

def connect_server(alias, authkey=None):
    server = next((s for (a, s) in serverList.items() if a == alias), None)
    if not server:
        return {'success':False, 'errormessage':'Unknown alias.'}
    if executors[alias]:
        return {'success':False, 'errormessage':'Already connected to server.'}
    refresher = CompletionRefresher()
    try:
        executor = new_executor(server['url'], authkey)
        completer = PGCompleter()
        executors[alias] = executor
        refresher.refresh(executor, special=special, callbacks=(
                            lambda c: swap_completer(c, alias)))
    except psycopg2.Error as e:
        return {'success':False, 'errormessage':str(e)}
    return {'success':True, 'errormessage':None}

def refresh_servers():
    for alias, server in serverList.items():
        if alias in executors:
            try:
                with executors[alias].conn.cursor() as cur:
                    cur.execute('SELECT 1')
                    server['connected'] = True
            except psycopg2.OperationalError:
                server['connected'] = False
                del executors[alias]
        else:
            server['connected'] = False

def disconnect_server(alias):
    if alias not in executors:
        return {'success':False, 'errormessage':'Unknown alias.'}
    for alias, server in ((a, s) for (a, s) in serverList.items() if a == alias):
        try:
            with executors[alias].conn.cursor() as cur:
                cur.close()
                server['connected'] = False
        except psycopg2.OperationalError:
            server['connected'] = False
            del executors[alias]

def new_executor(url, pwd=None):
    global password
    password = None
    uri = urlparse(url)
    database = uri.path[1:]  # ignore the leading fwd slash
    dsn = None  # todo: what is this for again
    return PGExecute(database, uri.username, pwd or uri.password, uri.hostname,
                     uri.port, dsn)

def swap_completer(comp,alias):
    completers[alias] = comp

def run_sql(alias, sql, uuid):
    for sql in sqlparse.split(sql):
        queryResults[uuid].append({
            'columns': None,
            'rows': None,
            'query': sql,
            'notices': None,
            'complete': False,
            'executing': False,
            'timestamp': None,
            'runtime_seconds': None,
            'error':None
        })
    executor = executors[alias]
    with executor_lock:
        with executor.conn.cursor() as cur:
            for n, qr in enumerate(queryResults[uuid]):
                timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
                timestamp_ts = time.mktime(datetime.datetime.now().timetuple())
                currentQuery = queryResults[uuid][n]
                currentQuery['timestamp'] = time.strftime('%Y-%m-%d %H:%M:%S')
                currentQuery['executing'] = True
                queryResults[uuid][n] = currentQuery

                #start polling notices
                #t = Thread(target=get_notices,
                #               args=(currentQuery, uuid, n, executor),
                #               name='get_notices')
                #t.setDaemon(True)
                #t.start()

                #run query
                try:
                    cur.execute(qr['query'])
                    currentQuery['columns'] = [{'name': d.name, 'type_code': d.type_code} for d in cur.description]
                    currentQuery['rows'] = [x for x in cur.fetchall()]
                except psycopg2.Error as e:
                    currentQuery['error'] = str(e)
                #update query result
                currentQuery['runtime_seconds'] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
                currentQuery['complete'] = True
                currentQuery['executing'] = False
                #TODO: statusmessage of statement.

                notices = []
                while executor.conn.notices:
                    notices.append(executor.conn.notices.pop(0))
                currentQuery['notices'] = notices

                queryResults[uuid][n] = currentQuery
                #stop polling notices
                #noticepoll_stop.set()

def get_notices(currentQuery, uuid, n, executor):
    notices = []
    while(not noticepoll_stop.is_set()):
        while executor.conn.notices:
            notices = executor.conn.notices.pop(0)
        currentQuery['notices'] = notices
        queryResults[uuid][n] = currentQuery
        noticepoll_stop.wait(0.1)

app = Flask(__name__)
@app.route("/query", methods=['POST'])
def query():
    alias = request.form.get('alias', 'Vagrant')
    sql = request.form['query']
    uid = str(uuid.uuid1())
    t = Thread(target=run_sql,
                   args=(alias, sql, uid),
                   name='run_sql')
    t.setDaemon(True)
    t.start()
    return 'localhost:5000/result/' + uid

@app.route("/result/<uuid>")
def result(uuid):
    result = queryResults[uuid]
    for r in result:
        if r['executing'] == 1:
            timestamp_ts = time.mktime(datetime.datetime.strptime(r["timestamp"], '%Y-%m-%d %H:%M:%S').timetuple())
            r["runtime_seconds"] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
    return Response(str(json.dumps(result)), mimetype='text/json')

@app.route("/pos/<pos>/query/<query>")
def completer(pos,query):
    comps = completer.get_completions(
                Document(text=query, cursor_position=int(pos)), None)
    return Response(str(json.dumps([c.text for c in comps])), mimetype='text/json')

@app.route("/listservers")
def list_servers():
    refresh_servers()
    return Response(str(json.dumps(serverList)), mimetype='text/json')

@app.route("/listconnections")
def list_connections():
    return Response(str(json.dumps(get_connections(), indent=4)), mimetype='text/json')

@app.route("/connect", methods=['POST'])
def connect():
    alias = request.form['alias']
    authkey = request.form['authkey']
    return Response(str(json.dumps(connect_server(alias, authkey))), mimetype='text/json')

@app.route("/addserver", methods=['POST'])
def addserver():
    alias = request.form['alias']
    if next((s for (a, s) in serverList.items() if a == alias), None):
        return Response(str(json.dumps({'success':False, 'errormessage':'Server alias already exists.'})), mimetype='text/json')
    else:
        url = request.form['url']
        requiresauthkey = request.form['requiresauthkey']
        new_server(alias, url, requiresauthkey)
        return Response(str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')

@app.route("/delserver", methods=['POST'])
def delserver():
    try:
        alias = request.form['alias']
        remove_server(alias)
        return Response(str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')
    except Exception as e:
        return Response(str(json.dumps({'success':False, 'errormessage':str(e)})), mimetype='text/json')

if __name__ == "__main__":
    main()
    app.run()