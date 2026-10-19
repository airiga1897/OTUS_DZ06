"""A real pg_dump/pg_restore migration from reconstructed standalone PG16."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psycopg
from psycopg import sql

BASE = Path('/home/otus/dz06')
APP = Path('/opt/otus-dz06')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    settings = json.loads((BASE / 'settings.json').read_text())
    passwords = settings['passwords']
    marker = APP / 'migration-result.json'
    if marker.exists():
        print('Existing completed migration preserved')
        return
    source = Path('/var/lib/postgresql/dz06-source')
    run('install', '-d', '-o', 'postgres', '-g', 'postgres', '-m', '700', str(source))
    pg = '/usr/lib/postgresql/16/bin/'
    if not (source / 'PG_VERSION').exists():
        run('runuser', '-u', 'postgres', '--', pg + 'initdb', '-D', str(source), '--auth-local=peer', '--auth-host=scram-sha-256')
    run('runuser', '-u', 'postgres', '--', pg + 'pg_ctl', '-D', str(source), '-l', str(source / 'server.log'),
        '-o', '-p 55432 -h 127.0.0.1', '-w', 'start')
    try:
        role_sql = sql.SQL('CREATE ROLE otus LOGIN PASSWORD {}').format(sql.Literal(passwords['app'])).as_string()
        run('runuser', '-u', 'postgres', '--', 'psql', '-p', '55432', '-v', 'ON_ERROR_STOP=1',
            input=role_sql + ';\n', text=True, stdout=subprocess.DEVNULL)
        run('runuser', '-u', 'postgres', '--', 'createdb', '-p', '55432', '-O', 'otus', 'otus')
        env = {**os.environ, 'DJANGO_SECRET_KEY': passwords['django'], 'DB_HOST': '127.0.0.1',
               'DB_PORT': '55432', 'DB_NAME': 'otus', 'DB_USER': 'otus', 'DB_PASSWORD': passwords['app'],
               'REQUIRE_SHARED_MOUNT': '0', 'SHARED_ROOT': '/srv/otus'}
        manage = [str(APP / 'venv/bin/python'), str(APP / 'app/manage.py')]
        run(*manage, 'migrate', '--noinput', env=env)
        run(*manage, 'seed_lessons', env=env)
        code = "from django.contrib.auth import get_user_model; get_user_model().objects.create_user('migration-proof', is_active=False)"
        run(*manage, 'shell', '-c', code, env=env)
        with psycopg.connect(host='127.0.0.1', port=55432, dbname='otus', user='otus', password=passwords['app']) as conn:
            source_rows = conn.execute('SELECT id,title,slug,lesson,summary,body,image,published,updated_at FROM knowledge_article ORDER BY id').fetchall()
        dump = APP / 'portal.dump'
        pgenv = {**os.environ, 'PGPASSWORD': passwords['app']}
        run('pg_dump', '-h', '127.0.0.1', '-p', '55432', '-U', 'otus', '-d', 'otus', '-Fc', '-f', str(dump), env=pgenv)
        # HAProxy endpoint must route to the current primary, never a fixed node.
        for attempt in range(60):
            try:
                admin = psycopg.connect(host='127.0.0.1', port=5432, user='postgres', password=passwords['postgres'], dbname='postgres', connect_timeout=3, autocommit=True)
                break
            except psycopg.OperationalError:
                time.sleep(2)
        else:
            raise RuntimeError('HAProxy primary connection unavailable')
        with admin:
            admin.execute(sql.SQL('CREATE ROLE otus LOGIN PASSWORD {}').format(sql.Literal(passwords['app'])))
            admin.execute('CREATE DATABASE otus OWNER otus')
        run('pg_restore', '-h', '127.0.0.1', '-p', '5432', '-U', 'otus', '-d', 'otus',
            '--no-owner', '--no-privileges', '--exit-on-error', str(dump), env=pgenv)
        with psycopg.connect(host='127.0.0.1', port=5432, dbname='otus', user='otus', password=passwords['app']) as conn:
            target_rows = conn.execute('SELECT id,title,slug,lesson,summary,body,image,published,updated_at FROM knowledge_article ORDER BY id').fetchall()
            users = conn.execute("SELECT count(*) FROM auth_user WHERE username='migration-proof' AND NOT is_active").fetchone()[0]
            assert conn.execute('SELECT NOT pg_is_in_recovery()').fetchone()[0]
        assert source_rows == target_rows and len(target_rows) == 4 and users == 1
        marker.write_text(json.dumps({'source': 'reconstructed_DZ04_seed_lessons_on_standalone_PostgreSQL16',
            'historical_dump_available': False, 'method': 'pg_dump -Fc / pg_restore', 'articles': len(target_rows),
            'full_article_rows_equal': True, 'inactive_user_preserved': True,
            'dump_sha256': hashlib.sha256(dump.read_bytes()).hexdigest()}, indent=2))
        dump.unlink()
    finally:
        run('runuser', '-u', 'postgres', '--', pg + 'pg_ctl', '-D', str(source), '-m', 'fast', '-w', 'stop')


if __name__ == '__main__':
    main()
