"""Credential sources are explicit and share one schema."""
import os
import unittest
from unittest.mock import patch

import credentials


class CredentialTests(unittest.TestCase):
    def test_default_environment_does_not_fall_back_to_file(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(credentials, 'read_private_file') as read:
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                credentials.load_credentials()
            read.assert_not_called()

    def test_explicit_development_file_is_selected(self):
        values = {'domain_name': 'sample-account', 'project_id': 'sample-project',
                  'username': 'sample-user', 'password': 'sample-password'}
        with patch.dict(os.environ, {'OTUS_SELECTEL_AUTH': 'explicit-file'}, clear=True), \
             patch.object(credentials, 'read_private_file', return_value=values) as read:
            config = credentials.load_credentials()
            self.assertEqual(config['project_id'], values['project_id'])
            read.assert_called_once()

    def test_environment_mapping_and_redaction_use_schema(self):
        values = {'domain_name': 'sample-account', 'project_id': 'sample-project',
                  'username': 'sample-user', 'password': 'sample-password'}
        env = credentials.to_environment(values)
        with patch.dict(os.environ, env, clear=True):
            config = credentials.load_credentials()
        self.assertEqual(set(credentials.credential_values(config)), set(values.values()))


if __name__ == '__main__':
    unittest.main()
