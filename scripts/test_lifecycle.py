"""Regression checks for resource ownership on deployment errors; no cloud calls."""
import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import lab


class LifecycleTests(unittest.TestCase):
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
