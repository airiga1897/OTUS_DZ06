"""User commands for the persistent stand and an explicitly destructive test-cycle."""
import argparse
import base64
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
import pool_selection as pools
import topology
import stack_config
import cloud_settings
from cloud_settings import terraform_root
from cloud_contract import InfrastructureResult, validate_binding

import paramiko
from network_diagnostics import BootstrapNetworkError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from selectel_adapter import SelectelAdapter
from credentials import load_credentials, to_environment, credential_values

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
STACK = stack_config.load()
CLOUD = cloud_settings.load(ROOT)
PREFIX = STACK['name_prefix']
TF = ROOT / '.tools/terraform.exe'
RUN = LOCAL / 'runs' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
_AUTH_CONFIG = None
_TEST_CYCLE_ID = None
_TOPOLOGY_OVERRIDE = None


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if path.is_relative_to(ROOT / 'docs'):
        text = redact(text)
    path.write_text(text, encoding='utf-8')


def redact(text):
    values = credential_values(_AUTH_CONFIG)
    secrets_path = LOCAL / 'secrets.json'
    if secrets_path.exists():
        values += list(json.loads(secrets_path.read_text()).values())
    for value in sorted({v for v in values if isinstance(v, str) and v}, key=len, reverse=True):
        text = text.replace(value, '<redacted>')
    return text


def bind_context(config):
    path = LOCAL / 'context.json'
    from selectel_adapter import binding
    expected = binding(config, PREFIX, placement().get('availability_zone'))
    from local_runtime import validate_state
    validate_state(ROOT, config, PREFIX)
    if path.exists():
        validate_binding(json.loads(path.read_text()), expected)
    save(path, expected)


def auth_config():
    global _AUTH_CONFIG
    if _AUTH_CONFIG is not None:
        return _AUTH_CONFIG
    config = load_credentials()
    bind_context(config)
    _AUTH_CONFIG = config
    return config


class Cloud(SelectelAdapter):
    """Bind the selected adapter to this clone's checked local context."""
    def __init__(self, bind=True):
        super().__init__(auth_config() if bind else load_credentials())


def tf(*args, capture=False):
    config = auth_config()
    env = dict(os.environ)
    env.update(to_environment(config))
    for name in list(env):
        if name.startswith('TF_VAR_') or name in {'TF_LOG', 'TF_LOG_CORE', 'TF_LOG_PROVIDER', 'TF_LOG_PATH', 'OS_DEBUG', 'OTUS_SELECTEL_AUTH'}:
            env.pop(name, None)
    env['TF_CLI_CONFIG_FILE'] = str(ROOT / (CLOUD['terraform_directory'] + '.rc'))
    env['TF_IN_AUTOMATION'] = '1'
    binary = str(TF)
    result = subprocess.run([binary, '-chdir=' + str(terraform_root(ROOT)), *args],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    RUN.mkdir(parents=True, exist_ok=True)
    output = redact((result.stdout + result.stderr).decode(errors='replace'))
    (RUN / ('terraform-' + args[0] + '-' + str(time.time_ns()) + '.log')).write_text(output, encoding='utf-8')
    if result.returncode:
        print(output[-5000:], flush=True)
        raise pools.TerraformError(output)
    if capture:
        return result.stdout.decode()
    print('Terraform ' + args[0] + ': OK', flush=True)


def placement():
    return pools.policy(ROOT, 'placement.json', 'context.json')


def desired_topology():
    return _TOPOLOGY_OVERRIDE if _TOPOLOGY_OVERRIDE is not None else topology.load(ROOT)


def preflight():
    from selectel_adapter import preflight as check
    return check(sys.modules[__name__])


def prepare():
    preflight()
    cloud = Cloud()
    choice = pools.selected(ROOT)
    if not choice:
        raise ValueError('Use prepare_selected to discover a complete pool/segment')
    images = [{'id': choice['image_id']}]
    ids = choice['flavor_ids']
    import ipaddress
    import urllib.request
    from operator_network import discover_cidrs, allowed_cidrs
    observed_cidrs = discover_cidrs(placement()['operator_ip_services'])
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
    variables_path = terraform_root(ROOT) / 'terraform.tfvars.json'
    existing_cidrs = json.loads(variables_path.read_text()).get('admin_cidrs', []) if variables_path.exists() else []
    cidrs = allowed_cidrs(placement().get('admin_cidrs', []), observed_cidrs, existing_cidrs)
    save(variables_path, {
        'region': placement()['region'], 'availability_zone': placement()['availability_zone'], 'image_id': images[0]['id'],
        'volume_type': placement()['volume_type'], 'flavor_ids': ids, 'admin_cidrs': cidrs,
        'route_probe_key': json.loads(secrets_path.read_text())['api'],
        'boot_disk_gb': placement()['boot_disk_gb'], 'name_prefix': PREFIX,
        'subnet_cidr': STACK['subnet_cidr'], **desired_topology()})
    tf('init', '-input=false', '-no-color')
    tf('validate', '-no-color')


def checked_plan(destroy=False, allowed_new_nodes=None, access_cidr=None):
    before_ids = managed_ids()
    plan = LOCAL / ('destroy.tfplan' if destroy else 'create.tfplan')
    tf('plan', '-input=false', '-no-color', *(['-destroy'] if destroy else []), '-out=' + str(plan))
    data = json.loads(tf('show', '-json', str(plan), capture=True))
    resources = data.get('prior_state', {}).get('values', {}).get('root_module', {}).get('resources', [])
    resources += data.get('planned_values', {}).get('root_module', {}).get('resources', [])
    for resource in resources:
        if resource.get('mode') == 'managed':
            name = resource.get('values', {}).get('name')
            if name and name != PREFIX and not name.startswith(PREFIX + '-'):
                raise ValueError('Foreign resource in state/plan')
    for change in data.get('resource_changes', []):
        if change.get('mode') == 'data':
            continue
        if not destroy:
            from infrastructure import validate_existing_identity
            validate_existing_identity(change, before_ids)
        actions = change['change']['actions']
        if access_cidr and actions != ['no-op']:
            from operator_network import validate_access_plan
            if actions != ['create']:
                raise ValueError('Route plan cannot alter existing resources')
            validate_access_plan(change, access_cidr,
                                 before_ids.get(('openstack_networking_secgroup_v2', 'lab', '')))
        if actions not in (['no-op'], ['delete'] if destroy else ['create']):
            raise ValueError('Unexpected mutation in plan: ' + change['address'])
        if not destroy and allowed_new_nodes is not None and actions == ['create']:
            from infrastructure import validate_node_creation
            validate_node_creation(change, allowed_new_nodes)
    return plan


def lab_output():
    result = infrastructure_output().application()
    result['site_url'] = 'http://' + result['app_public_ip']
    return result


def infrastructure_output():
    result = InfrastructureResult.from_dict(json.loads(tf('output', '-json', 'infrastructure', capture=True)))
    validate_binding(json.loads((LOCAL / 'context.json').read_text()), result.binding())
    return result


def managed_ids():
    path = terraform_root(ROOT) / 'terraform.tfstate'
    from local_runtime import checked_tree
    checked_tree(path, ROOT.resolve())
    state = json.loads(path.read_text()) if path.exists() else {}
    result = {}
    for resource in state.get('resources', []):
        if resource.get('mode') != 'managed':
            continue
        for instance in resource.get('instances', []):
            attributes = instance.get('attributes', {})
            name = attributes.get('name')
            if name and name != PREFIX and not name.startswith(PREFIX + '-'):
                raise ValueError('Foreign resource in DZ06 state')
            result[(resource['type'], resource['name'], str(instance.get('index_key', '')))] = attributes['id']
    return result


def cleanup(clean_local=True):
    from test_cycle import require_access
    require_access(sys.modules[__name__], cleanup=True)
    config = auth_config()
    from local_runtime import validate_state
    validate_state(ROOT, config, PREFIX)
    pools.phase(ROOT, 'cleanup_pending')
    state_path = terraform_root(ROOT) / 'terraform.tfstate'
    before = json.loads(state_path.read_text()) if state_path.exists() else {}
    own_fips = SelectelAdapter.cleanup_ids(before)
    scope_path = LOCAL / 'cleanup-fips.json'
    region = auth_config()['region']
    ownership = json.loads((LOCAL / 'context.json').read_text()) if (LOCAL / 'context.json').exists() else None
    if own_fips:
        save(scope_path, {**ownership, 'ids': sorted(own_fips)})
    elif scope_path.exists():
        scope = json.loads(scope_path.read_text())
        validate_binding(scope, ownership)
        own_fips = set(scope['ids'])
    if own_fips or any(r.get('mode') == 'managed' and r.get('instances') for r in before.get('resources', [])) or (terraform_root(ROOT) / 'terraform.tfvars.json').exists():
        plan = checked_plan(True)
        tf('apply', '-input=false', '-no-color', str(plan))
        state = json.loads(tf('show', '-json', capture=True))
    else:
        state = {}
    managed = [r for r in state.get('values', {}).get('root_module', {}).get('resources', []) if r.get('mode') == 'managed']
    if managed:
        raise RuntimeError('DZ06 resources remain in state')
    cloud = Cloud()
    from selectel_adapter import audit_cleanup
    remaining, remaining_fips = audit_cleanup(cloud, PREFIX, own_fips)
    save(ROOT / 'docs/cleanup-result.json', {'region': cloud.config['region'],
         'managed_remaining': len(managed), 'named_remaining': remaining,
         'own_floating_ips_remaining': remaining_fips, 'success': not remaining and not remaining_fips})
    if remaining or remaining_fips:
        raise RuntimeError('Named DZ06 resources remain outside state')
    pools.phase(ROOT, 'destroyed')
    if clean_local:
        from local_runtime import cleanup_runtime
        cleanup_runtime(ROOT)
    print('DZ06 cleanup verified; other labs untouched', flush=True)


def trust(lab):
    cloud = Cloud()
    cache_path = LOCAL / 'trusted-hosts.json'
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    lines = []
    for name, server_id in lab['server_ids'].items():
        key = cloud.ssh_host_key(name, server_id, PREFIX, cache.get(server_id))
        paramiko.Ed25519Key(data=base64.b64decode(key))
        cache[server_id] = key
        save(cache_path, cache)
        hosts = [lab['private_ips'][name]] + ([lab['app_public_ip']] if name == 'app' else [])
        lines.append(','.join(hosts) + ' ssh-ed25519 ' + key + '\n')
    (LOCAL / 'known_hosts').write_text(''.join(lines), encoding='utf-8')


def connect(lab, name='app', front=None):
    host = lab['app_public_ip'] if name == 'app' else lab['private_ips'][name]
    client = paramiko.SSHClient()
    client.load_host_keys(str(LOCAL / 'known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    sock = front.get_transport().open_channel('direct-tcpip', (host, 22), ('127.0.0.1', 0)) if front else None
    client.connect(host, port=22, username='otus', key_filename=str(LOCAL / 'otus_dz06'), sock=sock,
                   look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=30)
    if name == 'app' and front is None:
        from operator_network import verify_ssh_source
        try:
            _, output, _ = client.exec_command('printf "%s" "$SSH_CONNECTION"', timeout=20)
            variables = json.loads((terraform_root(ROOT) / 'terraform.tfvars.json').read_text())
            verify_ssh_source(output.read().decode('ascii').strip(), variables['admin_cidrs'])
        except Exception:
            client.close()
            raise
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
        log.write(redact(text))
    if channel.recv_exit_status():
        print(redact(text[-3000:]), flush=True)
        raise RuntimeError('Remote command failed: ' + label)
    print(label + ': OK', flush=True)
    return text


def put(client, path, content, mode=0o600):
    with client.open_sftp() as sftp:
        with sftp.open(path, 'wb') as f:
            f.write(content.encode() if isinstance(content, str) else content)
        sftp.chmod(path, mode)


def normalized_archive_entry(info):
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def upload_tree(client):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for folder in ['app', 'config', 'ansible', 'scripts/remote']:
            for path in (ROOT / folder).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False,
                                filter=lambda info: normalized_archive_entry(info))
    target = STACK['paths']['remote_root']
    put(client, target + '.tar.gz', buffer.getvalue())
    remote(client, 'mkdir -p ' + shlex.quote(target) + ' && tar xzf ' + shlex.quote(target + '.tar.gz') + ' -C ' + shlex.quote(target), 'upload')


def check_placement(lab):
    import verification
    return verification.check_placement(sys.modules[__name__], lab)


def configure(existing=False):
    import configuration
    return configuration.configure(sys.modules[__name__], existing)


def discover_route(lab):
    import urllib.request
    key = json.loads((LOCAL / 'secrets.json').read_text())['api'].encode()
    nonce = secrets.token_hex(24)
    for attempt in range(40):
        try:
            with urllib.request.urlopen(lab['site_url'] + '/?nonce=' + nonce, timeout=3) as response:
                result = json.load(response)
            from operator_network import signed_route_cidr
            cidr = signed_route_cidr(result, nonce, key)
            break
        except (OSError, json.JSONDecodeError):
            time.sleep(2)
    else:
        raise RuntimeError('Direct HTTP route to application VM unavailable')
    path = terraform_root(ROOT) / 'terraform.tfvars.json'
    variables = json.loads(path.read_text())
    if cidr not in variables['admin_cidrs']:
        variables['admin_cidrs'].append(cidr)
        save(path, variables)
        plan = checked_plan(access_cidr=cidr)
        pools.phase(ROOT, 'creating')
        tf('apply', '-input=false', '-no-color', str(plan))
    save(LOCAL / 'operator-network.json', {'tcp_http_cidr': cidr, 'admin_cidrs': variables['admin_cidrs']})
    print('Direct route operator IPv4 verified; SSH remains restricted to /32 entries', flush=True)


def verify(failover=False):
    import verification
    return verification.verify(sys.modules[__name__], failover)


def deploy(*, initial_creation=False):
    from test_cycle import require_access
    require_access(sys.modules[__name__])
    existing = managed_ids()
    saved = pools.selected(ROOT)
    configured = bool(existing) and ((LOCAL / 'configured.json').exists()
        or (LOCAL / 'expansion-pending.json').exists() or not saved or saved.get('phase') == 'active')
    try:
        import infrastructure
        import verification
        desired = desired_topology()
        configured_path = LOCAL / 'configured.json'
        if configured_path.exists():
            saved_config = json.loads(configured_path.read_text())
            if saved_config.get('stack_sha256', stack_config.digest(STACK)) != stack_config.digest(STACK):
                raise ValueError('Changing stack settings on an existing stand requires a separate migration')
        snapshot = None
        if configured:
            previous = lab_output()
            topology.validate_extension(topology.from_lab(previous), desired)
            snapshot = verification.snapshot(sys.modules[__name__], previous)
            infrastructure.check_replication_capacity(snapshot, len(desired['nodes']) - 1)
        added = None
        if snapshot:
            added = set(desired['nodes']) - set(snapshot['server_ids'])
        infrastructure.apply(sys.modules[__name__], desired, allowed_new_nodes=added)
        topology.assert_preserved(existing, managed_ids())
        configure(existing=configured)
        save(LOCAL / 'configured.json', {'portal_configured': True, 'stack_sha256': stack_config.digest(STACK)})
        if snapshot:
            verification.expansion(sys.modules[__name__], snapshot)
        pools.phase(ROOT, 'active')
    except BaseException as error:
        if (not existing and not configured and (initial_creation or _TEST_CYCLE_ID)
                and not (LOCAL / 'configured.json').exists() and not (LOCAL / 'expansion-pending.json').exists()
                and (isinstance(error, BootstrapNetworkError)
                     or pools.scheduler_unavailable(error, ROOT, Cloud, PREFIX))):
            reason = 'guest_IPv4_bootstrap_failed' if isinstance(error, BootstrapNetworkError) else 'scheduler_capacity_unavailable'
            save(ROOT / 'docs/capacity-result.json', {'reason': reason,
                'region': placement()['region'], 'availability_zone': placement()['availability_zone']})
            print('Initial placement failed: ' + reason + '; cleaning before the next candidate', flush=True)
            cleanup(clean_local=False)
            raise pools.CapacityRetry() from error
        diagnostics()
        print('DZ06 state, resources and recovery files preserved after deployment error', flush=True)
        raise



def select_candidate(choice):
    global _AUTH_CONFIG
    config = load_credentials()
    pools.select(ROOT, choice, config['project_id'], 'context.json')
    _AUTH_CONFIG = None


def deployment_candidates():
    policy = json.loads((ROOT / 'config/placement.json').read_text())
    return pools.candidates(policy, load_credentials())


def prepare_selected():
    return pools.run_candidates(deployment_candidates(), select_candidate, prepare, lambda: None)


def deploy_selected():
    from test_cycle import require_access
    require_access(sys.modules[__name__])
    if managed_ids() or (pools.selected(ROOT) and (terraform_root(ROOT) / 'terraform.tfvars.json').exists()):
        deploy()  # Keep the saved placement for repeat configuration.
        return
    if (LOCAL / 'configured.json').exists() or (LOCAL / 'expansion-pending.json').exists():
        raise ValueError('Existing configuration requires recovery; refusing initial placement retry')
    pools.run_candidates(deployment_candidates(), select_candidate, prepare, lambda: deploy(initial_creation=True))


def regression():
    from test_cycle import run
    return run(sys.modules[__name__])


def diagnostics():
    """Best-effort evidence; failures preserve resources and recovery files."""
    try:
        lab = lab_output()
        from network_diagnostics import console_evidence
        console_evidence(sys.modules[__name__], lab)
        with connect(lab) as app:
            remote(app, 'sudo python3 -', 'diagnostics-network-app',
                   stdin=(ROOT / 'scripts/remote/network_diagnostics.py').read_text())
            remote(app, 'sudo journalctl -u haproxy -u otus-dz06 -u nginx -n 100 --no-pager', 'diagnostics-app')
            for name in topology.database_names(lab):
                try:
                    with connect(lab, name, app) as db:
                        remote(db, 'sudo journalctl -u patroni -u etcd -n 150 --no-pager', 'diagnostics-' + name)
                except Exception:
                    print('Diagnostics unavailable for ' + name, flush=True)
    except Exception:
        print('Additional diagnostics unavailable; existing run logs preserved', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'preflight', 'deploy', 'configure', 'verify', 'failover', 'destroy', 'cleanup', 'test', 'test-cycle', 'open'])
    args = parser.parse_args()
    from test_cycle import require_access
    require_access(sys.modules[__name__], cleanup=args.action in ['destroy', 'cleanup'])
    if args.action in ['deploy', 'configure', 'verify', 'failover']:
        from operator_network import check_saved_access
        check_saved_access(terraform_root(ROOT) / 'terraform.tfvars.json',
                           placement()['operator_ip_services'], placement().get('admin_cidrs', []))
    if args.action == 'prepare':
        prepare_selected()
    elif args.action == 'preflight':
        preflight()
    elif args.action == 'open':
        from local_runtime import open_portal
        open_portal(ROOT, 6)
    elif args.action == 'deploy':
        deploy_selected()
    elif args.action in ['test', 'test-cycle']:
        regression()
    elif args.action == 'configure':
        configure()
    elif args.action in ['verify', 'failover']:
        verify(True)
    else:
        cleanup()


if __name__ == '__main__':
    main()
