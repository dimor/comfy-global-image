"""Run fixed image software against an attached persistent /workspace volume."""
import json
from contextlib import closing
import os
from pathlib import Path
import secrets
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

def secret(name):
    target = STATE / name
    if target.exists():
        return target.read_text().strip()
    value = secrets.token_urlsafe(24)
    target.write_text(value + '\n')
    return value

def restore_databases():
    for name in ('comfyui.db', 'filebrowser.db'):
        saved = STATE / name
        if saved.exists():
            shutil.copyfile(saved, LOCAL / name)

def snapshot():
    # SQLite's backup API yields a consistent snapshot without copying a live WAL.
    db = LOCAL / 'comfyui.db'
    if db.exists():
        try:
            backup = LOCAL / 'comfyui-backup.db'
            with closing(sqlite3.connect(db, timeout=10)) as src, closing(sqlite3.connect(backup)) as dst:
                src.backup(dst)
            shutil.copyfile(backup, STATE / 'comfyui.db')
        except Exception as exc:
            print(f'Database snapshot deferred: {exc}', flush=True)

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
    token = os.environ.get('JUPYTER_TOKEN') or secret('jupyter-token.txt')
    password = os.environ.get('FILEBROWSER_PASSWORD') or secret('filebrowser-password.txt')
    fb = LOCAL / 'filebrowser.db'
    if not fb.exists():
        subprocess.run(['filebrowser', '-d', str(fb), 'config', 'init'], check=True)
        subprocess.run(['filebrowser', '-d', str(fb), 'users', 'add', 'admin', password, '--perm.admin'], check=True, stdout=subprocess.DEVNULL)
        shutil.copyfile(fb, STATE / 'filebrowser.db')
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    launch('FileBrowser :8080', ['filebrowser', '-d', str(fb), '-r', str(ROOT), '-a', '0.0.0.0', '-p', '8080'])
    # Config file avoids printing the token in the command line or Jupyter startup URL.
    config = LOCAL / 'jupyter_config.py'
    config.write_text('c.IdentityProvider.token = ' + repr(token) + '\n' +
                      'c.ServerApp.root_dir = "/workspace"\n' +
                      'c.ServerApp.ip = "0.0.0.0"\n' +
                      'c.ServerApp.port = 8888\n' +
                      'c.ServerApp.allow_root = True\n' +
                      'c.ServerApp.open_browser = False\n' +
                      'c.ServerApp.log_level = 30\n')
    launch('JupyterLab :8888', ['jupyter', 'lab', '--config', str(config)])
    args = ['python', '/opt/ComfyUI/main.py', '--listen', '0.0.0.0', '--port', '8188',
            '--base-directory', str(DATA), '--user-directory', str(DATA / 'user'),
            '--database-url', 'sqlite:////tmp/comfy-image/comfyui.db',
            '--temp-directory', str(LOCAL / 'temp')]
    if os.environ.get('COMFY_CPU_TEST') == '1':
        args.append('--cpu')
    # Manager is available on demand; installation changes need an image rebuild.
    if os.environ.get('ENABLE_MANAGER') == '1':
        args.append('--enable-manager')
    launch('ComfyUI :8188', args)
    thread = threading.Thread(target=periodic_snapshot, daemon=True)
    thread.start()
    print('Persistent files: /workspace/ComfyUI. No git pull, pip install or model download at startup.', flush=True)
    print('Jupyter token and FileBrowser admin password: /workspace/.comfy-image/ (or set template environment variables).', flush=True)
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
        # FileBrowser uses BoltDB: copy only after its process has exited.
        if fb.exists():
            shutil.copyfile(fb, STATE / 'filebrowser.db')
    return exit_code

if __name__ == '__main__':
    sys.exit(main())
