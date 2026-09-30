"""Run ComfyUI with transparent, on-demand local caching of selected model files."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import runpy
import shutil
import sys
import threading
import time

CACHE = Path(os.environ.get('MODEL_CACHE_DIR', '/tmp/comfy-model-cache'))
STATUS = Path(os.environ.get('MODEL_CACHE_STATUS', '/workspace/ComfyUI/model-cache-status.json'))
SOURCE_ROOTS = tuple(Path(path) for path in (
    '/workspace/runpod-slim/ComfyUI/models',
    '/workspace/ComfyUI/models',
    '/workspace/models',
))
MIN_BYTES = int(float(os.environ.get('MODEL_CACHE_MIN_MB', '64')) * 1024**2)
RESERVE_BYTES = int(float(os.environ.get('MODEL_CACHE_RESERVE_GB', '10')) * 1024**3)
REPORT_SECONDS = float(os.environ.get('MODEL_CACHE_REPORT_SECONDS', '5'))
locks_guard = threading.Lock()
file_locks = {}
status_guard = threading.Lock()
status_data = {}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def save_status():
    """Write plainly because object-backed Global Volumes may not support atomic rename."""
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(status_data, indent=2), encoding='utf-8')
    except OSError as exc:
        print(f'CACHE: could not write status: {exc}', flush=True)


def set_overall(state, message):
    with status_guard:
        status_data.update(
            state=state,
            message=message,
            cache_directory=str(CACHE),
            status_file=str(STATUS),
            updated_at=now(),
        )
        status_data.setdefault('models', {})
        save_status()


def update_model(model_id, **values):
    with status_guard:
        models = status_data.setdefault('models', {})
        model = models.setdefault(model_id, {})
        model.update(values, updated_at=now())
        states = [item.get('state') for item in models.values()]
        status_data['state'] = 'copying' if 'copying' in states else values.get('state', 'waiting')
        status_data['message'] = values.get('message', status_data.get('message', ''))
        status_data['updated_at'] = now()
        save_status()


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


def progress_values(copied, size, started):
    elapsed = max(time.monotonic() - started, 0.001)
    speed = copied / elapsed
    return {
        'copied_bytes': copied,
        'percent': round(copied * 100 / size, 1),
        'elapsed_seconds': round(elapsed, 1),
        'speed_mib_per_second': round(speed / 1024**2, 1),
        'eta_seconds': round((size - copied) / speed, 1) if speed else None,
    }


def copy_to_cache(source, folder_name, filename):
    size = source.stat().st_size
    if size < MIN_BYTES:
        return str(source)
    safe_name = Path(os.path.relpath(os.path.join('/', filename), '/'))
    destination = CACHE / folder_name / safe_name
    model_id = f'{folder_name}/{safe_name.as_posix()}'
    details = {
        'name': filename,
        'category': folder_name,
        'source': str(source),
        'local_copy': str(destination),
        'total_bytes': size,
    }
    with lock_for(destination):
        if destination.is_file() and destination.stat().st_size == size:
            update_model(model_id, **details, state='ready', copied_bytes=size,
                         percent=100.0, message='Reusing local copy')
            print(f'CACHE: READY {model_id} (reusing local copy)', flush=True)
            return str(destination)
        free = shutil.disk_usage(CACHE).free
        if free < size + RESERVE_BYTES:
            update_model(model_id, **details, state='fallback', copied_bytes=0,
                         percent=0.0, free_bytes=free,
                         message='Not enough local disk; reading from Global')
            print(f'CACHE: FALLBACK {model_id} - insufficient local disk', flush=True)
            return str(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + '.partial')
        partial.unlink(missing_ok=True)
        started = time.monotonic()
        update_model(model_id, **details, state='copying', copied_bytes=0,
                     percent=0.0, message='Copying selected model from Global')
        print(f'CACHE: COPYING {model_id} (0.0% of {size / 1e9:.2f} GB)', flush=True)
        copied = 0
        last_report = started
        try:
            with source.open('rb') as reader, partial.open('wb') as writer:
                while chunk := reader.read(8 * 1024**2):
                    writer.write(chunk)
                    copied += len(chunk)
                    if time.monotonic() - last_report >= REPORT_SECONDS:
                        progress = progress_values(copied, size, started)
                        update_model(model_id, **details, state='copying', **progress,
                                     message='Copying selected model from Global')
                        print(
                            f"CACHE: COPYING {model_id} {progress['percent']:.1f}% "
                            f"at {progress['speed_mib_per_second']:.1f} MiB/s, "
                            f"ETA {progress['eta_seconds']:.0f}s",
                            flush=True,
                        )
                        last_report = time.monotonic()
            if partial.stat().st_size != size or source.stat().st_size != size:
                raise RuntimeError(f'source changed or copy incomplete: {filename}')
            partial.replace(destination)
        except Exception as exc:
            partial.unlink(missing_ok=True)
            update_model(model_id, **details, state='failed', copied_bytes=copied,
                         error=str(exc), message='Copy failed; reading from Global')
            raise
        progress = progress_values(size, size, started)
        update_model(model_id, **details, state='ready', **progress,
                     message='Selected model is ready on local disk')
        print(
            f"CACHE: READY {model_id} in {progress['elapsed_seconds']:.1f}s "
            f"({progress['speed_mib_per_second']:.1f} MiB/s)",
            flush=True,
        )
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
            print(f'CACHE: FAILED {folder_name}/{filename}: {exc}; reading from Global', flush=True)
            return resolved

    folder_paths.get_full_path = cached_get_full_path
    set_overall('waiting', 'Waiting for a workflow to request a model')
    print('CACHE: enabled. Run model-cache-status to see live progress.', flush=True)


def main():
    comfy_root = '/opt/ComfyUI'
    sys.path.insert(0, comfy_root)
    CACHE.mkdir(parents=True, exist_ok=True)
    # main.py does this before importing folder_paths. We must preserve that order,
    # otherwise flags such as --cpu and --listen are silently ignored.
    import comfy.options
    comfy.options.enable_args_parsing()
    import folder_paths
    install(folder_paths)
    runpy.run_path(f'{comfy_root}/main.py', run_name='__main__')


if __name__ == '__main__':
    main()
