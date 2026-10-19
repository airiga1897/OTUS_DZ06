"""Verify each failover using newly committed rows and exact read-back checks."""
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request
import uuid

PYTHON = '/opt/otus-dz06/venv/bin/python'
if sys.prefix != '/opt/otus-dz06/venv':
    os.execv(PYTHON, [PYTHON, __file__, *sys.argv[1:]])

import psycopg

BASE = Path('/home/otus/dz06')
settings = json.loads((BASE / 'settings.json').read_text())
lab, passwords = settings['lab'], settings['passwords']


def cluster():
    members = {}
    for name in ['db1', 'db2', 'db3']:
        try:
            with urllib.request.urlopen('http://' + lab['private_ips'][name] + ':8008/patroni', timeout=2) as response:
                data = json.load(response)
            members[name] = {'role': data.get('role'), 'state': data.get('state'),
                             'timeline': data.get('timeline'), 'server_version': data.get('server_version')}
        except OSError:
            members[name] = {'state': 'unreachable'}
    return members


def sql_connect(host='127.0.0.1'):
    return psycopg.connect(host=host, port=5432, user='otus', password=passwords['app'],
                          dbname='otus', connect_timeout=3, options='-c statement_timeout=10000')


def check_record(conn, record):
    row = conn.execute('SELECT id,slug,body FROM knowledge_article WHERE id=%s', (record['id'],)).fetchone()
    if row != (record['id'], record['slug'], record['body']):
        raise AssertionError('Committed record is absent or changed')


def insert_record(mode):
    token = uuid.uuid4().hex
    slug = mode + '-' + token
    body = 'committed-' + uuid.uuid4().hex
    with sql_connect() as conn:
        if not conn.execute('SELECT NOT pg_is_in_recovery()').fetchone()[0]:
            raise AssertionError('HAProxy did not route to primary')
        if conn.execute("SELECT count(*) FROM auth_user WHERE username='migration-proof'").fetchone()[0] != 1:
            raise AssertionError('Migrated user missing')
        row = conn.execute('INSERT INTO knowledge_article(title,slug,lesson,summary,body,image,published,updated_at) '
                           "VALUES (%s,%s,6,'proof',%s,'',true,now()) RETURNING id,slug,body",
                           (slug, slug, body)).fetchone()
    record = dict(zip(['id', 'slug', 'body'], row))
    # Read through a new connection after COMMIT.
    with sql_connect() as conn:
        check_record(conn, record)
    return record


def check_http(record):
    for path in ['/', '/health/ready', '/articles/' + record['slug'] + '/']:
        with urllib.request.urlopen('http://127.0.0.1' + path, timeout=5) as response:
            body = response.read()
            if response.status != 200 or not body:
                raise AssertionError('Portal unavailable')
            if path.startswith('/articles/') and record['body'].encode() not in body:
                raise AssertionError('Portal returned different article content')


def main():
    mode, run_id = sys.argv[1:3]
    previous = sys.argv[3] if len(sys.argv) > 3 else None
    if mode not in ['baseline', 'failover', 'recovered'] or not re.fullmatch('[0-9a-f]{32}', run_id):
        raise ValueError('Invalid verification context')
    folder = Path('/opt/otus-dz06/probes')
    folder.mkdir(exist_ok=True)
    context_path = folder / (run_id + '.json')
    context = json.loads(context_path.read_text()) if context_path.exists() else {}
    required = [] if mode == 'baseline' else [context['baseline']]
    if mode == 'recovered':
        required.append(context['failover'])
        if 'recovered' in context:
            required.append(context['recovered'])
    for attempt in range(150):
        members = cluster()
        leaders = [n for n, d in members.items() if d.get('role') in ['master', 'primary'] and d.get('state') == 'running']
        healthy = sum(d.get('state') == 'running' for d in members.values())
        try:
            if len(leaders) != 1 or healthy != (2 if mode == 'failover' else 3):
                raise AssertionError('Cluster membership is not ready')
            if mode == 'failover' and leaders[0] == previous:
                raise AssertionError('Leader did not change')
            with sql_connect() as conn:
                for record in required:
                    check_record(conn, record)
            record = insert_record(mode)
            check_http(record)
            if mode == 'recovered':
                for name in ['db1', 'db2', 'db3']:
                    with sql_connect(lab['private_ips'][name]) as conn:
                        for committed in required + [record]:
                            check_record(conn, committed)
            break
        except (AssertionError, OSError, psycopg.Error):
            time.sleep(2)
    else:
        raise RuntimeError('SQL/HTTP or cluster verification failed')
    context[mode] = record
    context_path.write_text(json.dumps(context))
    result = {'mode': mode, 'run_id': run_id, 'leader': leaders[0], 'members': members,
              'sql_and_http': True, 'committed_record_preserved': True, 'new_write_succeeded': True,
              'new_record': record, 'preserved_records': required,
              'replica_records_verified': mode == 'recovered',
              'migration': json.loads(Path('/opt/otus-dz06/migration-result.json').read_text())}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
