"""Selectel pool discovery and capacity retry; adapted from DZ05, clone-local."""
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
from selectel_api import authenticate, request_json

SERVICES = ('compute', 'image', 'volumev3', 'network')


class CandidateUnavailable(RuntimeError):
    pass


class CapacityRetry(RuntimeError):
    """Previous partial deployment has passed both deletion audits."""


class TerraformError(RuntimeError):
    def __init__(self, output):
        super().__init__('Terraform failed; inspect the private local log')
        self.output = output


def endpoints_for(catalog, region):
    endpoints = {s['type']: e['url'].rstrip('/') for s in catalog for e in s['endpoints']
                 if e.get('interface') == 'public' and e.get('region') == region
                 and e['url'].startswith('https://')}
    if any(kind not in endpoints for kind in SERVICES):
        raise CandidateUnavailable('Required services unavailable in pool ' + region)
    return endpoints


def ordered_pools(config, catalog):
    excluded = set(config.get('excluded_pools', []))
    if any(not re.fullmatch(r'ru-\d+', pool) for pool in excluded):
        raise ValueError('Invalid excluded pool')
    available = {e['region'] for s in catalog if s['type'] == 'compute' for e in s['endpoints']
                 if e.get('interface') == 'public' and re.fullmatch(r'ru-\d+', e.get('region', ''))}
    order = []
    for requested in config['pools']:
        choices = sorted(available, key=lambda p: int(p.split('-')[1]), reverse=True) if requested == 'auto' else [requested]
        for pool in choices:
            if pool in available and pool not in excluded and pool not in order:
                try:
                    endpoints_for(catalog, pool)
                except CandidateUnavailable:
                    continue
                order.append(pool)
    return order


def compatible_images(images, config):
    checksum = config.get('image_checksum')
    if checksum is not None and not re.fullmatch(r'[a-f0-9]{32}', checksum):
        raise ValueError('Configure the verified Glance image checksum')
    return [image for image in images if image['name'] == config['image_name']
            and image.get('visibility') == 'public'
            and image.get('min_disk', 0) <= config['boot_disk_gb']
            and image.get('min_ram', 0) <= min(p['ram_mb'] for p in config['profiles'].values())
            and image.get('architecture', 'x86_64') in ('x86_64', 'amd64')
            and (checksum is None or image.get('checksum') == checksum)]


def ordered_zones(config, zones):
    preferred = config.get('preferred_segments', [])
    if any(not re.fullmatch(r'ru-\d+[a-z]', zone) for zone in preferred):
        raise ValueError('Invalid preferred segment')
    return sorted(zones, key=lambda zone: (
        preferred.index(zone) if zone in preferred else len(preferred), zone))


def image_catalog(endpoint, token):
    url = endpoint.removesuffix('/v2') + '/v2/images?status=active'
    original, seen, images = urllib.parse.urlsplit(url), set(), []
    while url:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme, parsed.netloc, parsed.path) != (original.scheme, original.netloc, original.path) or url in seen:
            raise ValueError('Unsafe or cyclic image catalog pagination')
        seen.add(url)
        page = request_json(url, token=token)[0]
        images.extend(page['images'])
        if page.get('next'):
            following = urllib.parse.urlsplit(urllib.parse.urljoin(url, page['next']))
            if (following.scheme, following.netloc) != (original.scheme, original.netloc) or following.path not in (original.path, '/v2/images'):
                raise ValueError('Unsafe image catalog pagination')
            url = urllib.parse.urlunsplit((original.scheme, original.netloc, original.path, following.query, ''))
        else:
            url = None
    return images


def candidates(config, credentials):
    classes = config.get('disk_classes', [config.get('disk_class')])
    if (not isinstance(classes, list) or not classes
            or any(not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9]*', value) for value in classes)
            or len(set(classes)) != len(classes)):
        raise ValueError('Configure unique disk_classes in priority order')
    if not config.get('pools') or any(p != 'auto' and not re.fullmatch(r'ru-\d+', p) for p in config['pools']):
        raise ValueError('Configure Russian pools or auto')
    if config.get('segment') and not re.fullmatch(r'ru-\d+[a-z]', config['segment']):
        raise ValueError('Invalid configured segment')
    token, catalog = authenticate(credentials)
    for pool in ordered_pools(config, catalog):
        endpoints = endpoints_for(catalog, pool)
        try:
            def get(kind, path):
                return request_json(endpoints[kind] + path, token=token)[0]
            images = image_catalog(endpoints['image'], token)
            images = compatible_images(images, config)
            if not images:
                raise CandidateUnavailable('No matching verified image')
            images.sort(key=lambda i: (i.get('created_at', ''), i['id']), reverse=True)
            flavors = get('compute', '/flavors/detail')['flavors']
            ids = {}
            for role, profile in config['profiles'].items():
                wanted = config['flavor_name_template'].format(**profile)
                matches = [f for f in flavors if f['name'] == wanted and f['vcpus'] == profile['vcpus'] and f['ram'] == profile['ram_mb']]
                if len(matches) != 1:
                    raise CandidateUnavailable('No unique flavor for ' + role)
                ids[role] = matches[0]['id']
            types = {t['name'] for t in get('volumev3', '/types')['volume_types']}
            zones = get('compute', '/os-availability-zone')['availabilityZoneInfo']
            network_zones = get('network', '/v2.0/availability_zones')['availability_zones']
            supported = {r: {z['name'] for z in network_zones if z['resource'] == r and z['state'] == 'available'}
                         for r in ('network', 'router')}
            for zone in ordered_zones(config, (z['zoneName'] for z in zones if z['zoneState']['available']
                               and re.fullmatch(re.escape(pool) + r'[a-z]', z['zoneName'])
                               and z['zoneName'] in supported['network'] & supported['router'])):
                if config.get('segment') and config['segment'] != zone:
                    continue
                disk = next((kind + '.' + zone for kind in classes if kind + '.' + zone in types), None)
                if disk is not None:
                    yield dict(region=pool, availability_zone=zone, volume_type=disk,
                               image_id=images[0]['id'], flavor_ids=ids)
        except CandidateUnavailable as error:
            print(pool + ': ' + str(error) + '; skipped', flush=True)


def selected(root):
    path = Path(root) / '.local/placement.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def policy(root, filename, context_name):
    root = Path(root)
    data = json.loads((root / 'config' / filename).read_text(encoding='utf-8-sig'))
    preferred = next((p for p in data['pools'] if p != 'auto'), None)
    data['region'] = preferred
    saved = selected(root)
    if saved:
        data.update(saved)
    else:
        binding = root / '.local' / context_name
        if binding.exists():
            saved_context = json.loads(binding.read_text())
            data['region'] = saved_context.get('placement', {}).get('region', saved_context.get('region'))
        variables = root / 'terraform-selectel/terraform.tfvars.json'
        if variables.exists():
            values = json.loads(variables.read_text())
            data.update({k: values[k] for k in ('region', 'availability_zone', 'volume_type') if k in values})
    return data


def capacity(item, needed):
    if item is None:
        raise CandidateUnavailable('Required quota unavailable')
    free = item['limit'] - item.get('in_use', item.get('used', 0)) - item.get('reserved', 0)
    if item['limit'] != -1 and free < needed:
        raise CandidateUnavailable('Insufficient shared quota')


def remaining_demand(root, base, node_profiles):
    """Count only additions; current own resources already consume shared quotas."""
    demand = dict(base)
    state = Path(root) / 'terraform-selectel/terraform.tfstate'
    if not state.exists():
        return demand
    counts = {}
    for resource in json.loads(state.read_text()).get('resources', []):
        if resource.get('mode') != 'managed':
            continue
        kind = resource['type']
        instances = resource.get('instances', [])
        counts[kind] = counts.get(kind, 0) + len(instances)
        for instance in instances:
            attrs = instance.get('attributes', {})
            if kind == 'openstack_compute_instance_v2':
                profile = node_profiles.get(instance.get('index_key'))
                if profile:
                    for key, value in [('cores', profile['vcpus']), ('ram', profile['ram_mb']), ('instances', 1)]:
                        demand[key] = max(0, demand[key] - value)
            if kind == 'openstack_blockstorage_volume_v3':
                demand['disk_gb'] = max(0, demand['disk_gb'] - attrs.get('size', 0))
    for key, kind in dict(volumes='blockstorage_volume_v3', floatingip='networking_floatingip_v2',
                          network='networking_network_v2', subnet='networking_subnet_v2',
                          router='networking_router_v2', security_group='networking_secgroup_v2',
                          security_group_rule='networking_secgroup_rule_v2', port='networking_port_v2').items():
        if key in demand:
            demand[key] = max(0, demand[key] - counts.get('openstack_' + kind, 0))
    # Neutron creates router/DHCP/default-rule objects outside explicit state entries.
    if all(counts.get('openstack_' + kind, 0) for kind in
           ('networking_network_v2', 'networking_subnet_v2', 'networking_router_v2', 'networking_router_interface_v2')):
        demand['port'] = max(0, demand['port'] - 3)
    demand['port'] = max(0, demand['port'] - counts.get('openstack_networking_floatingip_v2', 0))
    if counts.get('openstack_networking_secgroup_v2'):
        demand['security_group_rule'] = max(0, demand['security_group_rule'] - 2)
    return demand


def reject_orphans(cloud, prefix, subnets):
    items = list(subnets)
    items += cloud.get('compute', '/servers/detail')['servers']
    items += cloud.get('network', '/v2.0/networks')['networks']
    if any(i.get('name') == prefix or i.get('name', '').startswith(prefix + '-') for i in items):
        raise ValueError('Resources with own prefix exist outside managed state; inspect before deployment')


def select(root, choice, project, context_name):
    root = Path(root)
    state = root / 'terraform-selectel/terraform.tfstate'
    if state.exists() and any(r.get('mode') == 'managed' and r.get('instances')
                              for r in json.loads(state.read_text()).get('resources', [])):
        raise ValueError('Cannot change pool while managed state remains')
    previous = selected(root)
    if previous and previous.get('phase') not in ('prepared', 'destroyed'):
        raise ValueError('Previous deployment needs confirmed destroy before pool selection')
    context = root / '.local' / context_name
    if context.exists() and not previous:
        raise ValueError('Legacy context needs explicit destroy before selecting a new pool')
    if previous and previous['project_id'] != project:
        raise ValueError('Saved deployment belongs to another project')
    local = root / '.local'
    local.mkdir(exist_ok=True)
    from selectel_adapter import binding
    from stack_config import load
    ownership = binding(dict(project_id=project, region=choice['region']), load()['name_prefix'], choice.get('availability_zone'))
    if previous:
        if previous.get('provider') != ownership['provider'] or previous.get('namespace') != ownership['namespace']:
            raise ValueError('Previous placement belongs to another provider or namespace')
        if context.exists():
            from cloud_contract import validate_binding
            expected = binding(dict(project_id=project, region=previous['region']), ownership['namespace'],
                               previous.get('availability_zone'))
            validate_binding(json.loads(context.read_text()), expected)
    data = dict(choice, project_id=project, phase='prepared', provider='selectel', namespace=ownership['namespace'])
    (local / 'placement.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    context.write_text(json.dumps(ownership), encoding='utf-8')


def phase(root, value):
    data = selected(root)
    if data:
        data['phase'] = value
        (Path(root) / '.local/placement.json').write_text(json.dumps(data, indent=2), encoding='utf-8')


def scheduler_unavailable(error, root, cloud_factory, prefix):
    if not isinstance(error, TerraformError):
        return False
    def capacity(text):
        normalized = ' '.join(text.lower().split())
        return any(s in normalized for s in ('no valid host', 'novalidhost', 'not enough hosts',
            'exhausted all retry attempts for shelving preemptible instances to build instance'))
    if capacity(error.output):
        return True
    path = Path(root) / 'terraform-selectel/terraform.tfstate'
    if not path.exists():
        return False
    cloud = cloud_factory()
    found = False
    for resource in json.loads(path.read_text()).get('resources', []):
        if resource.get('mode') != 'managed' or resource['type'] != 'openstack_compute_instance_v2':
            continue
        for instance in resource.get('instances', []):
            attrs = instance['attributes']
            expected = prefix + '-' + str(instance['index_key'])
            if attrs.get('name') != expected:
                raise ValueError('Foreign instance diagnostics refused')
            server = cloud.get('compute', '/servers/' + attrs['id'])['server']
            if server['name'] != expected:
                raise ValueError('Foreign server UUID')
            found |= server.get('status') == 'ERROR' and capacity(server.get('fault', {}).get('message', ''))
    return found


def run_candidates(choices, select_choice, prepare, apply, exhausted=None):
    for choice in choices:
        select_choice(choice)
        try:
            prepare()
        except CandidateUnavailable as error:
            print('Candidate rejected before allocation: ' + str(error), flush=True)
            continue  # No allocation has occurred.
        try:
            return apply()
        except CapacityRetry:
            continue  # Raised only AFTER state/API audits succeeded.
    if exhausted:
        exhausted()
    raise RuntimeError('No configured pool/segment can place the complete stand')
