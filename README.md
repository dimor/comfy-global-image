# ComfyUI with global storage

CUDA 13.0.0, PyTorch 2.13.0 (cu130), ComfyUI v0.38.0, Python 3.12 and JupyterLab 4.6.4 with its file browser. Linux amd64, intended for RunPod GPU Pods including RTX 5090. A build is fixed; nothing upgrades at Pod startup. Fast preflight tests run before the CUDA build. The built image must then pass CPU startup, Manager API, interface and fresh-container persistence tests before publishing. GPU inference and actual global-volume behavior still require a RunPod test.

## RunPod settings

Image: `ghcr.io/dimor/comfy-global-image:comfy0.38.0-cuda13.0-v8` (available after the build succeeds and the GHCR package is public).

Attach a **global** volume at `/workspace`. Explicitly set this mount path during deployment. Leave the Docker/start command empty. Expose HTTP ports `8188,8888`; use a 150 GB container disk for the H3 local cache. Jupyter authentication is always enabled. Use the reusable deployment procedure below to inject private credentials.

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

The image contains the cache launcher at `/opt/lazy-cache-main.py`. No script on
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

## Built-in Download to Pod

The hardened `ComfyUI-RunpodDirect` extension is fixed inside the image and is
available even with a completely empty Global Volume. Its **Download to Pod**
button writes new models directly into `/workspace/ComfyUI/models`, so they stay
on the Global Volume and are available to later Pods without rebuilding the
image. Runtime settings persist at
`/workspace/.comfy-image/runpoddirect-settings.json`.

Downloads use four connections by default, verify size and optional SHA-256,
keep incomplete files separate, reject private-network URLs and unsafe paths,
and expose live progress in the ComfyUI interface. Set
`RPD_DOWNLOAD_CONNECTIONS` to change the connection count. The optional cgroup
RAM override is disabled unless `RPD_ENABLE_CGROUP_RAM_PATCH=1` is explicitly
set.

On the same RTX PRO 4000 pod ($0.57/hour compute), replaying the previous
five-second, 20-step H3 workflow after the container restart succeeded in 193.82
seconds, versus 1109.041 seconds for the earlier Global-read run. No execution
nodes were cached in either run. Copy plus generation was 394.72 seconds,
excluding container boot and verification work. This is one measured comparison;
other models, hosts and compilation state may behave differently. The test video
was saved to the persistent output directory as `video/H3_local_cache_test_00001_.mp4`.

The old v3 test template `hyy4uo6j7p` depends on an H3-specific script stored on
the original volume. Replace it with the v4 template after the fresh-volume test.


## Reusable private Jupyter deployment (v8)

The v8 source supports `JUPYTER_TOKEN` and `JUPYTER_PASSWORD` as matching token
aliases. It rejects authentication bypasses and mismatched values. On RunPod both
are required. Startup derives the exact HTTPS origin from `RUNPOD_POD_ID`, enables
`ServerApp.trust_xheaders`, and keeps Jupyter XSRF and cookie authentication enabled.
The generated Jupyter config lives privately in `/tmp/comfy-image/jupyter_config.py`;
no credential is baked into the image. Existing volume data is untouched.

Each user must once bind their RunPod API key in their own secret manager/runtime,
choose a stable private user ID, and provision a private persistent token store
outside any Git repository. The default is `~/.local/state/comfy-runpod` on a
persistent personal workstation (0700 directories, 0600 files). Back it up securely;
loss of this store loses token continuity. Ephemeral workspaces must use a durable
private mount. Separate OS accounts are required for users running this CLI; a
multiuser service must protect the store and obtain the user ID from its authenticated
session, never from an untrusted request. Never share a user ID or token store login.

Prepare a complete RunPod **REST create-pod** request privately, preserving all
existing environment variables, GPU choices and Global volume selection. The
`runpod-settings.json` file is a reference, not an API request. REST `ports` must be
an array including `8188/http` and `8888/http`; mount the selected Global volume at
`/workspace`, and leave startup command overrides empty for the standard image launcher. Pin the successfully rebuilt
v8 image digest; the old v7 image does not implement these changes. Do not invent or
replace a volume ID. The wrapper only changes the two Jupyter credentials and removes
the obsolete no-auth and stale pod-ID overrides.

The inspected account template `3sesj934xz` instead has a persistent-application
bootstrap that imports `/opt/start.py` and calls `launcher.main()`. The wrapper
preserves that bootstrap, entrypoint, disk, ports and environment defaults with
`--template-id 3sesj934xz --image REBUILT_V8_DIGEST`. Supply deployment-specific
GPU and existing volume choices in the private request JSON; request fields override
template defaults. This template currently pins v7 and is not itself updated. Its
bootstrap uses a Network Volume; preserve that existing storage selection unless
you explicitly intend to migrate. Other users must use their own authorized template.

```sh
python deploy_runpod.py --user-id YOUR_STABLE_USER_ID --request /private/pod-request.json
# After authorizing rental of the specified replacement pod:
python deploy_runpod.py --user-id YOUR_STABLE_USER_ID --request /private/pod-request.json --deploy
```

Preparation generates a token once per user and reuses it across replacements.
Deployment saves a private `<pod-id>-launch.json` containing the launch URL under
the user's hashed directory. Open that URL locally; do not paste its contents into
chat, logs or GitHub. Prepared API requests contain credentials and stay in that
same private store. API errors are redacted and allocation requests are never retried
automatically. This wrapper creates a new pod and does not stop/delete existing pods.
Use one writing pod per Global volume; stop the old writer before the replacement
starts, once downtime is authorized.

Validate a new deployment with `verify_jupyter.py --launch-file /private/path/POD-launch.json`.
It checks unauthorized rejection, token login, cookie-only HTTP access and terminal
WebSocket command input/output with the exact origin. Install `websocket-client` in
the local verification environment. It creates and deletes a temporary terminal.

Status: source and local test automation are provided; no rebuilt image, live template
update or replacement-pod verification is implied. Future pods must go through this
wrapper using the rebuilt image. Deploying through an older saved RunPod template or
the console alone does not automatically inject these credentials.
