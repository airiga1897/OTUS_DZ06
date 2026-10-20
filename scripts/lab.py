"""Изолированный жизненный цикл ДЗ06; test всегда выполняет cleanup в finally."""
import argparse
import base64
import concurrent.futures
import datetime
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
import urllib.error
import uuid

import paramiko
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from selectel_api import authenticate, request_json

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
PREFIX = 'otus-dz06'
PROJECT = '<SELECTEL_PROJECT_ID>'
TF = ROOT.parent / 'OTUS_DZ04/.tools/terraform.exe'
RUN = LOCAL / 'runs' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
_AUTH_CONFIG = None


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def auth_config():
    global _AUTH_CONFIG
    if _AUTH_CONFIG is not None:
        return _AUTH_CONFIG
    path = os.environ.get('OTUS_SELECTEL_AUTH')
    if path:
        config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    elif os.environ.get('OS_PASSWORD'):
        from selectel_api import config_from_env
        config = config_from_env()
    else:
        raise RuntimeError('Set OTUS_SELECTEL_AUTH to local operator JSON or load OS_* environment')
    if config['project_id'] != PROJECT or config['region'] != 'ru-9':
        raise ValueError('Unexpected project/region')
    _AUTH_CONFIG = config
    return config


class Cloud:
    def __init__(self):
        self.config = auth_config()
        self.token, catalog = authenticate(self.config)
        self.endpoints = {s['type']: e['url'].rstrip('/') for s in catalog for e in s['endpoints']
                          if e.get('interface') == 'public' and e.get('region') == 'ru-9'
                          and e['url'].startswith('https://')}

    def get(self, service, path, payload=None):
        return request_json(self.endpoints[service] + path, token=self.token, payload=payload)[0]


def tf(*args, capture=False):
    config = auth_config()
    env = dict(os.environ)
    for key, field in {'OS_AUTH_URL': 'auth_url', 'OS_PROJECT_ID': 'project_id',
                       'OS_REGION_NAME': 'region', 'OS_USER_DOMAIN_NAME': 'domain_name',
                       'OS_USERNAME': 'username', 'OS_PASSWORD': 'password'}.items():
        env[key] = config[field]
    env['TF_CLI_CONFIG_FILE'] = str(ROOT / 'terraform-selectel.rc')
    env['TF_IN_AUTOMATION'] = '1'
    binary = os.environ.get('OTUS_TERRAFORM', str(TF))
    result = subprocess.run([binary, '-chdir=' + str(ROOT / 'terraform-selectel'), *args],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    RUN.mkdir(parents=True, exist_ok=True)
    (RUN / ('terraform-' + args[0] + '-' + str(time.time_ns()) + '.log')).write_bytes(result.stdout + result.stderr)
    if result.returncode:
        print((result.stdout + result.stderr).decode(errors='replace')[-5000:], flush=True)
        raise RuntimeError('Terraform ' + args[0] + ' failed; see local run log')
    if capture:
        return result.stdout.decode()
    print('Terraform ' + args[0] + ': OK', flush=True)


def preflight():
    cloud = Cloud()
    compute = cloud.get('compute', '/os-quota-sets/' + PROJECT + '/detail')['quota_set']['az_quotas']['ru-9a']
    volumes = cloud.get('volumev3', '/os-quota-sets/' + PROJECT + '?usage=True')['quota_set']
    network = cloud.get('network', '/v2.0/quotas/' + PROJECT + '/details.json')['quota']
    demands = [(compute['cores'], 7), (compute['ram'], 14336), (compute['instances'], 4),
               (volumes['gigabytes_basicssd.ru-9a'], 80), (network['floatingip'], 1),
               (network['network'], 1), (network['subnet'], 1), (network['router'], 1),
               (network['security_group'], 1), (network['security_group_rule'], 3), (network['port'], 7)]
    for quota, needed in demands:
        free = quota['limit'] - quota.get('in_use', quota.get('used', 0)) - quota.get('reserved', 0)
        if quota['limit'] != -1 and free < needed:
            raise RuntimeError('Insufficient free quota; no resources created')
    subnets = cloud.get('network', '/v2.0/subnets')['subnets']
    import ipaddress
    if any(ipaddress.ip_network(s['cidr']).overlaps(ipaddress.ip_network('10.66.0.0/24'))
           for s in subnets if s.get('ip_version') == 4 and not s.get('name', '').startswith(PREFIX)):
        raise RuntimeError('DZ06 subnet overlaps an existing subnet')
    report = {'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'compute': compute, 'ssd': volumes['gigabytes_basicssd.ru-9a'],
              'network': network, 'required': {'instances': 4, 'cores': 7, 'ram_mib': 14336, 'ssd_gb': 80}}
    save(ROOT / 'docs/preflight.json', report)
    print('Free quotas checked, including concurrent project usage', flush=True)


def prepare():
    preflight()
    cloud = Cloud()
    image_base = cloud.endpoints['image'].removesuffix('/v2')
    images = request_json(image_base + '/v2/images?name=Ubuntu%2024.04%20LTS%2064-bit&status=active', token=cloud.token)[0]
    images = [i for i in images['images'] if i.get('visibility') == 'public' and i.get('min_disk', 0) <= 20]
    if len(images) != 1:
        raise ValueError('Expected one compatible public Ubuntu image')
    flavors = cloud.get('compute', '/flavors/detail')['flavors']
    ids = {}
    for role, name in {'app': 'SL1.1-2048', 'db': 'SL1.2-4096'}.items():
        matches = [f for f in flavors if f['name'] == name]
        if len(matches) != 1:
            raise ValueError('Flavor missing: ' + name)
        ids[role] = matches[0]['id']
    import ipaddress
    import urllib.request
    ip = urllib.request.urlopen('https://api.ipify.org', timeout=15).read().decode().strip()
    if ipaddress.ip_address(ip).version != 4:
        raise ValueError('IPv4 operator required')
    LOCAL.mkdir(exist_ok=True)
    key, pub = LOCAL / 'otus_dz06', LOCAL / 'otus_dz06.pub'
    if key.exists() != pub.exists():
        raise ValueError('Incomplete existing key pair')
    if not key.exists():
        private = Ed25519PrivateKey.generate()
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
        pub.write_bytes(private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH) + b' otus-dz06\n')
    secrets_path = LOCAL / 'secrets.json'
    if not secrets_path.exists():
        save(secrets_path, {k: secrets.token_urlsafe(30) for k in ['postgres', 'replicator', 'app', 'django', 'api']})
    variables_path = ROOT / 'terraform-selectel/terraform.tfvars.json'
    existing_cidrs = json.loads(variables_path.read_text()).get('admin_cidrs', []) if variables_path.exists() else []
    if any(ipaddress.ip_network(c).prefixlen != 32 for c in existing_cidrs):
        raise ValueError('Only explicit operator IPv4 /32 addresses are supported')
    cidrs = list(dict.fromkeys(existing_cidrs + [ip + '/32']))
    save(variables_path, {
        'region': 'ru-9', 'availability_zone': 'ru-9a', 'image_id': images[0]['id'],
        'volume_type': 'basicssd.ru-9a', 'flavor_ids': ids, 'admin_cidrs': cidrs,
        'route_probe_key': json.loads(secrets_path.read_text())['api']})
    tf('init', '-input=false', '-no-color')
    tf('validate', '-no-color')


def checked_plan(destroy=False):
    plan = LOCAL / ('destroy.tfplan' if destroy else 'create.tfplan')
    tf('plan', '-input=false', '-no-color', *(['-destroy'] if destroy else []), '-out=' + str(plan))
    data = json.loads(tf('show', '-json', str(plan), capture=True))
    resources = data.get('prior_state', {}).get('values', {}).get('root_module', {}).get('resources', [])
    resources += data.get('planned_values', {}).get('root_module', {}).get('resources', [])
    for resource in resources:
        if resource.get('mode') == 'managed':
            name = resource.get('values', {}).get('name')
            if name and not name.startswith(PREFIX):
                raise ValueError('Foreign resource in state/plan')
    for change in data.get('resource_changes', []):
        if change.get('mode') == 'data':
            continue
        actions = change['change']['actions']
        if actions not in (['no-op'], ['delete'] if destroy else ['create']):
            raise ValueError('Unexpected mutation in plan: ' + change['address'])
    return plan


def lab_output():
    return json.loads(tf('output', '-json', 'lab', capture=True))


def managed_ids():
    path = ROOT / 'terraform-selectel/terraform.tfstate'
    state = json.loads(path.read_text()) if path.exists() else {}
    result = {}
    for resource in state.get('resources', []):
        if resource.get('mode') != 'managed':
            continue
        for instance in resource.get('instances', []):
            attributes = instance.get('attributes', {})
            name = attributes.get('name')
            if name and not name.startswith(PREFIX):
                raise ValueError('Foreign resource in DZ06 state')
            result[(resource['type'], resource['name'], str(instance.get('index_key', '')))] = attributes['id']
    return result


def cleanup():
    state_path = ROOT / 'terraform-selectel/terraform.tfstate'
    before = json.loads(state_path.read_text()) if state_path.exists() else {}
    own_fips = {i['attributes']['id'] for r in before.get('resources', [])
                if r.get('mode') == 'managed' and r['type'] == 'openstack_networking_floatingip_v2'
                for i in r.get('instances', []) if i.get('attributes', {}).get('id')}
    plan = checked_plan(True)
    tf('apply', '-input=false', '-no-color', str(plan))
    state = json.loads(tf('show', '-json', capture=True))
    managed = [r for r in state.get('values', {}).get('root_module', {}).get('resources', []) if r.get('mode') == 'managed']
    if managed:
        raise RuntimeError('DZ06 resources remain in state')
    cloud = Cloud()
    checks = {'compute': ['/servers/detail', '/os-server-groups'], 'volumev3': ['/volumes/detail'],
              'network': ['/v2.0/networks', '/v2.0/subnets', '/v2.0/routers', '/v2.0/ports', '/v2.0/security-groups']}
    deadline = time.monotonic() + 90
    while True:
        remaining = []
        for service, paths in checks.items():
            for path in paths:
                data = cloud.get(service, path)
                for items in data.values():
                    if isinstance(items, list):
                        remaining += [x.get('name') for x in items if isinstance(x, dict) and x.get('name', '').startswith(PREFIX)]
        floating = cloud.get('network', '/v2.0/floatingips')['floatingips']
        remaining_fips = sum(f['id'] in own_fips for f in floating)
        if not remaining and not remaining_fips or time.monotonic() >= deadline:
            break
        time.sleep(3)
    save(ROOT / 'docs/cleanup-result.json', {'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
         'managed_remaining': len(managed), 'named_remaining': remaining,
         'own_floating_ips_remaining': remaining_fips, 'success': not remaining and not remaining_fips})
    if remaining or remaining_fips:
        raise RuntimeError('Named DZ06 resources remain outside state')
    print('DZ06 cleanup verified; other labs untouched', flush=True)


def trust(lab):
    cloud = Cloud()
    cache_path = LOCAL / 'trusted-hosts.json'
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    lines = []
    for name, server_id in lab['server_ids'].items():
        server = cloud.get('compute', '/servers/' + server_id)['server']
        if server['name'] != PREFIX + '-' + name:
            raise ValueError('Foreign instance UUID')
        key = cache.get(server_id)
        if key is None:
            for attempt in range(60):
                console = cloud.get('compute', '/servers/' + server_id + '/action', {'os-getConsoleOutput': {'length': 1000}})['output']
                section = re.search(r'BEGIN SSH HOST KEY KEYS.*?END SSH HOST KEY KEYS', console, re.S)
                match = re.search(r'ssh-ed25519\s+([A-Za-z0-9+/=]+)', section.group()) if section else None
                if match:
                    key = match[1]
                    break
                time.sleep(3)
            else:
                raise RuntimeError('No trusted SSH key for ' + name)
        paramiko.Ed25519Key(data=base64.b64decode(key))
        cache[server_id] = key
        save(cache_path, cache)
        hosts = [lab['private_ips'][name]] + (['[' + lab['app_public_ip'] + ']:443'] if name == 'app' else [])
        lines.append(','.join(hosts) + ' ssh-ed25519 ' + key + '\n')
    (LOCAL / 'known_hosts').write_text(''.join(lines), encoding='utf-8')


def connect(lab, name='app', front=None):
    host = lab['app_public_ip'] if name == 'app' else lab['private_ips'][name]
    client = paramiko.SSHClient()
    client.load_host_keys(str(LOCAL / 'known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    sock = front.get_transport().open_channel('direct-tcpip', (host, 22), ('127.0.0.1', 0)) if front else None
    client.connect(host, port=443 if name == 'app' else 22, username='otus', key_filename=str(LOCAL / 'otus_dz06'), sock=sock,
                   look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=30)
    return client


def remote(client, command, label='remote', stdin=None):
    print(label + ': started', flush=True)
    channel = client.get_transport().open_session(timeout=30)
    channel.set_combine_stderr(True)
    channel.exec_command(command)
    if stdin is not None:
        channel.sendall(stdin.encode() if isinstance(stdin, str) else stdin)
        channel.shutdown_write()
    out = bytearray()
    deadline = time.monotonic() + 1800
    while not channel.exit_status_ready() or channel.recv_ready():
        if time.monotonic() > deadline:
            channel.close()
            raise TimeoutError('Remote command timeout: ' + label)
        if channel.recv_ready():
            out.extend(channel.recv(65536))
        else:
            time.sleep(0.1)
    text = out.decode(errors='replace')
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / (label + '.log')).open('a', encoding='utf-8') as log:
        log.write(text)
    if channel.recv_exit_status():
        print(text[-3000:], flush=True)
        raise RuntimeError('Remote command failed: ' + label)
    print(label + ': OK', flush=True)
    return text


def put(client, path, content, mode=0o600):
    with client.open_sftp() as sftp:
        with sftp.open(path, 'wb') as f:
            f.write(content.encode() if isinstance(content, str) else content)
        sftp.chmod(path, mode)


def upload_tree(client):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for folder in ['app', 'config', 'scripts/remote']:
            for path in (ROOT / folder).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    archive.add(path, arcname=path.relative_to(ROOT).as_posix())
    put(client, '/home/otus/dz06.tar.gz', buffer.getvalue())
    remote(client, 'mkdir -p /home/otus/dz06 && tar xzf /home/otus/dz06.tar.gz -C /home/otus/dz06', 'upload')


def configure():
    from render import node_config
    lab = lab_output()
    trust(lab)
    try:
        with connect(lab):
            pass
    except (OSError, paramiko.SSHException):
        discover_route(lab)
    passwords = json.loads((LOCAL / 'secrets.json').read_text())
    with connect(lab) as app:
        remote(app, 'sudo python3 -', 'cloud-init-app', stdin=(ROOT / 'scripts/remote/check_cloud_init.py').read_text())
        upload_tree(app)
        put(app, '/home/otus/dz06/settings.json', json.dumps({'lab': lab, 'passwords': passwords}))
        def install(name):
            with connect(lab, name, app) as db:
                remote(db, 'sudo python3 -', 'cloud-init-' + name, stdin=(ROOT / 'scripts/remote/check_cloud_init.py').read_text())
                upload_tree(db)
                put(db, '/home/otus/dz06/node.json', json.dumps(node_config(name, lab, passwords)))
                remote(db, 'sudo python3 /home/otus/dz06/scripts/remote/database.py install', 'install-' + name)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(install, ['db1', 'db2', 'db3']))
        # Start all etcd members only after all peer configs are present.
        for name in ['db1', 'db2', 'db3']:
            with connect(lab, name, app) as db:
                remote(db, 'sudo systemctl enable etcd && sudo systemctl start --no-block etcd', 'etcd-' + name)
        for name in ['db1', 'db2', 'db3']:
            with connect(lab, name, app) as db:
                remote(db, 'sudo systemctl enable --now patroni', 'patroni-' + name)
        remote(app, 'sudo python3 /home/otus/dz06/scripts/remote/application.py', 'application')
    print('Cluster and portal configured', flush=True)


def discover_route(lab):
    import hashlib
    import hmac
    import ipaddress
    import urllib.request
    key = json.loads((LOCAL / 'secrets.json').read_text())['api'].encode()
    nonce = secrets.token_hex(24)
    for attempt in range(40):
        try:
            with urllib.request.urlopen(lab['site_url'] + '/?nonce=' + nonce, timeout=3) as response:
                result = json.load(response)
            expected = hmac.new(key, (nonce + '|' + result['ip']).encode(), hashlib.sha256).hexdigest()
            if result['nonce'] != nonce or not hmac.compare_digest(expected, result['signature']):
                raise ValueError('Untrusted route probe response')
            address = ipaddress.IPv4Address(result['ip'])
            if not address.is_global:
                raise ValueError('Operator source must be a global IPv4')
            break
        except (OSError, json.JSONDecodeError):
            time.sleep(2)
    else:
        raise RuntimeError('Direct HTTP route to application VM unavailable')
    path = ROOT / 'terraform-selectel/terraform.tfvars.json'
    variables = json.loads(path.read_text())
    cidr = str(address) + '/32'
    if cidr not in variables['admin_cidrs']:
        variables['admin_cidrs'].append(cidr)
        save(path, variables)
        plan = checked_plan()
        tf('apply', '-input=false', '-no-color', str(plan))
    print('Direct route operator IPv4 verified; SSH remains restricted to /32 entries', flush=True)


def verify(failover=False):
    lab = lab_output()
    run_id = uuid.uuid4().hex
    with connect(lab) as app:
        command = 'sudo python3 /home/otus/dz06/scripts/remote/verify.py '
        raw = remote(app, command + 'baseline ' + run_id, 'verify-baseline')
        baseline = json.loads(raw)
        save(ROOT / 'docs/verification.json', baseline)
        if not failover:
            return {'baseline': baseline}
        leader = baseline['leader']
        server_id = lab['server_ids'][leader]
        cloud = Cloud()
        server = cloud.get('compute', '/servers/' + server_id)['server']
        if server['name'] != PREFIX + '-' + leader:
            raise ValueError('Refusing foreign instance shutdown')
        try:
            cloud.get('compute', '/servers/' + server_id + '/action', {'os-stop': None})
            for _ in range(60):
                if cloud.get('compute', '/servers/' + server_id)['server']['status'] == 'SHUTOFF':
                    break
                time.sleep(2)
            else:
                raise RuntimeError('Leader shutdown unconfirmed')
            raw = remote(app, command + 'failover ' + run_id + ' ' + leader, 'verify-failover')
            result = json.loads(raw)
            result['stopped_node'] = leader
            result['nova_shutdown_confirmed'] = True
            save(ROOT / 'docs/failover-result.json', result)
        finally:
            cloud.get('compute', '/servers/' + server_id + '/action', {'os-start': None})
        raw = remote(app, command + 'recovered ' + run_id, 'verify-recovered')
        recovery = json.loads(raw)
        save(ROOT / 'docs/recovery-result.json', recovery)
    print('Failover, committed data, HTTP and recovered replica verified', flush=True)
    return {'run_id': run_id, 'baseline': baseline, 'failover': result, 'recovery': recovery}


def deploy():
    existing = managed_ids()
    try:
        preflight()
        plan = checked_plan()
        tf('apply', '-input=false', '-no-color', str(plan))
        configure()
    except BaseException:
        diagnostics()
        if not existing:
            cleanup()
        else:
            print('Existing DZ06 resources preserved after deployment error', flush=True)
        raise


def regression():
    try:
        deploy()
        before = managed_ids()
        first = verify(True)
        deploy()
        after = managed_ids()
        if before != after:
            raise RuntimeError('Redeployment replaced DZ06 resource identities')
        # Re-run the first run's recovery check after configuration, including its exact rows.
        with connect(lab_output()) as app:
            raw = remote(app, 'sudo python3 /home/otus/dz06/scripts/remote/verify.py recovered ' + first['run_id'], 'verify-redeployment-data')
            data_check = json.loads(raw)
        second = verify(True)
        records = [r[stage]['new_record'] for r in [first, second] for stage in ['baseline', 'failover', 'recovery']]
        records.append(data_check['new_record'])
        if first['run_id'] == second['run_id'] or any(len({r[key] for r in records}) != 7 for key in ['id', 'slug', 'body']):
            raise RuntimeError('Failover runs reused verification records')
        save(ROOT / 'docs/redeployment-result.json', {
            'success': True, 'resource_ids_preserved': True, 'original_records_preserved': True,
            'repeat_configuration_completed': True, 'existing_portal_without_route_probe': True,
            'data_check': data_check})
        save(ROOT / 'docs/failover-runs.json', {'success': True, 'distinct_failover_records': 6,
             'redeployment_record_is_distinct': True, 'runs': [first, second]})
    finally:
        cleanup()


def diagnostics():
    """Best-effort evidence before destroying a failed configured stand."""
    if not (RUN / 'cloud-init-app.log').exists():
        return
    try:
        lab = lab_output()
        with connect(lab) as app:
            remote(app, 'sudo journalctl -u haproxy -u otus-dz06 -u nginx -n 100 --no-pager', 'diagnostics-app')
            for name in ['db1', 'db2', 'db3']:
                try:
                    with connect(lab, name, app) as db:
                        remote(db, 'sudo journalctl -u patroni -u etcd -n 150 --no-pager', 'diagnostics-' + name)
                except Exception:
                    print('Diagnostics unavailable for ' + name, flush=True)
    except Exception:
        print('Additional diagnostics unavailable; existing run logs preserved', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'preflight', 'deploy', 'configure', 'verify', 'failover', 'destroy', 'cleanup', 'test'])
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'preflight':
        preflight()
    elif args.action == 'deploy':
        deploy()
    elif args.action == 'test':
        regression()
    elif args.action == 'configure':
        configure()
    elif args.action in ['verify', 'failover']:
        verify(True)
    else:
        cleanup()


if __name__ == '__main__':
    main()
