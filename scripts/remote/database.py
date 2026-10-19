"""Bootstrap only new DZ06 database nodes; repeated installs preserve PGDATA."""
import json
from pathlib import Path
import subprocess
import sys


def run(*args):
    subprocess.run(args, check=True)


def write(path, text, mode=0o644):
    target = Path(path)
    target.write_text(text, encoding='utf-8')
    target.chmod(mode)


def main():
    config = json.loads(Path('/home/otus/dz06/node.json').read_text())
    import os
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    # Avoid creating or starting an unmanaged PostgreSQL cluster on port 5432.
    Path('/etc/postgresql-common').mkdir(exist_ok=True)
    write('/etc/postgresql-common/createcluster.conf', 'create_main_cluster = false\n')
    run('apt-get', 'update', '-qq')
    run('apt-get', 'install', '-y', '-qq', 'postgresql-16', 'postgresql-client-16',
        'etcd-server', 'etcd-client', 'python3-venv', 'python3-yaml', 'curl')
    run('systemctl', 'disable', '--now', 'postgresql')
    if not Path('/opt/patroni/bin/patroni').exists():
        run('python3', '-m', 'venv', '/opt/patroni')
        run('/opt/patroni/bin/pip', 'install', 'patroni[etcd3]==4.1.3', 'psycopg2-binary==2.9.11')
    run('systemctl', 'stop', 'etcd')
    name, ip = config['name'], config['ip']
    cluster = ','.join(n + '=http://' + addr + ':2380' for n, addr in config['nodes'].items())
    write('/etc/default/etcd', f'''ETCD_NAME="{name}"
ETCD_DATA_DIR="/var/lib/etcd/otus-dz06"
ETCD_LISTEN_PEER_URLS="http://{ip}:2380"
ETCD_LISTEN_CLIENT_URLS="http://{ip}:2379,http://127.0.0.1:2379"
ETCD_INITIAL_ADVERTISE_PEER_URLS="http://{ip}:2380"
ETCD_ADVERTISE_CLIENT_URLS="http://{ip}:2379"
ETCD_INITIAL_CLUSTER="{cluster}"
ETCD_INITIAL_CLUSTER_TOKEN="otus-dz06"
ETCD_INITIAL_CLUSTER_STATE="new"
''')
    import yaml
    write('/etc/patroni.yml', yaml.safe_dump(config['patroni'], sort_keys=False), 0o600)
    run('chown', 'postgres:postgres', '/etc/patroni.yml')
    run('install', '-d', '-o', 'postgres', '-g', 'postgres', '-m', '700', '/var/lib/postgresql/patroni')
    write('/etc/systemd/system/patroni.service', '''[Unit]
Description=PostgreSQL managed by Patroni (DZ06)
Wants=network-online.target
After=network-online.target etcd.service
[Service]
User=postgres
Group=postgres
Type=simple
ExecStart=/opt/patroni/bin/patroni /etc/patroni.yml
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
KillMode=process
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
''')
    run('systemctl', 'daemon-reload')
    run('/opt/patroni/bin/patroni', '--version')
    run('/usr/lib/postgresql/16/bin/postgres', '--version')
    run('etcd', '--version')


if __name__ == '__main__':
    main()
