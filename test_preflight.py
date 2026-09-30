"""Fast checks before downloading or building any CUDA/PyTorch layers."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import yaml
from traitlets.config import Config
from jupyter_server.serverapp import ServerApp
from jupyter_server.services.contents.filemanager import FileContentsManager

spec = importlib.util.spec_from_file_location('launcher', Path(__file__).with_name('start.py'))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

manager_patch_spec = importlib.util.spec_from_file_location('manager_patch', Path(__file__).with_name('patch_manager.py'))
manager_patch = importlib.util.module_from_spec(manager_patch_spec)
manager_patch_spec.loader.exec_module(manager_patch)

class Preflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        self.local = self.root / 'local'; self.local.mkdir()
        self.patches = [patch.object(launcher, 'STATE', self.state), patch.object(launcher, 'LOCAL', self.local)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in self.patches: item.stop()
        self.tmp.cleanup()

    def config(self):
        config = Config()
        exec(launcher.jupyter_config("token-with-'quote", self.root), {'c': config})
        return config

    def test_jupyter_config_validates_real_traits(self):
        app = ServerApp(config=self.config())
        self.assertEqual(app.log_level, 30)
        self.assertEqual(app.port, 8888)
        self.assertEqual(app.port_retries, 0)
        self.assertEqual(app.root_dir, str(self.root))
        self.assertTrue(app.allow_remote_access)
        self.assertTrue(app.allow_root)

    def test_jupyter_no_auth_config(self):
        config = Config()
        exec(launcher.jupyter_config('', self.root), {'c': config})
        self.assertEqual(config.IdentityProvider.token, '')

    def test_global_volume_save_avoids_atomic_rename(self):
        manager = FileContentsManager(config=self.config(), root_dir=str(self.root))
        self.assertFalse(manager.use_atomic_writing)
        self.assertFalse(manager.delete_to_trash)
        manager.save({'type': 'file', 'format': 'text', 'content': 'persistent'}, 'sample.txt')
        self.assertEqual((self.root / 'sample.txt').read_text(), 'persistent')

    def test_empty_credential_recovers_and_valid_credential_is_reused(self):
        (self.state / 'token.txt').write_text('')
        token = launcher.secret('token.txt')
        self.assertGreaterEqual(len(token), 24)
        self.assertEqual(launcher.secret('token.txt'), token)

    def test_existing_models_are_registered_without_copy(self):
        models = self.root / 'runpod-slim/ComfyUI/models'
        for name in ('loras', 'clip', 'unet'): (models / name).mkdir(parents=True)
        source = models / 'loras/example.safetensors'; source.write_bytes(b'sentinel')
        result = yaml.safe_load(launcher.model_paths(self.root, self.local).read_text())
        self.assertEqual(result['bundled_nodes']['custom_nodes'], '/opt/bundled-custom-nodes')
        self.assertEqual(result['existing_0']['base_path'], str(models))
        self.assertEqual(result['existing_0']['text_encoders'], 'clip')
        self.assertEqual(result['existing_0']['diffusion_models'], 'unet')
        self.assertEqual(source.read_bytes(), b'sentinel')

    def test_fresh_volume_loads_image_bundled_nodes(self):
        result = yaml.safe_load(launcher.model_paths(self.root, self.local).read_text())
        self.assertEqual(result, {'bundled_nodes': {'custom_nodes': '/opt/bundled-custom-nodes'}})

    def test_snapshot_restore_falls_back_after_interrupted_write(self):
        dbpath = self.local / 'comfyui.db'
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute('create table examples (name text)')
            db.execute('insert into examples values (?)', ('saved',)); db.commit()
        launcher.snapshot(); launcher.snapshot()
        (self.state / 'comfyui-current.db').write_bytes(b'partial write')
        dbpath.unlink()
        launcher.restore_databases()
        with closing(sqlite3.connect(dbpath)) as db:
            self.assertEqual(db.execute('select name from examples').fetchone()[0], 'saved')

    def test_no_volume_refuses_start_before_launching_services(self):
        with patch.object(launcher, 'ROOT', self.root), patch.object(Path, 'is_mount', return_value=False), patch.object(launcher, 'launch') as launch:
            with self.assertRaises(SystemExit): launcher.main()
            launch.assert_not_called()

    def test_manager_is_enabled_by_default(self):
        source = Path(launcher.__file__).read_text()
        self.assertIn("os.environ.get('ENABLE_MANAGER', '1') != '0'", source)

    def test_manager_copy_patch_avoids_unsupported_permission_changes(self):
        package = self.root / 'comfyui_manager'
        for relative in manager_patch.TARGETS:
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('before\nshutil.copy(source, destination)\nafter\n')
        manager_patch.patch_manager(package)
        for relative in manager_patch.TARGETS:
            source = (package / relative).read_text()
            self.assertIn('shutil.copyfile(source, destination)', source)
            self.assertNotIn('shutil.copy(source, destination)', source)

    def test_real_jupyter_authentication_and_proxy_host(self):
        # Actual Jupyter startup is tiny compared with a CUDA build. No GPU libraries required.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        config = self.local / 'jupyter_config.py'
        token = 'preflight-token'
        config.write_text(launcher.jupyter_config(token, self.root).replace('port = 8888', f'port = {port}').replace('ip = "0.0.0.0"', 'ip = "127.0.0.1"'))
        logfile = self.local / 'jupyter.log'
        with logfile.open('w') as log:
            env = dict(os.environ)
            for name in ('JUPYTER_CONFIG_DIR', 'JUPYTER_RUNTIME_DIR', 'JUPYTER_DATA_DIR'):
                target = self.local / name; target.mkdir()
                env[name] = str(target)
            process = subprocess.Popen([sys.executable, '-m', 'jupyter_server', '--config', str(config)], stdout=log, stderr=log, env=env)
            try:
                url = f'http://127.0.0.1:{port}/api/status'
                for _ in range(90):
                    if process.poll() is not None: self.fail(logfile.read_text())
                    request = urllib.request.Request(url + '?token=' + token, headers={'Host': 'example-8888.proxy.runpod.net'})
                    try:
                        with urllib.request.urlopen(request, timeout=2) as response:
                            self.assertEqual(response.status, 200); break
                    except (urllib.error.URLError, TimeoutError): time.sleep(0.5)
                else: self.fail('Jupyter startup timeout: ' + logfile.read_text())
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(urllib.request.Request(url, headers={'Accept': 'application/json'}), timeout=3)
                self.assertEqual(rejected.exception.code, 403)
            finally:
                process.terminate()
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired: process.kill(); process.wait()

if __name__ == '__main__':
    unittest.main(verbosity=2)
