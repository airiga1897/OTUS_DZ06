"""Restore reconstructed standalone PostgreSQL into Patroni, then launch portal."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

BASE = Path('/home/otus/dz06')
APP = Path('/opt/otus-dz06')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def write(path, text, mode=0o600):
    path = Path(path)
    path.write_text(text, encoding='utf-8')
    path.chmod(mode)


def main():
    settings = json.loads((BASE / 'settings.json').read_text())
    lab, passwords = settings['lab'], settings['passwords']
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    run('systemctl', 'disable', '--now', 'otus-dz06-route.service')
    run('apt-get', 'update', '-qq')
    Path('/etc/postgresql-common').mkdir(exist_ok=True)
    write('/etc/postgresql-common/createcluster.conf', 'create_main_cluster = false\n', 0o644)
    run('apt-get', 'install', '-y', '-qq', 'haproxy', 'nginx', 'postgresql-16',
        'postgresql-client-16', 'python3-venv', 'python3-dev', 'build-essential', 'libpcre3-dev')
    APP.mkdir(exist_ok=True)
    shutil.copytree(BASE / 'app', APP / 'app', dirs_exist_ok=True)
    if not (APP / 'venv/bin/python').exists():
        run('python3', '-m', 'venv', str(APP / 'venv'))
    run(str(APP / 'venv/bin/pip'), 'install', '-r', str(APP / 'app/requirements.txt'), 'uWSGI==2.0.31')
    if subprocess.run(['id', 'otusapp'], stdout=subprocess.DEVNULL).returncode:
        run('useradd', '--system', '--home', str(APP), '--shell', '/usr/sbin/nologin', 'otusapp')
    run('install', '-d', '-o', 'otusapp', '-g', 'otusapp', '/srv/otus/static', '/srv/otus/media')
    write('/srv/otus/.ready', 'otus-dz04\n', 0o644)
    haproxy = '''global
    log /dev/log local0
    user haproxy
    group haproxy
    daemon
defaults
    mode tcp
    timeout connect 3s
    timeout client 60s
    timeout server 60s
listen postgres
    bind 127.0.0.1:5432
    option httpchk GET /primary
    http-check expect status 200
    default-server inter 2s fall 2 rise 2 on-marked-down shutdown-sessions
'''
    for name in ['db1', 'db2', 'db3']:
        haproxy += f"    server {name} {lab['private_ips'][name]}:5432 check port 8008\n"
    write('/etc/haproxy/haproxy.cfg', haproxy, 0o644)
    run('haproxy', '-c', '-f', '/etc/haproxy/haproxy.cfg')
    run('systemctl', 'enable', 'haproxy')
    run('systemctl', 'restart', 'haproxy')
    for _ in range(120):
        leader = None
        for name in ['db1', 'db2', 'db3']:
            try:
                with urllib.request.urlopen('http://' + lab['private_ips'][name] + ':8008/primary', timeout=2) as response:
                    if response.status == 200:
                        leader = name
            except OSError:
                pass
        if leader:
            break
        time.sleep(2)
    else:
        raise RuntimeError('Patroni leader did not become ready')
    run(str(APP / 'venv/bin/python'), str(BASE / 'scripts/remote/migration.py'))
    env = {'DJANGO_SECRET_KEY': passwords['django'], 'DB_NAME': 'otus', 'DB_USER': 'otus',
           'DB_PASSWORD': passwords['app'], 'DB_HOST': '127.0.0.1', 'DB_PORT': '5432',
           'DJANGO_ALLOWED_HOSTS': lab['app_public_ip'] + ',localhost,127.0.0.1',
           'REQUIRE_SHARED_MOUNT': '0', 'SHARED_ROOT': '/srv/otus', 'NODE_NAME': 'otus-dz06-app'}
    write('/etc/otus-dz06.env', ''.join(k + '=' + v + '\n' for k, v in env.items()))
    run(str(APP / 'venv/bin/python'), str(APP / 'app/manage.py'), 'collectstatic', '--noinput', env={**os.environ, **env})
    run('chown', '-R', 'otusapp:otusapp', '/srv/otus')
    write('/etc/systemd/system/otus-dz06.service', '''[Unit]
Description=OTUS DZ06 Django portal
After=network.target haproxy.service
[Service]
User=otusapp
Group=otusapp
WorkingDirectory=/opt/otus-dz06/app
EnvironmentFile=/etc/otus-dz06.env
ExecStart=/opt/otus-dz06/venv/bin/uwsgi --http-socket 127.0.0.1:8080 --module config.wsgi:application --master --processes 2 --threads 2 --enable-threads --need-app --die-on-term
Restart=on-failure
[Install]
WantedBy=multi-user.target
''', 0o644)
    write('/etc/nginx/sites-available/otus-dz06', '''server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 5m;
    location /static/ { alias /srv/otus/static/; }
    location /media/ { alias /srv/otus/media/; }
    location / { proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; }
}
''', 0o644)
    Path('/etc/nginx/sites-enabled/default').unlink(missing_ok=True)
    link = Path('/etc/nginx/sites-enabled/otus-dz06')
    if not link.exists():
        link.symlink_to('/etc/nginx/sites-available/otus-dz06')
    run('nginx', '-t')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'otus-dz06', 'nginx')
    run('systemctl', 'restart', 'otus-dz06', 'nginx')
    run('haproxy', '-v')


if __name__ == '__main__':
    main()
