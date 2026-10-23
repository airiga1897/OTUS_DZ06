"""Run Ansible on the Ubuntu application VM, using an ephemeral private inventory."""
import json

from stack_config import load

STACK = load()
REMOTE = STACK['paths']['remote_root']
RUNTIME = REMOTE + '/.ansible-runtime'


def configure(api, app, inventory: dict) -> None:
    try:
        api.remote(app, 'install -d -m 700 ' + RUNTIME, 'ansible-runtime')
        for name, content in [('key', (api.LOCAL / 'otus_dz06').read_text()),
                              ('known_hosts', (api.LOCAL / 'known_hosts').read_text()),
                              ('inventory.json', json.dumps(inventory))]:
            api.put(app, RUNTIME + '/' + name, content)
        api.remote(app, 'sudo python3 ' + REMOTE + '/scripts/remote/ansible_control.py', 'ansible-prepare')
        base = 'cd ' + REMOTE + '/ansible && ANSIBLE_CONFIG=' + REMOTE + '/ansible/ansible.cfg '
        base += STACK['ansible']['venv'] + '/bin/ansible-playbook -i '
        api.remote(app, base + RUNTIME + '/inventory.json site.yml --syntax-check', 'ansible-syntax')
        api.remote(app, base + RUNTIME + '/inventory.json site.yml', 'ansible-configure')
    finally:
        # Remove controller copies even when Ansible fails; local recovery keys remain.
        with app.open_sftp() as sftp:
            for name in ('key', 'known_hosts', 'inventory.json'):
                try:
                    sftp.remove(RUNTIME + '/' + name)
                except FileNotFoundError:
                    pass
