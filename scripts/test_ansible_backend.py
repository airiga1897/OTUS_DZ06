"""Offline Ansible contracts: inventory, rendered templates, guarded tasks and cleanup."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from jinja2 import Environment, StrictUndefined
import yaml

import ansible_backend
from render import ansible_inventory
import topology
from selectel_adapter import infrastructure_result

ROOT = Path(__file__).resolve().parents[1]


def tasks(value):
    for task in value:
        yield task
        yield from tasks(task.get('block', []))


class AnsibleBackendTests(unittest.TestCase):
    def inventory(self, extended=False):
        desired = topology.load(ROOT)
        if extended:
            desired['nodes']['db4'] = {'host': 24, 'role': 'db'}
        lab = {'private_ips': {n: '10.66.0.' + str(v['host']) for n, v in desired['nodes'].items()},
               'app_public_ip': '192.0.2.1', 'subnet_cidr': '10.66.0.0/24', 'etcd_members': desired['etcd_members'],
               **{k: {n: k+n for n in desired['nodes']} for k in ('server_ids','volume_ids','port_ids')}}
        lab = infrastructure_result(lab, {'project_id':'synthetic','region':'ru-7'}, 'otus-dz06', 'ru-7a')
        passwords = dict.fromkeys(('postgres', 'replicator', 'api', 'django', 'app'), 'synthetic')
        return lab, passwords, ansible_inventory(lab, passwords)

    def test_new_database_node_does_not_change_existing_hostvars_or_dcs(self):
        before = self.inventory()[2]['all']['children']
        after = self.inventory(True)[2]['all']['children']
        self.assertEqual(before['etcd_voters'], after['etcd_voters'])
        for name, variables in before['database']['hosts'].items():
            self.assertEqual(variables, after['database']['hosts'][name])
        self.assertIn('db4', after['database']['hosts'])
        self.assertEqual(after['frontend']['hosts']['app']['ansible_connection'], 'local')

    def test_controller_private_copies_are_removed_on_success_and_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
                local = Path(directory)
                (local / 'otus_dz06').write_text('synthetic-private-key')
                (local / 'known_hosts').write_text('synthetic-known-host')
                api = MagicMock(LOCAL=local)
                def remote(app, command, label):
                    if fails and label == 'ansible-configure':
                        raise RuntimeError('configuration failed')
                api.remote.side_effect = remote
                app = MagicMock()
                lab, passwords, _ = self.inventory(True)
                if fails:
                    with self.assertRaisesRegex(RuntimeError, 'configuration failed'):
                        ansible_backend.configure(api, app, ansible_inventory(lab, passwords))
                else:
                    ansible_backend.configure(api, app, ansible_inventory(lab, passwords))
                sftp = app.open_sftp.return_value.__enter__.return_value
                self.assertEqual(sftp.remove.call_count, 3)
                self.assertTrue((local / 'otus_dz06').exists())
                labels = [c.args[2] for c in api.remote.call_args_list]
                self.assertIn('ansible-syntax', labels)
                self.assertIn('ansible-configure', labels)

    def test_roles_and_handlers_are_present_and_tasks_are_valid_yaml(self):
        site = yaml.safe_load((ROOT / 'ansible/site.yml').read_text())
        self.assertEqual(site[0]['roles'], ['common', 'etcd', 'patroni'])
        self.assertEqual(site[1]['roles'], ['haproxy', 'portal'])
        for play in site:
            for role in play['roles']:
                path = ROOT / 'ansible/roles' / role / 'tasks/main.yml'
                definitions = list(tasks(yaml.safe_load(path.read_text())))
                self.assertTrue(definitions)
                handlers = ROOT / 'ansible/roles' / role / 'handlers/main.yml'
                known = {h['name'] for h in yaml.safe_load(handlers.read_text())} if handlers.exists() else set()
                for task in definitions:
                    if 'notify' in task:
                        self.assertIn(task['notify'], known)

    def test_rendered_dcs_is_stable_and_haproxy_includes_new_node(self):
        env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=False, keep_trailing_newline=True)
        def render(relative, extended):
            inventory = self.inventory(extended)[2]['all']['children']
            hostvars = inventory['database']['hosts']
            groups = {g: list(v['hosts']) for g, v in inventory.items()}
            return env.from_string((ROOT / 'ansible/roles' / relative).read_text()).render(
                inventory_hostname='db1', ansible_host='10.66.0.21', hostvars=hostvars, groups=groups,
                stack=ansible_backend.STACK)
        etcd = 'etcd/templates/etcd.j2'
        self.assertEqual(render(etcd, False), render(etcd, True))
        self.assertIn('server db4 10.66.0.24:5432 check port 8008', render('haproxy/templates/haproxy.cfg.j2', True))

    def test_redeploy_applies_code_migrations_and_guarded_initial_data(self):
        portal = yaml.safe_load((ROOT / 'ansible/roles/portal/tasks/main.yml').read_text())
        initialize = next(t for t in portal if t['name'] == 'Apply migrations and guarded initial data')
        self.assertNotIn('when', initialize)
        self.assertTrue(initialize['no_log'])
        for name in ('Install portal source from this repository', 'Install portal dependencies', 'Install uWSGI', 'Collect static assets'):
            task = next(t for t in portal if t['name'] == name)
            self.assertNotIn('when', task)
        self.assertEqual(next(t for t in portal if t['name'] == 'Install portal source from this repository')['notify'], 'Restart portal')
        config = yaml.safe_load((ROOT / 'ansible/roles/patroni/tasks/main.yml').read_text())
        private = next(t for t in config if t['name'] == 'Install Patroni configuration')
        self.assertTrue(private['no_log'])
        service = next(t for t in config if t['name'] == 'Enable and start Patroni without restart')
        self.assertEqual(service['ansible.builtin.systemd_service']['state'], 'started')

    def test_new_patroni_is_not_signalled_during_startup(self):
        handler = yaml.safe_load((ROOT / 'ansible/roles/patroni/handlers/main.yml').read_text())[0]
        self.assertEqual(handler.get('when'), 'patroni_previous.stat.exists')
        definitions = yaml.safe_load((ROOT / 'ansible/roles/patroni/tasks/main.yml').read_text())
        start = next(i for i,t in enumerate(definitions) if t['name']=='Enable and start Patroni without restart')
        ready = next(i for i,t in enumerate(definitions) if 'ansible.builtin.wait_for' in t)
        self.assertGreater(ready,start)
        self.assertEqual(definitions[ready]['ansible.builtin.wait_for']['port'],'{{ stack.patroni.api_port }}')

    def test_native_templates_have_valid_jinja_syntax(self):
        env = Environment()
        for path in (ROOT / 'ansible').rglob('*.j2'):
            with self.subTest(path=path.name):
                env.parse(path.read_text())
