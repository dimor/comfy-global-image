import copy
import tempfile
from pathlib import Path
import unittest
from deploy_runpod import deployment_request, launch_url, user_token, template_request


class DeploymentTests(unittest.TestCase):
    def test_replacements_reuse_token_and_users_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            token, folder = user_token(Path(temp) / 'private', 'alice')
            self.assertEqual(user_token(Path(temp) / 'private', 'alice')[0], token)
            self.assertNotEqual(user_token(Path(temp) / 'private', 'bob')[0], token)
            self.assertEqual((folder / 'jupyter-token').stat().st_mode & 0o777, 0o600)

    def test_deployment_preserves_volume_and_environment(self):
        original = {'imageName': 'rebuilt-image', 'volumeMountPath': '/workspace',
                    'networkVolumeId': 'existing-volume', 'ports': ['8188/http', '8888/http'],
                    'env': {'ENABLE_MANAGER': '1', 'OTHER': 'preserve',
                            'JUPYTER_NO_AUTH': '1', 'RUNPOD_POD_ID': 'old'}}
        before = copy.deepcopy(original)
        result = deployment_request(original, 'private-test-token')
        self.assertEqual(original, before)
        self.assertEqual(result['networkVolumeId'], 'existing-volume')
        self.assertEqual(result['env']['OTHER'], 'preserve')
        self.assertEqual(result['env']['JUPYTER_TOKEN'], result['env']['JUPYTER_PASSWORD'])
        self.assertNotIn('JUPYTER_NO_AUTH', result['env'])
        self.assertNotIn('RUNPOD_POD_ID', result['env'])
        self.assertNotEqual(launch_url('newpod', 'token'), launch_url('replacement', 'token'))

    def test_template_bootstrap_and_defaults_are_preserved(self):
        template = {'imageName': 'old-image', 'volumeMountPath': '/workspace',
                    'dockerStartCmd': ["import '/opt/start.py'; launcher.main()"],
                    'dockerEntrypoint': ['bash', '-c'], 'ports': ['8888/http'],
                    'env': {'KEEP': 'original', 'OVERRIDE': 'old'}}
        merged = template_request(template, {'imageName': 'rebuilt', 'networkVolumeId': 'existing',
                                            'env': {'OVERRIDE': 'new'}})
        result = deployment_request(merged, 'token')
        self.assertEqual(result['dockerStartCmd'], template['dockerStartCmd'])
        self.assertEqual(result['env']['KEEP'], 'original')
        self.assertEqual(result['env']['OVERRIDE'], 'new')
        self.assertEqual(result['networkVolumeId'], 'existing')

    def test_rejects_command_override_and_wrong_mount(self):
        for config in ({'volumeMountPath': '/other'},
                       {'volumeMountPath': '/workspace', 'imageName': 'image', 'dockerStartCmd': ['bash']}):
            with self.assertRaises(ValueError):
                deployment_request(config, 'test-token')


if __name__ == '__main__':
    unittest.main(verbosity=2)
