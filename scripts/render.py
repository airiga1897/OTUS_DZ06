"""Генерация конфигураций Patroni/etcd без фиксированных паролей."""


def node_config(name, lab, passwords):
    ip = lab['private_ips'][name]
    nodes = {n: lab['private_ips'][n] for n in ['db1', 'db2', 'db3']}
    return {'name': name, 'ip': ip, 'nodes': nodes,
            'patroni': {
                'scope': 'otus-dz06', 'namespace': '/service/', 'name': name,
                'restapi': {'listen': ip + ':8008', 'connect_address': ip + ':8008',
                            'authentication': {'username': 'patroni', 'password': passwords['api']}},
                'etcd3': {'hosts': [v + ':2379' for v in nodes.values()]},
                'bootstrap': {'dcs': {'ttl': 30, 'loop_wait': 5, 'retry_timeout': 5,
                    'maximum_lag_on_failover': 1048576, 'synchronous_mode': True,
                    'synchronous_mode_strict': True, 'synchronous_node_count': 1,
                    'postgresql': {'use_pg_rewind': True, 'use_slots': True,
                        'parameters': {'wal_level': 'replica', 'hot_standby': 'on',
                                       'max_wal_senders': 10, 'max_replication_slots': 10,
                                       'wal_keep_size': '128MB', 'password_encryption': 'scram-sha-256'}}},
                    'initdb': [{'encoding': 'UTF8'}, 'data-checksums'],
                    'pg_hba': ['local all postgres peer', 'local all all scram-sha-256',
                               'host replication replicator 10.66.0.0/24 scram-sha-256',
                               'host all all 10.66.0.0/24 scram-sha-256']},
                'postgresql': {'listen': ip + ':5432', 'connect_address': ip + ':5432',
                    'data_dir': '/var/lib/postgresql/patroni', 'bin_dir': '/usr/lib/postgresql/16/bin',
                    'authentication': {'superuser': {'username': 'postgres', 'password': passwords['postgres']},
                                       'replication': {'username': 'replicator', 'password': passwords['replicator']}},
                    'parameters': {'unix_socket_directories': '/var/run/postgresql'}},
                'watchdog': {'mode': 'off'}, 'tags': {'nofailover': False, 'noloadbalance': False}}}


if __name__ == '__main__':
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[1]
    lab = {'private_ips': {'app': '10.66.0.10', 'db1': '10.66.0.21', 'db2': '10.66.0.22', 'db3': '10.66.0.23'}}
    placeholder = {key: 'REPLACE_WITH_GENERATED_SECRET' for key in ['postgres', 'replicator', 'api']}
    (root / 'config/patroni.example.yml').write_text(yaml.safe_dump(node_config('db1', lab, placeholder)['patroni'], sort_keys=False), encoding='utf-8')
