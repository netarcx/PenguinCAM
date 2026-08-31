"""Static safety contract for the UP2 deployment pipeline.

These tests cannot contact production, but they keep a later workflow edit from quietly
deploying pull requests, using an unpinned action, or rsyncing over persistent config.
"""

from pathlib import Path
import stat
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class TestUP2Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(
            (ROOT / '.github/workflows/integration.yaml').read_text(encoding='utf-8'))
        cls.deploy = cls.workflow['jobs']['deploy-up2']

    def test_deploys_only_tested_uvcam_pushes(self):
        self.assertEqual(self.deploy['needs'], 'test-cam')
        condition = self.deploy['if']
        self.assertIn("github.event_name == 'push'", condition)
        self.assertIn("github.ref == 'refs/heads/uvcam'", condition)
        self.assertEqual(self.deploy['environment']['name'], 'up2-production')
        self.assertFalse(self.deploy['concurrency']['cancel-in-progress'])

    def test_third_party_actions_are_commit_pinned(self):
        for job in self.workflow['jobs'].values():
            for step in job.get('steps', []):
                action = step.get('uses')
                if action:
                    with self.subTest(action=action):
                        self.assertRegex(action, r'@[0-9a-f]{40}$')

    def test_production_job_uses_short_lived_private_network_identity(self):
        self.assertEqual(self.deploy['runs-on'], 'ubuntu-latest')
        self.assertEqual(self.deploy['permissions']['id-token'], 'write')
        serialized = repr(self.deploy)
        for name in ('TS_OAUTH_CLIENT_ID', 'TS_AUDIENCE',
                     'UP2_SSH_PRIVATE_KEY', 'UP2_SSH_KNOWN_HOSTS'):
            self.assertIn(name, serialized)


class TestUP2DeployScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / 'scripts/deploy_up2.sh'
        cls.script = cls.path.read_text(encoding='utf-8')

    def test_script_is_executable_and_fixed_to_expected_root(self):
        self.assertTrue(self.path.stat().st_mode & stat.S_IXUSR)
        self.assertIn("readonly DEPLOY_ROOT='/mnt/user/appdata/penguincam'", self.script)
        self.assertIn("[[ \"$deploy_root\" == '/mnt/user/appdata/penguincam' ]]", self.script)

    def test_rsync_cannot_replace_persistent_assets(self):
        self.assertIn('--delete-delay', self.script)
        for exclusion in ("--exclude='/.env'", "--exclude='/docker-compose.yml'",
                          "--exclude='/PenguinCAM-config-*.yaml'",
                          "--exclude='/.venv/'", "--exclude='/google-creds.txt'",
                          "--exclude='/onshape_config.json'",
                          "--exclude='/penguincam-jobs/'"):
            self.assertIn(exclusion, self.script)
        self.assertIn('test ! -L "$deploy_root/build"', self.script)

    def test_health_failure_has_image_and_compose_rollback(self):
        self.assertIn('penguincam:rollback', self.script)
        self.assertIn('docker-compose.previous.yml', self.script)
        self.assertIn('wait_for_health 150', self.script)
        self.assertIn('wait_for_health 90', self.script)

    def test_compose_keeps_machine_config_external(self):
        compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        service = compose['services']['penguincam']
        self.assertEqual(service['environment']['PENGUINCAM_LOCAL'], '1')
        self.assertTrue(any('/app/PenguinCAM-config.yaml:ro' in mount
                            for mount in service['volumes']))
        self.assertTrue(compose['networks']['cloudflare-net']['external'])


if __name__ == '__main__':
    unittest.main()
