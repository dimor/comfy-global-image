# ComfyUI with global storage

CUDA 13.0.0, PyTorch/TorchAudio 2.13.0 (cu130), ComfyUI v0.38.0, Python 3.12 and JupyterLab 4.6.4 with its file browser. Linux amd64, intended for RunPod GPU Pods including RTX 5090. A build is fixed; nothing upgrades at Pod startup. Fast preflight tests run before the CUDA build. The built image must then pass CPU startup, Manager API, interface and fresh-container persistence tests before publishing. GPU inference and actual global-volume behavior still require a RunPod test.

## RunPod settings

Image: `ghcr.io/dimor/comfy-global-image:comfy0.38.0-cuda13.0-v6` (available after the build succeeds and the GHCR package is public).

Attach a **global** volume at `/workspace`. Explicitly set this mount path during deployment. Leave the Docker/start command empty. Expose HTTP ports `8188,8888`; use a 150 GB container disk for the H3 local cache. The supplied settings use `JUPYTER_NO_AUTH=1`, so JupyterLab opens directly on port 8888. For a protected Jupyter instead, remove that variable and set `JUPYTER_TOKEN`; without a supplied token, a random one is generated once in `/workspace/.comfy-image/jupyter-token.txt`.

The image refuses to start without a mount at `/workspace`; it cannot determine the volume's type, so select the global volume in RunPod. There are no startup pip installs, git pulls or automatic model downloads. A new host must still pull the container image. Remote storage and model loading also take time.

## Persistent layout

| Path | Contents |
| --- | --- |
| `/workspace/ComfyUI/models` | Model weights and LoRAs in their usual category folders |
| `/workspace/ComfyUI/input` | Inputs |
| `/workspace/ComfyUI/output` | Results |
| `/workspace/ComfyUI/user` | User settings and saved workflows |
| `/workspace/ComfyUI/custom_nodes` | Optional custom-node source files |
| `/workspace/.cache` | Hugging Face and Torch caches |
| `/workspace/.comfy-image` | Credentials and database snapshots |

ComfyUI code, Python and preinstalled libraries are in the image at `/opt`, following the selected image + global-storage architecture. A node whose source is on the volume still needs its Python dependencies included in the image: add its installation to the Dockerfile, then rebuild. Manager is enabled by default so its model download controls are available; set `ENABLE_MANAGER=0` to disable it. The image patches Manager's permission-preserving copies because RunPod Global Volumes reject Unix `chmod`, while retaining normal data copies. Manager installation/update actions are not a durable way to change the image. Use image rebuilds for stable changes.

Set `JUPYTER_NO_AUTH=1` to open Jupyter without a login, as used by the provided Runpod settings. Omit it to require `JUPYTER_TOKEN` instead.

Existing model folders at `/workspace/runpod-slim/ComfyUI/models` and `/workspace/models` are automatically registered in place, including older `clip` and `unet` category names. Model files are not copied, moved or deleted. Other layouts require an additional model-path mapping. Attaching the same volume at a new path preserves its existing contents.

Global volumes lack locking and atomic rename. ComfyUI's SQLite database therefore runs locally and gets a consistent backup to the volume every two minutes and at graceful shutdown. Startup validates snapshots and falls back to the previous snapshot if the current one is incomplete. Abrupt termination can lose database changes since the last snapshot. Ordinary workflow/settings/result files are stored directly on the volume. Jupyter file saves disable atomic renames and trash moves for this filesystem. Use one writing Pod at a time. Git clones, dependency installation and arbitrary custom nodes may require POSIX features absent on global volumes.

## Build

Push to `main` or run the GitHub Actions workflow manually. It builds, tests, then publishes to GHCR. Set the package visibility to public after the first publication for credential-free RunPod pulls. Build artifacts contain no models, user files or credentials.

Local alternative with Docker installed:

```powershell
docker build -t comfy-global-image .
```

Published versions can be pinned by image digest for stronger reproducibility. Keep using the fixed version tag rather than `latest` for repeat deployments. Record the digest after a successful build. The GitHub build does not rent a GPU or deploy a Pod.

## Built-in on-demand model cache

Version v4 contains the cache launcher at `/opt/lazy-cache-main.py`. No script on
the volume, command override, model list or image rebuild is required. A brand-new
empty Global Volume starts normally.

When a workflow first requests a model, LoRA, VAE or text encoder of at least 64
MB, Comfy transparently copies only that selected file to `/tmp/comfy-model-cache`
and loads the local copy. Unused models stay only on Global. Later uses in the same
container reuse the cached file. Inputs, outputs, settings and original weights
stay on Global. Cache files are temporary and must be copied again after a fresh
container starts. If local space is insufficient, Comfy falls back to the Global
file instead of failing the workflow.

Run `model-cache-status --watch` in a Jupyter terminal for a live table with each
requested model's state, percentage, copied and total bytes, speed, and ETA. The
same details, including Global source and local destination, are stored in
`/workspace/ComfyUI/model-cache-status.json` and progress also appears in pod
logs. Jupyter and Comfy start immediately; copying begins when the selected model is first used.
Set `MODEL_CACHE_MIN_MB` to change the 64 MB threshold and
`MODEL_CACHE_RESERVE_GB` to change the 10 GB free-space reserve.

On the same RTX PRO 4000 pod ($0.57/hour compute), replaying the previous
five-second, 20-step H3 workflow after the container restart succeeded in 193.82
seconds, versus 1109.041 seconds for the earlier Global-read run. No execution
nodes were cached in either run. Copy plus generation was 394.72 seconds,
excluding container boot and verification work. This is one measured comparison;
other models, hosts and compilation state may behave differently. The test video
was saved to the persistent output directory as `video/H3_local_cache_test_00001_.mp4`.

The old v3 test template `hyy4uo6j7p` depends on an H3-specific script stored on
the original volume. Replace it with the v4 template after the fresh-volume test.
