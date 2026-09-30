"""Run ComfyUI with transparent, on-demand local caching of selected model files."""
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import threading
import time

CACHE = Path(os.environ.get('MODEL_CACHE_DIR', '/tmp/comfy-model-cache'))
STATUS = Path('/workspace/ComfyUI/model-cache-status.json')
SOURCE_ROOTS = tuple(Path(path) for path in (
    '/workspace/runpod-slim/ComfyUI/models',
    '/workspace/ComfyUI/models',
    '/workspace/models',
))
MIN_BYTES = int(float(os.environ.get('MODEL_CACHE_MIN_MB', '64')) * 1024**2)
RESERVE_BYTES = int(float(os.environ.get('MODEL_CACHE_RESERVE_GB', '10')) * 1024**3)
locks_guard = threading.Lock()
file_locks = {}
status_guard = threading.Lock()

def write_status(**values):
    try:
        with status_guard:
            current = {}
            if STATUS.exists():
                try:
                    current = json.loads(STATUS.read_text())
                except (OSError, json.JSONDecodeError):
                    pass
            current.update(values, updated_at=time.time())
            STATUS.parent.mkdir(parents=True, exist_ok=True)
            STATUS.write_text(json.dumps(current), encoding='utf-8')
    except OSError as exc:
        print(f'CACHE: could not write status: {exc}', flush=True)

def source_root(path):
    absolute = Path(path).absolute()
    for root in SOURCE_ROOTS:
        try:
            absolute.relative_to(root)
            return root
        except ValueError:
            continue
    return None

def lock_for(path):
    with locks_guard:
        return file_locks.setdefault(str(path), threading.Lock())

def copy_to_cache(source, folder_name, filename):
    size = source.stat().st_size
    if size < MIN_BYTES:
        return str(source)
    safe_name = Path(os.path.relpath(os.path.join('/', filename), '/'))
    destination = CACHE / folder_name / safe_name
    with lock_for(destination):
        if destination.is_file() and destination.stat().st_size == size:
            print(f'CACHE: reuse {filename} from local disk', flush=True)
            return str(destination)
        free = shutil.disk_usage(CACHE).free
        if free < size + RESERVE_BYTES:
            print(f'CACHE: insufficient local disk for {filename}; reading from Global', flush=True)
            write_status(state='fallback', current=filename,
                         reason='insufficient local disk', required_bytes=size,
                         free_bytes=free)
            return str(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + '.partial')
        partial.unlink(missing_ok=True)
        started = time.monotonic()
        print(f'CACHE: copying selected model {filename} ({size / 1e9:.2f} GB)', flush=True)
        write_status(state='copying', current=filename, total_bytes=size,
                     copied_bytes=0)
        copied = 0
        last_report = started
        try:
            with source.open('rb') as reader, partial.open('wb') as writer:
                while chunk := reader.read(8 * 1024**2):
                    writer.write(chunk)
                    copied += len(chunk)
                    if time.monotonic() - last_report >= 15:
                        write_status(state='copying', current=filename,
                                     total_bytes=size, copied_bytes=copied)
                        last_report = time.monotonic()
            if partial.stat().st_size != size or source.stat().st_size != size:
                raise RuntimeError(f'source changed or copy incomplete: {filename}')
            partial.replace(destination)
        except Exception as exc:
            partial.unlink(missing_ok=True)
            write_status(state='failed', current=filename, error=str(exc))
            raise
        elapsed = round(time.monotonic() - started, 1)
        write_status(state='ready', current=filename, total_bytes=size,
                     copied_bytes=size, elapsed_seconds=elapsed,
                     cache_path=str(destination))
        print(f'CACHE: {filename} ready locally in {elapsed} seconds', flush=True)
        return str(destination)

def install(folder_paths):
    original = folder_paths.get_full_path
    def cached_get_full_path(folder_name, filename):
        resolved = original(folder_name, filename)
        if not resolved:
            return resolved
        source = Path(resolved)
        if source_root(source) is None or not source.is_file():
            return resolved
        try:
            return copy_to_cache(source, folder_name, filename)
        except Exception as exc:
            print(f'CACHE: failed for {filename}: {exc}; reading from Global', flush=True)
            return resolved
    folder_paths.get_full_path = cached_get_full_path
    write_status(state='waiting', message='A selected model will be copied on first use')
    print('CACHE: on-demand model cache enabled; unused models stay on Global', flush=True)

def main():
    comfy = '/opt/ComfyUI'
    sys.path.insert(0, comfy)
    CACHE.mkdir(parents=True, exist_ok=True)
    import folder_paths
    install(folder_paths)
    runpy.run_path(f'{comfy}/main.py', run_name='__main__')

if __name__ == '__main__':
    main()
