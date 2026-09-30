# ComfyUI with global storage

CUDA 13.0.0, PyTorch 2.13.0 (cu130), ComfyUI v0.38.0, Python 3.12 and JupyterLab 4.6.4 with its file browser. Linux amd64, intended for RunPod GPU Pods including RTX 5090. A build is fixed; nothing upgrades at Pod startup. Fast preflight tests run before the CUDA build. The built image must then pass CPU startup, interface and fresh-container persistence tests before publishing. GPU inference and actual global-volume behavior still require a RunPod test.

## RunPod settings

Image: `ghcr.io/dimor/comfy-global-image:comfy0.38.0-cuda13.0-v4` (available after the build succeeds and the GHCR package is public).

Attach a **global** volume at `/workspace`. Explicitly set this mount path during deployment. Leave the Docker/start command empty. Expose HTTP ports `8188,8888`; use a 150 GB container disk for the H3 local cache. Set `JUPYTER_TOKEN` in the template. Without a supplied token, a random token is generated once in `/workspace/.comfy-image/jupyter-token.txt`; retrieve it through the RunPod console or set your own in the template. Manage your files through JupyterLab on port 8888.

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

ComfyUI code, Python and preinstalled libraries are in the image at `/opt`, following the selected image + global-storage architecture. A node whose source is on the volume still needs its Python dependencies included in the image: add its installation to the Dockerfile, then rebuild. Manager is installed but disabled by default; `ENABLE_MANAGER=1` enables it. Manager installation/update actions are not a durable way to change the image and may fail on the global filesystem. Use image rebuilds for stable changes.

Existing model folders at `/workspace/runpod-slim/ComfyUI/models` and `/workspace/models` are automatically registered in place, including older `clip` and `unet` category names. Model files are not copied, moved or deleted. Other layouts require an additional model-path mapping. Attaching the same volume at a new path preserves its existing contents.

Global volumes lack locking and atomic rename. ComfyUI's SQLite database therefore runs locally and gets a consistent backup to the volume every two minutes and at graceful shutdown. Startup validates snapshots and falls back to the previous snapshot if the current one is incomplete. Abrupt termination can lose database changes since the last snapshot. Ordinary workflow/settings/result files are stored directly on the volume. Jupyter file saves disable atomic renames and trash moves for this filesystem. Use one writing Pod at a time. Git clones, dependency installation and arbitrary custom nodes may require POSIX features absent on global volumes.

## Build

Push to `main` or run the GitHub Actions workflow manually. It builds, tests, then publishes to GHCR. Set the package visibility to public after the first publication for credential-free RunPod pulls. Build artifacts contain no models, user files or credentials.

Local alternative with Docker installed:

```powershell
docker build -t comfy-global-image .
```

Published versions can be pinned by image digest for stronger reproducibility. Keep using the fixed version tag rather than `latest` for repeat deployments. Record the digest after a successful build. The GitHub build does not rent a GPU or deploy a Pod.

## Built-in H3 local cache

Version v4 contains the cache launcher at `/opt/cache-start.py` and runs it by
default. No script on the volume and no command override are required. A brand-new
empty Global Volume starts normally. After all five expected H3 files are installed
in the Slim model layout, the next container start automatically enables caching.

The launcher copies five specific H3 files from the existing Slim model layout
to `/tmp/comfy-model-cache`, checks disk capacity and copied sizes, then registers
local paths first in Comfy's search order. Other models keep loading from Global.
Inputs, outputs, settings and original weights stay on Global. Cache files are
temporary and must be copied again after a fresh container starts.

Progress is visible in `/workspace/ComfyUI/h3-cache-status.json`. Jupyter starts
before copying; Comfy starts after the cache is ready. The first live test copied
42,030,035,159 bytes in 200.9 seconds. A separate live check resolved all five H3
files to local paths and parsed their safetensors headers successfully. This
transfer measurement is not an inference performance guarantee.

On the same RTX PRO 4000 pod ($0.57/hour compute), replaying the previous
five-second, 20-step H3 workflow after the container restart succeeded in 193.82
seconds, versus 1109.041 seconds for the earlier Global-read run. No execution
nodes were cached in either run. Copy plus generation was 394.72 seconds,
excluding container boot and verification work. This is one measured comparison;
other models, hosts and compilation state may behave differently. The test video
was saved to the persistent output directory as `video/H3_local_cache_test_00001_.mp4`.

The old v3 test template `hyy4uo6j7p` depends on a script stored on the original
volume. Replace it with the v4 template after the fresh-volume test passes.
