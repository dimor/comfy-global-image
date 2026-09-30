# ComfyUI with global storage

CUDA 13.0.0, PyTorch 2.13.0 (cu130), ComfyUI v0.38.0, Python 3.12, JupyterLab and FileBrowser. Linux amd64, intended for RunPod GPU Pods including RTX 5090. A build is fixed; nothing upgrades at Pod startup. The Actions job checks CPU startup and the three interfaces before publishing. GPU inference and actual global-volume behavior still require a RunPod test.

## RunPod settings

Image: `ghcr.io/dimor/comfy-global-image:comfy0.38.0-cuda13.0-v1` (available after the build succeeds and the GHCR package is public).

Attach your existing **global** volume at `/workspace`. Explicitly set this mount path during deployment. Leave the Docker/start command empty. Expose HTTP ports `8188,8888,8080`; use a 30 GB container disk initially. Set `JUPYTER_TOKEN` and `FILEBROWSER_PASSWORD` in the template. FileBrowser username is `admin`. Without supplied credentials, random credentials are generated once in `/workspace/.comfy-image/`; retrieve them through the RunPod console or set your own in the template.

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

Existing model folders elsewhere on the volume are not automatically moved or deleted. Put them into the paths above, or supply ComfyUI's extra model paths configuration in a follow-up revision. Attaching the same volume at a new path preserves its existing contents.

Global volumes lack locking and atomic rename. SQLite and FileBrowser databases therefore run locally. ComfyUI's database gets a SQLite backup to the volume every two minutes and at graceful shutdown. FileBrowser's configuration database is saved at graceful shutdown. Abrupt termination can lose changes since the last snapshot. Ordinary workflow/settings/result files are stored directly on the volume. Use one writing Pod at a time. Git clones, dependency installation and arbitrary custom nodes may require POSIX features absent on global volumes.

## Build

Push to `main` or run the GitHub Actions workflow manually. It builds, tests, then publishes to GHCR. Set the package visibility to public after the first publication for credential-free RunPod pulls. Build artifacts contain no models, user files or credentials.

Local alternative with Docker installed:

```powershell
docker build -t comfy-global-image .
```

Published versions can be pinned by image digest for stronger reproducibility. Keep using the fixed version tag rather than `latest` for repeat deployments. Record the digest after a successful build. The GitHub build does not rent a GPU or deploy a Pod.
