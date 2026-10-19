"""Check actual SQL/HTTP, committed data and member recovery through fixed HAProxy."""
import datetime
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

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
    return psycopg.connect(host=host, port=5432, user='otus', password=passwords['app'], dbname='otus', connect_timeout=3)


def probe(slug):
    with sql_connect() as conn:
        assert conn.execute('SELECT NOT pg_is_in_recovery()').fetchone()[0]
        assert conn.execute("SELECT count(*) FROM knowledge_article WHERE slug='before-failover'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM auth_user WHERE username='migration-proof'").fetchone()[0] == 1
        conn.execute('INSERT INTO knowledge_article(title,slug,lesson,summary,body,image,published,updated_at) '
                     "VALUES (%s,%s,6,'proof','committed proof','',true,now()) ON CONFLICT (slug) DO NOTHING", (slug, slug))
    # Both index and detail access the database through the real portal configuration.
    for path in ['/', '/health/ready', '/articles/' + slug + '/']:
        with urllib.request.urlopen('http://127.0.0.1' + path, timeout=5) as response:
            body = response.read()
            assert response.status == 200 and body
    return True


def main():
    mode = sys.argv[1]
    previous = sys.argv[2] if len(sys.argv) > 2 else None
    start = time.monotonic()
    samples = []
    if mode == 'baseline':
        with sql_connect() as conn:
            conn.execute('INSERT INTO knowledge_article(title,slug,lesson,summary,body,image,published,updated_at) '
                         "VALUES ('before failover','before-failover',6,'proof','committed proof','',true,now()) ON CONFLICT (slug) DO NOTHING")
    for attempt in range(150):
        members = cluster()
        leaders = [name for name, data in members.items() if data.get('role') in ['master', 'primary'] and data.get('state') == 'running']
        healthy = sum(data.get('state') == 'running' for data in members.values())
        try:
            assert len(leaders) == 1
            if mode == 'failover':
                assert leaders[0] != previous and healthy == 2
            else:
                assert healthy == 3
            probe({'baseline': 'baseline-proof', 'failover': 'after-failover', 'recovered': 'recovered-proof'}[mode])
            if mode == 'recovered':
                for name in ['db1', 'db2', 'db3']:
                    with sql_connect(lab['private_ips'][name]) as conn:
                        assert conn.execute("SELECT count(*) FROM knowledge_article WHERE slug IN ('before-failover','after-failover')").fetchone()[0] == 2
            break
        except (AssertionError, OSError, psycopg.Error) as error:
            samples.append({'elapsed': round(time.monotonic() - start, 2), 'error': type(error).__name__})
            time.sleep(2)
    else:
        raise RuntimeError('Verification deadline exceeded: ' + json.dumps(samples[-5:]))
    result = {'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'mode': mode,
              'leader': leaders[0], 'members': members, 'sql_and_http': True,
              'committed_record_preserved': True, 'new_write_succeeded': True,
              'probe_elapsed_seconds': round(time.monotonic() - start, 2), 'failed_probes': samples,
              'migration': json.loads(Path('/opt/otus-dz06/migration-result.json').read_text())}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
