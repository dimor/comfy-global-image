"""Start ComfyUI, caching H3 locally when its complete model set exists."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time

START = Path('/opt/start.py')
if not START.exists():
    START = Path(__file__).with_name('start.py')
spec = importlib.util.spec_from_file_location('image_start', START)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
original_paths = app.model_paths
CACHE = Path('/tmp/comfy-model-cache')
FILES = (
    'unet/minimax_h3_fl2va_pruned_int8_convrot.safetensors',
    'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
    'vae/minimax_h3_video_vae_int8_convrot.safetensors',
    'vae/minimax_h3_audio_vae_fp32.safetensors',
    'loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors',
)

def cached_paths(root, local):
    fallback = original_paths(root, local)
    source = root / 'runpod-slim/ComfyUI/models'
    cache = CACHE
    cache.mkdir(parents=True, exist_ok=True)
    progress = root / 'ComfyUI/h3-cache-status.json'
    started = time.monotonic()
    selected = [(source / name, cache / name) for name in FILES]
    missing = [str(src) for src, dst in selected if not src.is_file()]
    if missing:
        progress.write_text(json.dumps({'state': 'skipped',
            'reason': 'Complete H3 model set is not installed',
            'missing': missing}), encoding='utf-8')
        print('CACHE: H3 set is incomplete; starting Comfy normally. '
              'Install all five H3 files and restart to enable local caching.', flush=True)
        return fallback
    sizes = [src.stat().st_size for src, dst in selected]
    required = sum(size for (src, dst), size in zip(selected, sizes)
                   if not dst.exists() or dst.stat().st_size != size)
    free = shutil.disk_usage(cache).free
    if free < required + 10 * 1024**3:
        raise RuntimeError(f'Insufficient local disk: need {required / 1e9:.1f} GB plus 10 GB reserve; free {free / 1e9:.1f} GB')
    status = {'state': 'copying', 'total_bytes': sum(sizes), 'copied_bytes': 0,
              'started_at': time.time(), 'files': [], 'elapsed_seconds': 0}
    def report():
        status['elapsed_seconds'] = round(time.monotonic() - started, 1)
        progress.write_text(json.dumps(status), encoding='utf-8')
    report()
    for (src, dst), size in zip(selected, sizes):
        status['current_file'] = src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size == size:
            status['copied_bytes'] += size
            status['files'].append({'name': src.name, 'bytes': size, 'reused': True})
            report()
            continue
        print(f'CACHE: copying {src.name} ({size / 1e9:.2f} GB) to local disk', flush=True)
        partial = dst.with_name(dst.name + '.partial')
        last_report = time.monotonic()
        before = time.monotonic()
        try:
            with src.open('rb') as reader, partial.open('wb') as writer:
                while chunk := reader.read(8 * 1024**2):
                    writer.write(chunk)
                    status['copied_bytes'] += len(chunk)
                    if time.monotonic() - last_report >= 15:
                        report()
                        last_report = time.monotonic()
            if partial.stat().st_size != size or src.stat().st_size != size:
                raise RuntimeError(f'Model changed or incomplete copy: {src.name}')
            partial.replace(dst)
        except Exception as exc:
            status.update(state='failed', error=str(exc))
            report()
            partial.unlink(missing_ok=True)
            raise
        status['files'].append({'name': src.name, 'bytes': size,
                                'seconds': round(time.monotonic() - before, 1)})
        report()
    mappings = json.loads(fallback.read_text()) if fallback else {}
    # Prepend cache paths to Comfy's search order while keeping other models visible.
    preferred = {'h3_local_cache': {'base_path': str(cache), 'is_default': True,
                 'diffusion_models': 'unet', 'text_encoders': 'text_encoders',
                 'vae': 'vae', 'loras': 'loras'}}
    preferred.update(mappings)
    target = local / 'extra-model-paths-cached.yaml'
    target.write_text(json.dumps(preferred), encoding='utf-8')
    status.update(state='ready', cache_path=str(cache))
    report()
    print(f'CACHE: ready in {status["elapsed_seconds"]} seconds; H3 weights now load locally', flush=True)
    return target

app.model_paths = cached_paths
if __name__ == '__main__':
    sys.exit(app.main())
