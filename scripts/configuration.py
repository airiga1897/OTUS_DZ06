"""Server configuration backend, independent of infrastructure creation."""
import json
import paramiko
from cloud_settings import terraform_root


def configure(api, existing=False):
    ROOT, LOCAL = api.ROOT, api.LOCAL
    lab_output, trust, connect = api.lab_output, api.trust, api.connect
    remote, upload_tree, put = api.remote, api.upload_tree, api.put
    discover_route = api.discover_route
    lab = lab_output()
    api.check_placement(lab)
    from cloud_settings import adapter
    variables = json.loads((terraform_root(ROOT) / 'terraform.tfvars.json').read_text())
    api.save(ROOT / 'docs/network-result.json', adapter().check_network(api.Cloud(), api.infrastructure_output(), variables['admin_cidrs']))
    trust(lab)
    if not existing:
        from network_diagnostics import require_bootstrap_network
        require_bootstrap_network(api, lab)
    try:
        with connect(lab):
            pass
    except (OSError, paramiko.SSHException):
        if existing:
            raise RuntimeError('SSH to the existing stand is unavailable; check routing and allowed /32 before a separate network change')
        discover_route(lab)
    passwords = json.loads((LOCAL / 'secrets.json').read_text())
    with connect(lab) as app:
        remote(app, 'sudo python3 -', 'network-app', stdin=(ROOT / 'scripts/remote/network_diagnostics.py').read_text())
        remote(app, 'sudo python3 -', 'cloud-init-app', stdin=(ROOT / 'scripts/remote/check_cloud_init.py').read_text())
        upload_tree(app)
        put(app, api.STACK['paths']['remote_root'] + '/settings.json', json.dumps({'lab': lab, 'passwords': passwords}))
        from ansible_backend import configure as configure_ansible
        from render import ansible_inventory
        configure_ansible(api, app, ansible_inventory(api.infrastructure_output(), passwords))
    print('Cluster and portal configured with Ansible', flush=True)
