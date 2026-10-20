"""Regression checks for resource ownership on deployment errors; no cloud calls."""
import unittest
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import lab


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        lab.LOCAL.mkdir(parents=True, exist_ok=True)

    def test_environment_auth_needs_no_credential_file(self):
        config = {'auth_url': 'https://cloud.api.selcloud.ru/identity/v3', 'region': 'ru-7',
                  'project_id': 'test-project', 'domain_name': 'test-account',
                  'username': 'test-operator', 'password': 'test-password'}
        names = {'auth_url': 'OS_AUTH_URL', 'region': 'OS_REGION_NAME', 'project_id': 'OS_PROJECT_ID',
                 'domain_name': 'OS_USER_DOMAIN_NAME', 'username': 'OS_USERNAME', 'password': 'OS_PASSWORD'}
        with patch.dict(os.environ, {names[k]: v for k, v in config.items()}, clear=True), \
             patch.object(lab, '_AUTH_CONFIG', None), patch.object(lab, 'bind_context') as bind:
            self.assertEqual(lab.auth_config(), config)
            bind.assert_called_once_with(config)

    def test_context_adopts_existing_stand_and_rejects_other_project(self):
        with tempfile.TemporaryDirectory(dir=lab.LOCAL) as directory:
            root = Path(directory)
            (root / 'terraform-selectel').mkdir()
            (root / 'terraform-selectel/terraform.tfstate').write_text(json.dumps({'resources': [
                {'mode': 'managed', 'instances': [{'attributes': {'tenant_id': 'own-project'}}]}]}))
            with patch.object(lab, 'ROOT', root), patch.object(lab, 'LOCAL', root / '.local'):
                lab.bind_context({'project_id': 'own-project', 'region': 'ru-7', 'password': 'not-saved'})
                context = json.loads((root / '.local/context.json').read_text())
                self.assertEqual(context, {'project_id': 'own-project', 'region': 'ru-7'})
                with self.assertRaisesRegex(ValueError, 'existing stand project'):
                    lab.bind_context({'project_id': 'foreign-project', 'region': 'ru-7'})

    def test_redaction_removes_credentials_and_application_secrets(self):
        with tempfile.TemporaryDirectory(dir=lab.LOCAL) as directory:
            local = Path(directory)
            (local / 'secrets.json').write_text(json.dumps({'app': 'application-secret'}))
            config = dict(zip(['domain_name', 'project_id', 'username', 'password'],
                              ['sample-account', 'sample-project', 'sample-user', 'sample-password']))
            with patch.object(lab, 'LOCAL', local), patch.object(lab, '_AUTH_CONFIG', config):
                original = ' '.join([*config.values(), 'application-secret'])
                self.assertEqual(lab.redact(original), ' '.join(['<redacted>'] * 5))

    def test_operator_file_removed_only_after_verified_cleanup(self):
        for success in [True, False]:
            with self.subTest(success=success), tempfile.TemporaryDirectory(dir=lab.LOCAL) as directory:
                root = Path(directory)
                local = root / '.local'
                local.mkdir()
                operator = local / 'operator.json'
                operator.write_text('{}')
                config = {'project_id': 'test-project', 'region': 'ru-7'}
                with patch.object(lab, 'ROOT', root), patch.object(lab, 'LOCAL', local), \
                     patch.object(lab, 'auth_config', return_value=config), patch.object(lab, 'checked_plan'), \
                     patch.object(lab, 'tf', return_value='{}'), patch.object(lab, 'Cloud') as cloud:
                    cloud.return_value.config = config
                    if success:
                        cloud.return_value.get.return_value = {'floatingips': []}
                        lab.cleanup()
                    else:
                        cloud.return_value.get.side_effect = TimeoutError()
                        with self.assertRaises(TimeoutError):
                            lab.cleanup()
                    self.assertEqual(operator.exists(), not success)

    def test_cloud_retries_timed_out_reads(self):
        cloud = lab.Cloud.__new__(lab.Cloud)
        cloud.endpoints, cloud.token = {'compute': 'https://example.invalid'}, 'test-token'
        with patch.object(lab, 'request_json', side_effect=[TimeoutError(), ({'servers': []}, {})]) as request, \
             patch.object(lab.time, 'sleep'):
            self.assertEqual(cloud.get('compute', '/servers'), {'servers': []})
            self.assertEqual(request.call_count, 2)

    def test_cloud_does_not_retry_power_actions(self):
        cloud = lab.Cloud.__new__(lab.Cloud)
        cloud.endpoints, cloud.token = {'compute': 'https://example.invalid'}, 'test-token'
        with patch.object(lab, 'request_json', side_effect=TimeoutError()) as request:
            with self.assertRaises(TimeoutError):
                cloud.get('compute', '/servers/own-id/action', {'os-stop': None})
            self.assertEqual(request.call_count, 1)

    def test_placement_requires_hard_policy_and_distinct_hosts(self):
        ids = {'db1': 'id1', 'db2': 'id2', 'db3': 'id3'}
        for policy, hosts, valid in [(['anti-affinity'], ['a', 'b', 'c'], True),
                                     (['soft-anti-affinity'], ['a', 'b', 'c'], False),
                                     (['anti-affinity'], ['a', 'a', 'b'], False)]:
            group = {'name': 'otus-dz06-db', 'policies': policy, 'members': list(ids.values())}
            servers = {identifier: {'name': 'otus-dz06-' + name, 'status': 'ACTIVE', 'hostId': host}
                       for (name, identifier), host in zip(ids.items(), hosts)}
            with self.subTest(policy=policy, hosts=hosts), patch.object(lab, 'Cloud') as cloud, \
                 patch.object(lab, 'save'), patch.object(lab, 'auth_config', return_value={'region': 'ru-7'}):
                cloud.return_value.get.side_effect = lambda service, path: (
                    {'server_groups': [group]} if path == '/os-server-groups'
                    else {'server': servers[path.rsplit('/', 1)[1]]})
                if valid:
                    lab.check_placement({'server_ids': ids})
                else:
                    with self.assertRaises(RuntimeError):
                        lab.check_placement({'server_ids': ids})

    def test_verify_includes_failure_and_recovery(self):
        with patch('sys.argv', ['lab.py', 'verify']), patch.object(lab, 'verify') as verify:
            lab.main()
            verify.assert_called_once_with(True)

    def test_destroy_and_cleanup_use_same_owned_resource_cleanup(self):
        for action in ['destroy', 'cleanup']:
            with self.subTest(action=action), patch('sys.argv', ['lab.py', action]), \
                 patch.object(lab, 'cleanup') as cleanup:
                lab.main()
                cleanup.assert_called_once_with()

    def check_deploy_failure(self, existing):
        with patch.object(lab, 'managed_ids', return_value=existing), \
             patch.object(lab, 'preflight'), patch.object(lab, 'checked_plan'), \
             patch.object(lab, 'tf'), \
             patch.object(lab, 'configure', side_effect=RuntimeError('configuration unavailable')), \
             patch.object(lab, 'diagnostics'), patch.object(lab, 'cleanup') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'configuration unavailable'):
                lab.deploy()
            return cleanup.call_count

    def test_failed_repeat_deploy_preserves_existing_resources(self):
        self.assertEqual(self.check_deploy_failure({'existing-node': 'owned-id'}), 0)

    def test_failed_initial_deploy_cleans_partial_resources(self):
        self.assertEqual(self.check_deploy_failure({}), 1)

    def test_test_cycle_always_cleans_after_repeat_failure(self):
        with patch.object(lab, 'deploy', side_effect=RuntimeError('test failure')), \
             patch.object(lab, 'cleanup') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                lab.regression()
            cleanup.assert_called_once_with()


class TrustTests(unittest.TestCase):
    def test_verified_key_survives_console_reset_for_same_uuid(self):
        key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).split()[1].decode()
        lab.LOCAL.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=lab.LOCAL) as directory:
            local = Path(directory)
            (local / 'trusted-hosts.json').write_text(json.dumps({'same-uuid': key}))
            with patch.object(lab, 'LOCAL', local), patch.object(lab, 'Cloud') as cloud:
                cloud.return_value.get.return_value = {'server': {'name': 'otus-dz06-app'}}
                lab.trust({'server_ids': {'app': 'same-uuid'}, 'private_ips': {'app': '10.66.0.10'}, 'app_public_ip': '192.0.2.1'})
                cloud.return_value.get.assert_called_once_with('compute', '/servers/same-uuid')
                self.assertIn(key, (local / 'known_hosts').read_text())
                self.assertIn('10.66.0.10,192.0.2.1 ssh-ed25519 ', (local / 'known_hosts').read_text())

    def test_new_uuid_requires_new_api_verification(self):
        lab.LOCAL.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=lab.LOCAL) as directory:
            local = Path(directory)
            (local / 'trusted-hosts.json').write_text(json.dumps({'old-uuid': 'old-key'}))
            with patch.object(lab, 'LOCAL', local), patch.object(lab, 'Cloud') as cloud, patch.object(lab.time, 'sleep'):
                cloud.return_value.get.side_effect = lambda service, path, *args: (
                    {'server': {'name': 'otus-dz06-app'}} if path.endswith('/new-uuid') else {'output': ''})
                with self.assertRaisesRegex(RuntimeError, 'No trusted SSH key'):
                    lab.trust({'server_ids': {'app': 'new-uuid'}, 'private_ips': {'app': '10.66.0.10'}, 'app_public_ip': '192.0.2.1'})


if __name__ == '__main__':
    unittest.main()
