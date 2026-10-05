"""Run fixed image software against an attached persistent /workspace volume."""
import json
from contextlib import closing
import os
from pathlib import Path
import secrets
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path('/workspace')
DATA = ROOT / 'ComfyUI'
STATE = ROOT / '.comfy-image'
LOCAL = Path('/tmp/comfy-image')
stop = threading.Event()
children = []
snapshot_lock = threading.Lock()

def jupyter_config(token, root, pod_id=None):
    if not token:
        raise ValueError('Jupyter authentication requires a nonempty token')
    pod_id = os.environ.get('RUNPOD_POD_ID', '') if pod_id is None else pod_id
    if pod_id and not re.fullmatch(r'[a-zA-Z0-9]+', pod_id):
        raise ValueError('Invalid RUNPOD_POD_ID')
    origin = f'https://{pod_id}-8888.proxy.runpod.net' if pod_id else ''
    return ('c.IdentityProvider.token = ' + repr(token) + '\n' +
            'c.ServerApp.allow_origin = ' + repr(origin) + '\n' +
            'c.ServerApp.trust_xheaders = True\n' +
            'c.ServerApp.root_dir = ' + repr(str(root)) + '\n' +
            'c.ServerApp.ip = "0.0.0.0"\n' +
            'c.ServerApp.port = 8888\n' +
            'c.ServerApp.port_retries = 0\n' +
            'c.ServerApp.allow_root = True\n' +
            'c.ServerApp.allow_remote_access = True\n' +
            'c.ServerApp.open_browser = False\n' +
            'c.ServerApp.log_level = 30\n' +
            'c.FileContentsManager.use_atomic_writing = False\n' +
            'c.FileContentsManager.delete_to_trash = False\n')

def jupyter_token():
    if os.environ.get('JUPYTER_NO_AUTH') == '1':
        raise ValueError('JUPYTER_NO_AUTH=1 is unsupported; authentication must remain enabled')
    token = os.environ.get('JUPYTER_TOKEN')
    password = os.environ.get('JUPYTER_PASSWORD')
    if token and password and token != password:
        raise ValueError('JUPYTER_TOKEN and JUPYTER_PASSWORD must match')
    # PASSWORD is a compatibility alias for this image's token, not a password hash.
    if os.environ.get('RUNPOD_POD_ID') and not (token and password):
        raise ValueError('Deploy RunPod pods with both JUPYTER_TOKEN and JUPYTER_PASSWORD')
    return token or password or secret('jupyter-token.txt')

def model_paths(root, local):
    # Image-owned nodes work even when the attached Global Volume is completely empty.
    mappings = {'bundled_nodes': {'custom_nodes': '/opt/bundled-custom-nodes'}}
    # Register existing storage in place, including the user's previous Slim layout.
    for index, models in enumerate((root / 'runpod-slim/ComfyUI/models', root / 'models')):
        if not models.is_dir():
            continue
        categories = {p.name: p.name for p in models.iterdir() if p.is_dir()}
        for canonical, aliases in {'text_encoders': ('text_encoders', 'clip'),
                                   'diffusion_models': ('diffusion_models', 'unet'),
                                   'controlnet': ('controlnet', 't2i_adapter')}.items():
            present = [name for name in aliases if (models / name).is_dir()]
            if present:
                categories[canonical] = '\n'.join(present)
                for alias in aliases:
                    if alias != canonical:
                        categories.pop(alias, None)
        mappings[f'existing_{index}'] = {'base_path': str(models), **categories}
    target = local / 'extra-model-paths.yaml'
    target.write_text(json.dumps(mappings), encoding='utf-8')  # JSON is valid YAML.
    return target

def secret(name):
    target = STATE / name
    if target.exists():
        value = target.read_text().strip()
        if len(value) >= 24:
            return value
    value = secrets.token_urlsafe(24)
    target.write_text(value + '\n')
    return value

def restore_databases():
    # Keep previous snapshots intact; validate local copies before using them.
    for name in ('comfyui-current.db', 'comfyui-previous.db', 'comfyui.db'):
        saved = STATE / name
        if saved.exists():
            candidate = LOCAL / 'restore-check.db'
            shutil.copyfile(saved, candidate)
            with candidate.open('rb') as stream:
                if stream.read(16) != b'SQLite format 3\x00':
                    continue
            try:
                with closing(sqlite3.connect(candidate)) as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        continue
                shutil.copyfile(candidate, LOCAL / 'comfyui.db')
                return
            except sqlite3.DatabaseError:
                print(f'Skipping incomplete database snapshot: {name}', flush=True)

def snapshot():
    # SQLite's backup API yields a consistent snapshot without copying a live WAL.
    db = LOCAL / 'comfyui.db'
    if db.exists():
        try:
            with snapshot_lock:
                _snapshot(db)
        except Exception as exc:
            print(f'Database snapshot deferred: {exc}', flush=True)

def _snapshot(db):
    backup = LOCAL / 'comfyui-backup.db'
    deadline = time.monotonic() + 15
    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('snapshot exceeded 15 seconds')
    with closing(sqlite3.connect(db, timeout=10)) as src, closing(sqlite3.connect(backup)) as dst:
        src.backup(dst, pages=128, progress=progress)
    current = STATE / 'comfyui-current.db'
    if current.exists():
        # Only rotate a valid snapshot; an interrupted write must not replace fallback.
        check = LOCAL / 'rotate-check.db'
        shutil.copyfile(current, check)
        try:
            with check.open('rb') as stream:
                header_valid = stream.read(16) == b'SQLite format 3\x00'
            if header_valid:
                with closing(sqlite3.connect(check)) as connection:
                    if connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok':
                        shutil.copyfile(check, STATE / 'comfyui-previous.db')
        except sqlite3.DatabaseError:
            pass
    shutil.copyfile(backup, current)

def periodic_snapshot():
    while not stop.wait(120):
        snapshot()

def launch(name, args):
    print(f'Starting {name}', flush=True)
    child = subprocess.Popen(args, start_new_session=True)
    children.append((name, child))

def shutdown(*_):
    stop.set()

def main():
    if not ROOT.is_mount():
        sys.exit('Attach your GLOBAL volume at /workspace before starting this image. Nothing was installed.')
    for folder in (DATA, STATE, LOCAL, DATA / 'input', DATA / 'output', DATA / 'user', DATA / 'custom_nodes'):
        folder.mkdir(parents=True, exist_ok=True)
    # Create model category folders once. Never copy model weights or reinstall packages.
    for source, dirs, files in os.walk('/opt/ComfyUI/models'):
        (DATA / 'models' / Path(source).relative_to('/opt/ComfyUI/models')).mkdir(parents=True, exist_ok=True)
    (LOCAL / 'temp').mkdir(exist_ok=True)
    restore_databases()
    token = jupyter_token()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    # Config file avoids printing the token in the command line or Jupyter startup URL.
    config = LOCAL / 'jupyter_config.py'
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(jupyter_config(token, ROOT))
    config.chmod(0o600)
    launch('JupyterLab :8888', ['jupyter', 'lab', '--config', str(config)])
    args = ['python', '/opt/lazy-cache-main.py', '--listen', '0.0.0.0', '--port', '8188',
            '--base-directory', str(DATA), '--user-directory', str(DATA / 'user'),
            '--database-url', 'sqlite:////tmp/comfy-image/comfyui.db',
            '--temp-directory', str(LOCAL / 'temp')]
    if os.environ.get('COMFY_CPU_TEST') == '1':
        args.append('--cpu')
    # Manager is enabled by default for its model installation and download controls.
    if os.environ.get('ENABLE_MANAGER', '1') != '0':
        args.append('--enable-manager')
    extra_paths = model_paths(ROOT, LOCAL)
    if extra_paths:
        args.extend(['--extra-model-paths-config', str(extra_paths)])
    launch('ComfyUI :8188', args)
    thread = threading.Thread(target=periodic_snapshot, daemon=True)
    thread.start()
    print('Persistent files: /workspace/ComfyUI. No git pull, pip install or model download at startup.', flush=True)
    print('Jupyter file browser: :8888/lab. Authentication enabled; use the private deployment launch file.', flush=True)
    exit_code = 0
    try:
        while not stop.wait(2):
            for name, child in children:
                if child.poll() is not None:
                    print(f'{name} exited with status {child.returncode}', flush=True)
                    exit_code = child.returncode or 1
                    stop.set()
                    break
    finally:
        stop.set()
        thread.join(timeout=15)
        for name, child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for name, child in children:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        snapshot()
    return exit_code

if __name__ == '__main__':
    sys.exit(main())
