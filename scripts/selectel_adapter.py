"""Selectel-specific authentication, discovery, placement and resource API operations."""
import json
import re
import time
import urllib.error
import pool_selection as pools
import topology
from selectel_api import authenticate, request_json
from cloud_contract import Capability, CapabilityStatus, InfrastructureResult


def binding(config: dict, namespace: str, zone: str | None) -> dict:
    return dict(provider='selectel', scope={'project_id': config['project_id']}, namespace=namespace,
                placement={'region': config['region'], 'zone': zone})


def partial_state_binding(root, config: dict, prefix: str) -> dict:
    """A failed apply may have no outputs; require the original preparation records."""
    from cloud_contract import validate_binding
    from local_runtime import checked_tree, read
    from cloud_settings import terraform_root
    paths = [root / '.local/context.json', root / '.local/placement.json',
             terraform_root(root) / 'terraform.tfvars.json']
    for path in paths:
        checked_tree(path, root.resolve())
        if not path.is_file():
            raise ValueError('Partial state binding requires context, placement and tfvars; preserve recovery files')
    context, placement, variables = [read(p) for p in paths]
    zone = variables.get('availability_zone')
    if not isinstance(zone, str) or not zone:
        raise ValueError('Partial state binding has no segment')
    expected = binding(config, prefix, zone)
    validate_binding(context, expected)
    saved = dict(provider=placement.get('provider'), namespace=placement.get('namespace'),
                 scope={'project_id': placement.get('project_id')},
                 placement={'region': placement.get('region'), 'zone': placement.get('availability_zone')})
    validate_binding(saved, expected)
    if variables.get('region') != config['region'] or variables.get('name_prefix') != prefix:
        raise ValueError('Partial state binding conflicts with Terraform variables')
    return expected

class SelectelAdapter:
    provider = 'selectel'

    def __init__(self, config):
        self.config = config
        self.token, catalog = authenticate(self.config)
        self.catalog = catalog
        self.endpoints = {s['type']: e['url'].rstrip('/') for s in catalog for e in s['endpoints']
                          if e.get('interface') == 'public' and e.get('region') == self.config['region']
                          and e['url'].startswith('https://')}

    def read_only_pool(self, region):
        other = type(self).__new__(type(self))
        other.config = {**self.config, 'region': region}
        other.token, other.catalog = self.token, self.catalog
        other.endpoints = {s['type']: e['url'].rstrip('/') for s in self.catalog for e in s['endpoints']
                           if e.get('interface') == 'public' and e.get('region') == region
                           and e['url'].startswith('https://')}
        return other

    def resource_regions(self) -> list[str]:
        return sorted({e['region'] for s in self.catalog if s['type'] == 'compute'
                       for e in s['endpoints'] if e.get('interface') == 'public'})


    @staticmethod
    def cleanup_ids(state: dict) -> set[str]:
        return {i['attributes']['id'] for r in state.get('resources', [])
                if r.get('mode') == 'managed' and r['type'] == 'openstack_networking_floatingip_v2'
                for i in r.get('instances', []) if i.get('attributes', {}).get('id')}

    def get(self, service, path, payload=None):
        for attempt in range(3):
            try:
                return request_json(self.endpoints[service] + path, token=self.token, payload=payload)[0]
            except (urllib.error.URLError, TimeoutError) as error:
                # Retrying power actions could repeat a mutation; retry GET only.
                if payload is not None or attempt == 2 or (
                    isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code != 429
                ):
                    raise
                time.sleep(2)

    def set_power(self, name: str, server_id: str, *, running: bool, prefix: str) -> None:
        server = self.get('compute', '/servers/' + server_id)['server']
        if server['name'] != prefix + '-' + name or server.get('tenant_id') != self.config['project_id']:
            raise ValueError('Refusing foreign instance power action')
        self.get('compute', '/servers/' + server_id + '/action', {'os-start' if running else 'os-stop': None})
        target = 'ACTIVE' if running else 'SHUTOFF'
        for _ in range(60):
            if self.get('compute', '/servers/' + server_id)['server']['status'] == target:
                return
            time.sleep(2)
        raise RuntimeError('Instance power transition unconfirmed')

    def ssh_host_key(self, name: str, server_id: str, prefix: str, cached: str | None) -> str:
        server = self.get('compute', '/servers/' + server_id)['server']
        if server['name'] != prefix + '-' + name:
            raise ValueError('Foreign instance UUID')
        if cached is not None:
            return cached
        for _ in range(60):
            console = self.get('compute', '/servers/' + server_id + '/action', {'os-getConsoleOutput': {'length': 1000}})['output']
            section = re.search(r'BEGIN SSH HOST KEY KEYS.*?END SSH HOST KEY KEYS', console, re.S)
            match = re.search(r'ssh-ed25519\s+([A-Za-z0-9+/=]+)', section.group()) if section else None
            if match:
                return match[1]
            time.sleep(3)
        raise RuntimeError('No trusted SSH key for ' + name)


def owned(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + '-')


def audit_cleanup(cloud, prefix: str, own_fips: set[str]) -> tuple[list[str], int]:
    checks = {'compute': ['/servers/detail', '/os-server-groups'], 'volumev3': ['/volumes/detail'],
              'network': ['/v2.0/networks', '/v2.0/subnets', '/v2.0/routers', '/v2.0/ports', '/v2.0/security-groups']}
    deadline = time.monotonic() + 90
    while True:
        remaining = []
        for service, paths in checks.items():
            for path in paths:
                data = cloud.get(service, path)
                remaining += [x['name'] for items in data.values() if isinstance(items, list)
                              for x in items if isinstance(x, dict) and owned(x.get('name', ''), prefix)]
        floating = cloud.get('network', '/v2.0/floatingips')['floatingips']
        count = sum(f['id'] in own_fips for f in floating)
        if not remaining and not count or time.monotonic() >= deadline:
            return remaining, count
        time.sleep(3)


def _extensions(cloud, service: str, path: str) -> set[str] | None:
    try:
        value = cloud.get(service, path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return None
    if not isinstance(value.get('extensions'), list) or any(not isinstance(e.get('alias'), str) for e in value['extensions']):
        raise ValueError('Malformed capability discovery response')
    return {e['alias'] for e in value['extensions']}


def discover_capabilities(cloud) -> dict[str, Capability]:
    """Use Neutron extension aliases and Nova's documented server-group API."""
    network = _extensions(cloud, 'network', '/v2.0/extensions')
    compute = _extensions(cloud, 'compute', '/extensions')
    definitions = {'network_isolation': (network, {'security-group', 'port-security'}),
                   'placement_hints': (network, {'network_availability_zone', 'router_availability_zone'}),
                   'hard_anti_affinity': (compute, {'os-server-groups'})}
    result = {}
    for name, (aliases, required) in definitions.items():
        status = CapabilityStatus.UNKNOWN if aliases is None else (
            CapabilityStatus.SUPPORTED if required <= aliases else CapabilityStatus.UNSUPPORTED)
        result[name] = Capability(status, 'Extension discovery unavailable' if aliases is None
                                  else 'Declared extensions: ' + ', '.join(sorted(required)))
    # A declared extension does not permit suppressing an operation's 404/403/timeout.
    if result['network_isolation'].status == CapabilityStatus.SUPPORTED:
        cloud.get('network', '/v2.0/security-groups')['security_groups']
    if result['hard_anti_affinity'].status == CapabilityStatus.SUPPORTED:
        cloud.get('compute', '/os-server-groups')['server_groups']
    return result


def infrastructure_result(output: dict, config: dict, namespace: str, zone: str) -> InfrastructureResult:
    nodes = {}
    for name, server_id in output['server_ids'].items():
        roles = ['app' if name == 'app' else 'db']
        if name in output['etcd_members']:
            roles.append('etcd')
        nodes[name] = dict(id=server_id, roles=roles, private_ip=output['private_ips'][name],
                           volume_id=output['volume_ids'][name], port_id=output['port_ids'][name])
        if name == 'app':
            nodes[name]['public_ip'] = output['app_public_ip']
    return InfrastructureResult.from_dict(dict(provider='selectel', scope={'project_id': config['project_id']},
        namespace=namespace, placement={'region': config['region'], 'zone': zone}, nodes=nodes,
        subnet_cidr=output['subnet_cidr']))


def preflight(api):
    ROOT, STACK, PREFIX = api.ROOT, api.STACK, api.PREFIX
    Cloud, placement, desired_topology = api.Cloud, api.placement, api.desired_topology
    managed_ids, save = api.managed_ids, api.save
    cloud = Cloud()
    required = json.loads((ROOT / 'config/cloud.json').read_text())['required_capabilities']
    from cloud_contract import require_capabilities
    capabilities = discover_capabilities(cloud)
    try:
        require_capabilities(capabilities, required)
    except ValueError as error:
        raise pools.CandidateUnavailable(str(error)) from error
    project = cloud.config['project_id']
    compute = cloud.get('compute', '/os-quota-sets/' + project + '/detail')['quota_set'].get('az_quotas', {}).get(placement()['availability_zone'])
    if compute is None:
        raise pools.CandidateUnavailable('Compute quotas unavailable in candidate segment')
    volumes = cloud.get('volumev3', '/os-quota-sets/' + project + '?usage=True')['quota_set']
    network = cloud.get('network', '/v2.0/quotas/' + project + '/details.json')['quota']
    profiles = {n: placement()['profiles'][v['role']] for n, v in desired_topology()['nodes'].items()}
    demand = pools.remaining_demand(ROOT, dict(cores=sum(p['vcpus'] for p in profiles.values()),
        ram=sum(p['ram_mb'] for p in profiles.values()), instances=len(profiles),
        disk_gb=placement()['boot_disk_gb'] * len(profiles), volumes=len(profiles),
        floatingip=1, network=1, subnet=1, router=1, security_group=1,
        security_group_rule=3, port=len(profiles) + 3), profiles)
    for key in ('cores', 'ram', 'instances'):
        pools.capacity(compute.get(key), demand[key])
    pools.capacity(volumes.get('gigabytes_' + placement()['volume_type']), demand['disk_gb'])
    for key in ('gigabytes', 'volumes'):
        if key in volumes:
            pools.capacity(volumes[key], demand['disk_gb' if key == 'gigabytes' else 'volumes'])
    for key in ('floatingip', 'network', 'subnet', 'router', 'security_group', 'security_group_rule', 'port'):
        pools.capacity(network.get(key), demand[key])
    subnets = cloud.get('network', '/v2.0/subnets')['subnets']
    if not managed_ids():
        from pool_selection import reject_orphans
        reject_orphans(cloud, PREFIX, subnets)
    import ipaddress
    if any(ipaddress.ip_network(s['cidr']).overlaps(ipaddress.ip_network(STACK['subnet_cidr']))
           for s in subnets if s.get('ip_version') == 4 and not s.get('name', '').startswith(PREFIX)):
        raise pools.CandidateUnavailable('DZ06 subnet overlaps an existing subnet')
    report = {'region': placement()['region'], 'availability_zone': placement()['availability_zone'],
              'quotas_sufficient': True, 'subnet_no_conflict': True,
              'capabilities': {name: value.status.value for name, value in capabilities.items()}}
    save(ROOT / 'docs/preflight.json', report)
    print('Free quotas checked, including concurrent project usage', flush=True)


def check_placement(api, lab):
    Cloud, PREFIX, save, ROOT, auth_config = api.Cloud, api.PREFIX, api.save, api.ROOT, api.auth_config
    cloud = Cloud()
    ids = {lab['server_ids'][name] for name in topology.database_names(lab)}
    groups = cloud.get('compute', '/os-server-groups')['server_groups']
    groups = [g for g in groups if g.get('name') == PREFIX + '-db' and set(g.get('members', [])) == ids]
    if len(groups) != 1 or groups[0].get('policies') != ['anti-affinity']:
        raise RuntimeError('Database nodes must belong to their hard anti-affinity group')
    hosts = []
    for name in topology.database_names(lab):
        server = cloud.get('compute', '/servers/' + lab['server_ids'][name])['server']
        if server['name'] != PREFIX + '-' + name or server['status'] != 'ACTIVE':
            raise RuntimeError('Unexpected database node identity or state')
        hosts.append(server.get('hostId'))
    if not all(hosts) or len(set(hosts)) != len(ids):
        raise RuntimeError('Distinct database host IDs required')
    save(ROOT / 'docs/placement-result.json', {'success': True, 'region': auth_config()['region'],
         'policy': 'anti-affinity', 'database_nodes': len(ids), 'distinct_hosts': len(set(hosts))})


def check_network(cloud, result: InfrastructureResult, admin_cidrs: list[str]) -> dict:
    """Confirm actual filtering, private ports and both network/router placement."""
    zone, prefix = result.placement['zone'], result.namespace
    groups = cloud.get('network', '/v2.0/security-groups')['security_groups']
    groups = [g for g in groups if g.get('name') == prefix and g.get('tenant_id', result.scope['project_id']) == result.scope['project_id']]
    if len(groups) != 1:
        raise RuntimeError('Own security group identity unconfirmed')
    group = groups[0]
    actual = {(r.get('ethertype'), r.get('protocol'), r.get('port_range_min'), r.get('port_range_max'),
               r.get('remote_ip_prefix'), r.get('remote_group_id'))
              for r in group['security_group_rules'] if r['direction'] == 'ingress'}
    expected = {('IPv4', None, None, None, None, group['id']), ('IPv4', 'tcp', 80, 80, '0.0.0.0/0', None)}
    expected |= {('IPv4', 'tcp', 22, 22, cidr, None) for cidr in admin_cidrs}
    if actual != expected:
        raise RuntimeError('Actual ingress rules differ from required network isolation')
    networks, database_ports = set(), set()
    for name, node in result.nodes.items():
        port = cloud.get('network', '/v2.0/ports/' + node['port_id'])['port']
        if port['name'] != prefix + '-' + name or port.get('port_security_enabled') is not True or port['security_groups'] != [group['id']]:
            raise RuntimeError('Node port filtering or identity unconfirmed')
        if {i['ip_address'] for i in port['fixed_ips']} != {node['private_ip']}:
            raise RuntimeError('Unexpected private port addresses')
        networks.add(port['network_id'])
        if 'db' in node['roles']:
            database_ports.add(node['port_id'])
    if len(networks) != 1:
        raise RuntimeError('Nodes do not share the required private network')
    network = cloud.get('network', '/v2.0/networks/' + networks.pop())['network']
    routers = cloud.get('network', '/v2.0/routers')['routers']
    routers = [r for r in routers if r.get('name') == prefix]
    if network.get('shared') is not False or network.get('router:external') is not False or network.get('port_security_enabled') is not True:
        raise RuntimeError('Private network isolation unconfirmed')
    if len(routers) != 1 or any(set(x.get('availability_zone_hints', [])) != {zone} or set(x.get('availability_zones', [])) != {zone}
                                for x in (network, *routers)):
        raise RuntimeError('Actual network/router placement unconfirmed')
    if any(f.get('port_id') in database_ports for f in cloud.get('network', '/v2.0/floatingips')['floatingips']):
        raise RuntimeError('Database ports must not have public addresses')
    return dict(success=True, private_ports_filtered=True, ingress_rules_confirmed=True,
                database_public_ips_absent=True, network_router_zone_confirmed=True)




def reject_cloud_resources(api, cloud) -> None:
    paths = {'compute': ['/servers/detail', '/os-server-groups'], 'volumev3': ['/volumes/detail'],
             'network': ['/v2.0/networks', '/v2.0/subnets', '/v2.0/routers', '/v2.0/ports', '/v2.0/security-groups']}
    for service, urls in paths.items():
        if service not in cloud.endpoints:
            continue  # Audit every available service; image discovery is not needed for reads.
        for url in urls:
            try:
                data = cloud.get(service, url)
            except urllib.error.HTTPError as error:
                if service != 'network' or url != '/v2.0/security-groups' or error.code != 404:
                    raise
                extensions = cloud.get('network', '/v2.0/extensions')['extensions']
                if any(e['alias'] == 'security-group' for e in extensions):
                    raise
                continue  # The catalog can expose Neutron without security-group support.
            if any(isinstance(item, dict) and owned(item.get('name', ''), api.PREFIX)
                   for items in data.values() if isinstance(items, list) for item in items):
                raise RuntimeError('Existing DZ06 cloud resources require separate recovery')



def validate_node_creation(change: dict, allowed: set[str]) -> None:
    kinds = {'openstack_compute_instance_v2': 'node', 'openstack_networking_port_v2': 'node',
             'openstack_blockstorage_volume_v3': 'boot'}
    if change.get('type') not in kinds or change.get('name') != kinds[change['type']] or change.get('index') not in allowed:
        raise ValueError('Extension may only create resources of new nodes: ' + change['address'])



def validate_managed_state(state: dict, config: dict, prefix: str) -> None:
    for resource in state.get('resources', []):
        if resource.get('mode') != 'managed' or not resource.get('instances'):
            continue
        if not resource.get('type', '').startswith('openstack_'):
            raise ValueError('Foreign provider in state')
        for item in resource['instances']:
            attrs = item.get('attributes', {})
            name = attrs.get('name')
            if name and name != prefix and not name.startswith(prefix + '-'):
                raise ValueError('Foreign resource in state')
            if attrs.get('tenant_id') and attrs['tenant_id'] != config['project_id']:
                raise ValueError('Foreign project in state')
            if attrs.get('region') and attrs['region'] != config['region']:
                raise ValueError('Foreign pool in state')
