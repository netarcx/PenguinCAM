"""Regression coverage for OAuth and cloud-integration boundary behavior."""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

from flask import Flask, session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google_drive_integration import GoogleDriveUploader
from onshape_integration import OnshapeClient
from penguincam_auth import PenguinCAMAuth


class TestGoogleAuthSession(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            'AUTH_ENABLED': 'true',
            'GOOGLE_CLIENT_ID': 'client-id',
            'GOOGLE_CLIENT_SECRET': 'client-secret',
            'ALLOWED_DOMAINS': 'example.org',
        })
        self.env.start()
        self.app = Flask(__name__)
        self.app.secret_key = 'test-secret'
        self.auth = PenguinCAMAuth(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        self.env.stop()

    def test_status_and_get_user_read_the_google_profile_keys(self):
        with self.client.session_transaction() as flask_session:
            flask_session.update({
                'authenticated': True,
                'google_user_email': 'driver@example.org',
                'google_user_name': 'Driver',
                'google_user_picture': 'https://example.org/driver.png',
                # A separate Onshape identity may coexist in the session.
                'user_email': 'cad@example.org',
            })

        payload = self.client.get('/auth/status').get_json()
        self.assertEqual(payload['user']['email'], 'driver@example.org')
        self.assertEqual(payload['user']['name'], 'Driver')
        with self.app.test_request_context('/'):
            session.update({
                'authenticated': True,
                'google_user_email': 'driver@example.org',
                'google_user_name': 'Driver',
            })
            self.assertEqual(self.auth.get_user()['email'], 'driver@example.org')

    def test_callback_rejects_a_missing_state_instead_of_matching_none(self):
        response = self.client.get('/auth/callback?code=attacker-code')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid state', response.get_data(as_text=True))

    def test_callback_state_is_single_use_even_when_the_callback_fails(self):
        with self.client.session_transaction() as flask_session:
            flask_session['oauth_state'] = 'expected'
        response = self.client.get('/auth/callback?code=x&state=wrong')
        self.assertEqual(response.status_code, 400)
        with self.client.session_transaction() as flask_session:
            self.assertNotIn('oauth_state', flask_session)


class TestDriveConfiguration(unittest.TestCase):
    def test_environment_overrides_the_checked_in_config_file(self):
        with mock.patch.dict(os.environ, {
            'DRIVE_NAME': 'Competition Drive',
            'DRIVE_FOLDER': 'CAM/Ready',
            'GOOGLE_DRIVE_FOLDER_ID': 'folder-from-env',
        }, clear=False):
            config = GoogleDriveUploader().config
        self.assertEqual(config['shared_drive_name'], 'Competition Drive')
        self.assertEqual(config['folder_path'], 'CAM/Ready')
        self.assertEqual(config['folder_id'], 'folder-from-env')

    def test_folder_lookup_starts_at_root_and_escapes_apostrophes(self):
        uploader = GoogleDriveUploader()
        uploader.service = mock.MagicMock()
        execute = uploader.service.files.return_value.list.return_value.execute
        execute.side_effect = [
            {'files': [{'id': 'parent-folder'}]},
            {'files': [{'id': 'target-folder'}]},
        ]

        found = uploader.find_folder_in_drive('shared-drive-id', "Robots' CAM/G-code")

        self.assertEqual(found, 'target-folder')
        calls = uploader.service.files.return_value.list.call_args_list
        first_query = calls[0].kwargs['q']
        second_query = calls[1].kwargs['q']
        self.assertIn("name='Robots\\' CAM'", first_query)
        self.assertIn("'shared-drive-id' in parents", first_query)
        self.assertIn("'parent-folder' in parents", second_query)


class TestOnshapeOAuthAndTranslation(unittest.TestCase):
    def test_app_callback_rejects_a_missing_state_instead_of_matching_none(self):
        from frc_cam_gui_app import app

        app.config['TESTING'] = True
        response = app.test_client().get(
            '/onshape/oauth/callback?code=attacker-code')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid state', response.get_data(as_text=True))

    def test_refresh_keeps_a_rotated_refresh_token(self):
        client = OnshapeClient()
        client.refresh_token = 'old-refresh-token'
        client.config['client_id'] = 'id'
        client.config['client_secret'] = 'secret'
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            'access_token': 'new-access-token',
            'refresh_token': 'new-refresh-token',
            'expires_in': 120,
        }
        before = datetime.now()
        with mock.patch('onshape_integration.requests.post', return_value=response):
            self.assertTrue(client.refresh_access_token())
        self.assertEqual(client.access_token, 'new-access-token')
        self.assertEqual(client.refresh_token, 'new-refresh-token')
        self.assertGreaterEqual(client.token_expires, before + timedelta(seconds=119))

    def test_async_export_waits_while_translation_is_active(self):
        client = object.__new__(OnshapeClient)
        client.start_dxf_translation = mock.Mock(return_value='translation-id')
        client.check_translation_status = mock.Mock(side_effect=[
            {'requestState': 'ACTIVE'},
            {'requestState': 'DONE', 'resultExternalDataIds': ['external-id']},
        ])
        client.download_translation_result = mock.Mock(return_value=b'DXF')

        with mock.patch('onshape_integration.time.sleep'):
            result = client.export_dxf_async('doc', 'workspace', 'element', timeout=5)

        self.assertEqual(result, b'DXF')
        self.assertEqual(client.check_translation_status.call_count, 2)


if __name__ == '__main__':
    unittest.main()
