"""
ComfyUI RunpodDirect - Direct Model Downloads for RunPod
Download models directly to your RunPod instance with multi-connection support
"""

import os
import json
import logging
import asyncio
import hashlib
import ipaddress
import re
import shutil
import socket
import time
import threading
from urllib.parse import urlparse
import folder_paths
import comfy.model_management as comfy_mm
from aiohttp import web
from aiohttp.abc import AbstractResolver
from server import PromptServer

# Track active downloads
active_downloads = {}
# Download control (for pause/resume)
download_control = {}
# Download queue management
download_queue = []
current_download_task = None  # Only one download at a time

# Configuration optimized for datacenter connections (Runpod)
CHUNK_SIZE = 32 * 1024 * 1024  # 32MB chunks - balanced for 500MB to 30GB+ files
NUM_CONNECTIONS = max(1, min(8, int(os.environ.get("RPD_DOWNLOAD_CONNECTIONS", "4"))))
CHUNK_MAX_RETRIES = 4
PROGRESS_EVENT_INTERVAL = 0.1  # seconds
SOCK_CONNECT_TIMEOUT = 30
SOCK_READ_TIMEOUT = 120
HASH_READ_CHUNK_SIZE = 8 * 1024 * 1024
WS_KEEPALIVE_INTERVAL_SECONDS = 45
SETTINGS_FILENAME = "runpoddirect_settings.json"
PREQUEUE_CHECK_MAX_MODELS = 512
MAX_QUEUE_SIZE = int(os.environ.get("RPD_MAX_QUEUE_SIZE", "64"))
MIN_FREE_BYTES = int(float(os.environ.get("RPD_MIN_FREE_GB", "2")) * 1024 ** 3)
MAX_DOWNLOAD_BYTES = int(float(os.environ.get("RPD_MAX_DOWNLOAD_GB", "0")) * 1024 ** 3)
MAX_HISTORY_SIZE = max(32, int(os.environ.get("RPD_MAX_HISTORY", "512")))

_ws_keepalive_task = None
_ws_keepalive_enabled = True
_cgroup_ram_patch_applied = False
_cgroup_ram_patch_enabled = False
_original_virtual_memory_fn = None
_settings_file_lock = threading.Lock()


async def _ws_keepalive_loop():
    """Send lightweight periodic websocket traffic to reduce proxy idle disconnects."""
    while True:
        try:
            server = getattr(PromptServer, "instance", None)
            sockets = getattr(server, "sockets", None) if server is not None else None
            if _ws_keepalive_enabled and server is not None and sockets:
                stale_ids = []
                for sid, ws in list(sockets.items()):
                    try:
                        if ws is None or ws.closed:
                            stale_ids.append(sid)
                            continue
                        # Use websocket ping control frame so frontend APIs stay silent.
                        await ws.ping()
                    except Exception:
                        stale_ids.append(sid)
                for sid in stale_ids:
                    sockets.pop(sid, None)
                    metadata = getattr(server, "sockets_metadata", None)
                    if metadata is not None:
                        metadata.pop(sid, None)
        except Exception:
            # Silent by design: keepalive should never create user-facing log noise.
            pass
        await asyncio.sleep(WS_KEEPALIVE_INTERVAL_SECONDS)


def _ensure_ws_keepalive_task():
    global _ws_keepalive_task
    if _ws_keepalive_task is not None and not _ws_keepalive_task.done():
        return
    try:
        server = getattr(PromptServer, "instance", None)
        loop = getattr(server, "loop", None) if server is not None else None
        if loop is None:
            return
        _ws_keepalive_task = loop.create_task(_ws_keepalive_loop())
    except Exception:
        _ws_keepalive_task = None


def _parse_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return default


def _read_int_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw or raw == "max":
            return None
        return int(raw)
    except Exception:
        return None


def _read_cgroup_memory_limit_and_usage():
    # cgroup v2
    v2_limit = _read_int_file("/sys/fs/cgroup/memory.max")
    v2_usage = _read_int_file("/sys/fs/cgroup/memory.current")
    if v2_limit is not None or v2_usage is not None:
        limit = v2_limit
        usage = v2_usage if v2_usage is not None else 0
    else:
        # cgroup v1
        limit = _read_int_file("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        usage = _read_int_file("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        usage = usage if usage is not None else 0

    # Very large limits in cgroup v1 commonly mean "unlimited".
    if limit is not None and limit >= (1 << 60):
        limit = None
    if usage < 0:
        usage = 0
    return limit, usage


def _patch_comfy_ram_detection_for_cgroups():
    """Monkey-patch ComfyUI RAM detection to respect cgroup memory limits."""
    global _cgroup_ram_patch_applied
    global _cgroup_ram_patch_enabled
    global _original_virtual_memory_fn
    if _cgroup_ram_patch_applied:
        return

    env_disabled = _parse_bool(os.environ.get("RPD_DISABLE_CGROUP_RAM_PATCH"), default=False)
    env_enabled = _parse_bool(os.environ.get("RPD_ENABLE_CGROUP_RAM_PATCH"), default=False)
    if env_disabled or not env_enabled:
        _cgroup_ram_patch_enabled = False
        logging.info("[RunpodDirect] Cgroup RAM patch disabled (explicit opt-in required)")
        return

    try:
        original_virtual_memory = comfy_mm.psutil.virtual_memory
        _original_virtual_memory_fn = original_virtual_memory
        sample = original_virtual_memory()
        host_total = int(getattr(sample, "total", 0) or 0)
        host_available = int(getattr(sample, "available", 0) or 0)
        limit, usage = _read_cgroup_memory_limit_and_usage()
        if not limit or host_total <= 0 or limit >= host_total:
            return

        def _virtual_memory_cgroup_aware():
            vm = original_virtual_memory()
            if not _cgroup_ram_patch_enabled:
                return vm
            vm_total = int(getattr(vm, "total", 0) or 0)
            vm_available = int(getattr(vm, "available", 0) or 0)
            c_limit, c_usage = _read_cgroup_memory_limit_and_usage()
            if not c_limit or vm_total <= 0 or c_limit >= vm_total:
                return vm
            effective_total = min(vm_total, c_limit)
            cgroup_free = max(c_limit - (c_usage or 0), 0)
            effective_available = min(vm_available, cgroup_free, effective_total)
            try:
                return vm._replace(total=effective_total, available=effective_available)
            except Exception:
                return vm

        comfy_mm.psutil.virtual_memory = _virtual_memory_cgroup_aware
        _cgroup_ram_patch_applied = True
        _cgroup_ram_patch_enabled = True

        limit_gib = limit / (1024 ** 3)
        host_gib = host_total / (1024 ** 3)
        avail_gib = min(host_available, max(limit - usage, 0)) / (1024 ** 3)
        logging.info(
            f"[RunpodDirect] Cgroup RAM patch enabled: host={host_gib:.2f} GiB, "
            f"limit={limit_gib:.2f} GiB, available={avail_gib:.2f} GiB"
        )
    except Exception as e:
        logging.warning(f"[RunpodDirect] Failed to apply cgroup RAM patch: {e}")


def _settings_file_path():
    return os.environ.get(
        "RPD_SETTINGS_PATH",
        os.path.join(os.path.dirname(__file__), SETTINGS_FILENAME),
    )


def _parse_download_url(url):
    """Accept only public HTTPS URLs without embedded credentials."""
    if not isinstance(url, str):
        raise ValueError("Download URL must be a string")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("Only HTTPS download URLs are allowed")
    if not parsed.hostname:
        raise ValueError("Download URL is missing a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Download URLs must not contain embedded credentials")
    return parsed


def _display_url(url):
    """Keep useful source information without exposing signed query parameters."""
    parsed = _parse_download_url(url)
    return parsed._replace(query="", fragment="").geturl()


def _prune_download_history(target_size=MAX_HISTORY_SIZE):
    """Bound terminal status history so thousands of installs do not grow forever."""
    if len(active_downloads) <= target_size:
        return
    terminal = {"completed", "error", "cancelled"}
    for download_id, details in list(active_downloads.items()):
        if details.get("status") in terminal:
            active_downloads.pop(download_id, None)
            if len(active_downloads) <= target_size:
                break


def _validate_range_response(response, requested_start, requested_end, total_size):
    if response.status != 206:
        return
    value = response.headers.get("content-range", "")
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value.strip(), re.IGNORECASE)
    if not match:
        raise Exception("Range response is missing a valid Content-Range header")
    actual_start, actual_end, actual_total = match.groups()
    if int(actual_start) != requested_start or int(actual_end) != requested_end:
        raise Exception(
            f"Range response mismatch: requested {requested_start}-{requested_end}, "
            f"received {actual_start}-{actual_end}"
        )
    if actual_total != "*" and int(actual_total) != total_size:
        raise Exception(
            f"Range response total mismatch: expected {total_size}, received {actual_total}"
        )


def _is_huggingface_url(url):
    try:
        host = (_parse_download_url(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "huggingface.co" or host.endswith(".huggingface.co")


class _PublicOnlyResolver(AbstractResolver):
    """Resolve hosts once and refuse loopback, private, link-local, or reserved IPs."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host,
            port,
            family=family,
            type=socket.SOCK_STREAM,
        )
        resolved = []
        seen = set()
        for addr_family, _socktype, proto, _canonname, address in infos:
            address_text = address[0]
            if address_text in seen:
                continue
            seen.add(address_text)
            if not ipaddress.ip_address(address_text).is_global:
                raise OSError(f"Refusing non-public download address for {host}")
            resolved.append({
                "hostname": host,
                "host": address_text,
                "port": port,
                "family": addr_family,
                "proto": proto,
                "flags": socket.AI_NUMERICHOST,
            })
        if not resolved:
            raise OSError(f"No public address found for {host}")
        return resolved

    async def close(self):
        return None


def _build_runtime_settings_payload():
    return {
        "keepalive_enabled": bool(_ws_keepalive_enabled),
        "cgroup_ram_patch_enabled": bool(_cgroup_ram_patch_enabled),
    }


def _persist_runtime_settings():
    settings_path = _settings_file_path()
    temp_path = f"{settings_path}.tmp"
    payload = _build_runtime_settings_payload()
    try:
        with _settings_file_lock:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(temp_path, settings_path)
        return True
    except Exception as e:
        logging.warning(f"[RunpodDirect] Failed to persist settings: {e}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return False


def _load_persisted_settings():
    global _ws_keepalive_enabled
    global _cgroup_ram_patch_enabled

    settings_path = _settings_file_path()
    if not os.path.exists(settings_path):
        return

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return

        if "keepalive_enabled" in data:
            _ws_keepalive_enabled = _parse_bool(data.get("keepalive_enabled"), default=True)

        cgroup_locked_by_env = (
            _parse_bool(os.environ.get("RPD_DISABLE_CGROUP_RAM_PATCH"), default=False)
            or not _parse_bool(os.environ.get("RPD_ENABLE_CGROUP_RAM_PATCH"), default=False)
        )
        if cgroup_locked_by_env:
            _cgroup_ram_patch_enabled = False
        elif "cgroup_ram_patch_enabled" in data:
            _cgroup_ram_patch_enabled = _parse_bool(
                data.get("cgroup_ram_patch_enabled"),
                default=_cgroup_ram_patch_enabled,
            )

        logging.info(
            "[RunpodDirect] Loaded settings: "
            f"keepalive={_ws_keepalive_enabled}, "
            f"cgroup_ram_patch={_cgroup_ram_patch_enabled}"
        )
    except Exception as e:
        logging.warning(f"[RunpodDirect] Failed to load settings: {e}")


def _normalize_expected_hash(expected_hash):
    if not expected_hash or not isinstance(expected_hash, str):
        return None
    value = expected_hash.strip().lower()
    if value.startswith("sha256:"):
        value = value.split(":", 1)[1]
    if not value:
        return None
    if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def _resolve_hash_type(expected_hash, expected_hash_type):
    hash_type = (expected_hash_type or "").strip().lower()
    if hash_type in ("", None):
        if expected_hash and len(expected_hash) == 64:
            return "sha256"
        return None
    if hash_type in ("sha256", "sha-256"):
        return "sha256"
    return None


def _compute_file_hash(path, hash_type):
    if hash_type != "sha256":
        raise ValueError(f"Unsupported hash type: {hash_type}")
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_READ_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _write_at(path, offset, data):
    """Perform potentially slow Global Volume writes outside the event loop."""
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(data)


def _preallocate_file(path, size):
    with open(path, "wb") as f:
        f.seek(size - 1)
        f.write(b"\0")


def _cleanup_partial_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logging.warning(f"[RunpodDirect] Failed to remove partial file {path}: {e}")


def _sanitize_simple_filename(filename):
    if not filename or not isinstance(filename, str):
        raise ValueError("Missing filename")

    normalized = filename.strip().replace("\\", "/")
    if not normalized or len(normalized) > 1024 or "\x00" in normalized:
        raise ValueError("Invalid model path")
    if normalized.startswith(("/", "~")):
        raise ValueError("Invalid model path: absolute paths are not allowed")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Invalid model path: traversal or empty path component")
    if any(":" in part for part in parts):
        raise ValueError("Invalid model path: colon is not allowed")
    return "/".join(parts)


def _resolve_model_path(directory, safe_filename):
    if directory not in folder_paths.folder_names_and_paths:
        raise ValueError(f"Invalid directory: {directory}")

    output_dir = os.path.abspath(folder_paths.folder_names_and_paths[directory][0][0])
    model_path = os.path.abspath(os.path.join(output_dir, safe_filename))
    if os.path.commonpath([output_dir, model_path]) != output_dir:
        raise ValueError("Security error: attempted directory escape")

    return output_dir, model_path


def _model_exists_in_directory(directory, safe_filename):
    try:
        _, model_path = _resolve_model_path(directory, safe_filename)
    except Exception:
        return False
    return os.path.exists(model_path)


def _find_model_directory_anywhere(safe_filename, preferred_directories=None):
    ordered_directories = []
    seen = set()
    for directory in preferred_directories or []:
        if directory in folder_paths.folder_names_and_paths and directory not in seen:
            ordered_directories.append(directory)
            seen.add(directory)
    for directory in folder_paths.folder_names_and_paths.keys():
        if directory not in seen:
            ordered_directories.append(directory)
            seen.add(directory)

    for directory in ordered_directories:
        if _model_exists_in_directory(directory, safe_filename):
            return directory
    return None


async def _emit_download_progress(download_id, total_size, force=False):
    ctrl = download_control.get(download_id)
    if not ctrl:
        return

    now = time.monotonic()
    total_downloaded = 0
    should_emit = force

    async with ctrl["lock"]:
        total_downloaded = ctrl.get("total_downloaded", 0)
        if not should_emit:
            last_emit = ctrl.get("last_progress_emit", 0.0)
            should_emit = (now - last_emit) >= PROGRESS_EVENT_INTERVAL
        if should_emit:
            ctrl["last_progress_emit"] = now
            progress = 0.0 if total_size <= 0 else min((total_downloaded / total_size) * 100.0, 100.0)
            if download_id in active_downloads:
                active_downloads[download_id]["progress"] = progress
                active_downloads[download_id]["downloaded"] = total_downloaded

    if should_emit:
        progress = 0.0 if total_size <= 0 else min((total_downloaded / total_size) * 100.0, 100.0)
        await PromptServer.instance.send("server_download_progress", {
            "download_id": download_id,
            "progress": progress,
            "downloaded": total_downloaded,
            "total": total_size
        })


def _verify_download_integrity(output_path, total_size, expected_hash=None, expected_hash_type=None):
    if not os.path.exists(output_path):
        raise Exception("Downloaded file is missing on disk")

    actual_size = os.path.getsize(output_path)
    if actual_size != total_size:
        raise Exception(f"Size mismatch: expected {total_size} bytes, got {actual_size} bytes")

    normalized_hash = _normalize_expected_hash(expected_hash)
    if not normalized_hash:
        return {"size_verified": True, "hash_verified": False, "hash_type": None}

    hash_type = _resolve_hash_type(normalized_hash, expected_hash_type)
    if not hash_type:
        raise Exception(f"Unsupported hash_type: {expected_hash_type}. Supported: sha256")

    actual_hash = _compute_file_hash(output_path, hash_type)
    if actual_hash.lower() != normalized_hash:
        raise Exception(f"{hash_type} mismatch for downloaded file")

    return {"size_verified": True, "hash_verified": True, "hash_type": hash_type}


@PromptServer.instance.routes.post("/server_download/start")
async def start_download(request):
    """Start downloading a model file to the server"""
    try:
        json_data = await request.json()
        url = json_data.get("url")
        save_path = json_data.get("save_path")  # e.g., "checkpoints"
        filename = json_data.get("filename")    # e.g., "model.safetensors"
        token = json_data.get("token")          # Optional HF token for gated models
        expected_hash_input = json_data.get("hash")  # Optional integrity hash
        expected_hash_type = json_data.get("hash_type")  # Optional hash type, e.g. sha256

        if not url or not save_path or not filename:
            return web.json_response(
                {"error": "Missing required parameters: url, save_path, filename"},
                status=400
            )

        try:
            _parse_download_url(url)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        expected_hash = _normalize_expected_hash(expected_hash_input)
        if expected_hash_input and not expected_hash:
            return web.json_response(
                {"error": "Invalid hash format. Expected SHA-256 hex digest."},
                status=400
            )

        if expected_hash:
            resolved_hash_type = _resolve_hash_type(expected_hash, expected_hash_type)
            if not resolved_hash_type:
                return web.json_response(
                    {"error": f"Unsupported hash_type: {expected_hash_type}. Supported: sha256"},
                    status=400
                )
            expected_hash_type = resolved_hash_type
        else:
            expected_hash_type = None

        # Validate save_path
        if save_path not in folder_paths.folder_names_and_paths:
            return web.json_response(
                {"error": f"Invalid save_path: {save_path}. Must be one of: {list(folder_paths.folder_names_and_paths.keys())}"},
                status=400
            )

        try:
            safe_filename = _sanitize_simple_filename(filename)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        # Get the first folder path for this model type
        output_dir = folder_paths.folder_names_and_paths[save_path][0][0]
        output_path = os.path.join(output_dir, safe_filename)

        # Final security check: ensure the resolved path is within the intended directory
        output_path = os.path.abspath(output_path)
        output_dir = os.path.abspath(output_dir)
        if os.path.commonpath([output_dir, output_path]) != output_dir:
            return web.json_response(
                {"error": "Security error: attempted directory escape"},
                status=400
            )

        temp_output_path = f"{output_path}.runpoddirect.part"
        download_id = f"{save_path}/{safe_filename}"

        current_status = active_downloads.get(download_id, {}).get("status")
        if current_status in ("queued", "downloading", "paused"):
            return web.json_response(
                {"error": f"Download is already active: {download_id}"},
                status=409,
            )
        if len(download_queue) >= MAX_QUEUE_SIZE:
            return web.json_response(
                {"error": f"Download queue is full (max {MAX_QUEUE_SIZE})"},
                status=429,
            )

        # If a previous session crashed mid-download, remove stale temp file and start clean.
        if os.path.exists(temp_output_path):
            _cleanup_partial_file(temp_output_path)

        # Check if final file already exists.
        # If hash is available and mismatched, treat it as corrupted and redownload.
        if os.path.exists(output_path):
            if expected_hash:
                hash_type = _resolve_hash_type(expected_hash, expected_hash_type)
                actual_hash = await asyncio.to_thread(_compute_file_hash, output_path, hash_type)
                if actual_hash.lower() != expected_hash.lower():
                    logging.warning(
                        f"[RunpodDirect] Existing file hash mismatch for {output_path}; redownloading atomically"
                    )
                else:
                    return web.json_response(
                        {"error": f"File already exists and hash matches: {output_path}"},
                        status=400
                    )
            else:
                return web.json_response(
                    {"error": f"File already exists: {output_path}"},
                    status=400
                )

        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Mark as queued
        active_downloads.pop(download_id, None)
        _prune_download_history(MAX_HISTORY_SIZE - 1)
        active_downloads[download_id] = {
            "url": _display_url(url),
            "filename": safe_filename,
            "save_path": save_path,
            "output_path": output_path,
            "temp_output_path": temp_output_path,
            "progress": 0,
            "status": "queued",
            "priority": None,
            "expected_hash": expected_hash,
            "expected_hash_type": expected_hash_type,
        }

        # Resolve token: prefer explicit token, fall back to HF_TOKEN env var
        resolved_token = token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")

        # Add to queue
        download_queue.append({
            "download_id": download_id,
            "url": url,
            "output_path": output_path,
            "temp_output_path": temp_output_path,
            "token": resolved_token,
            "expected_hash": expected_hash,
            "expected_hash_type": expected_hash_type,
        })

        # Process queue (will start download if slot available)
        asyncio.create_task(process_download_queue())

        return web.json_response({
            "success": True,
            "download_id": download_id,
            "message": "Download queued"
        })

    except Exception as e:
        logging.error(f"Error starting download: {e}")
        return web.json_response(
            {"error": str(e)},
            status=500
        )


async def process_download_queue():
    """Process the download queue - one download at a time"""
    global download_queue, current_download_task

    # Check if already downloading
    if current_download_task is not None and not current_download_task.done():
        logging.info("[RunpodDirect] Download already in progress, waiting...")
        return  # Already downloading

    if len(download_queue) == 0:
        logging.info("[RunpodDirect] Queue is empty")
        return  # Nothing to process

    # Get next download from queue
    download_item = download_queue.pop(0)
    download_id = download_item["download_id"]
    url = download_item["url"]
    output_path = download_item["output_path"]
    temp_output_path = download_item.get("temp_output_path")
    token = download_item.get("token")
    expected_hash = download_item.get("expected_hash")
    expected_hash_type = download_item.get("expected_hash_type")

    # Set status to downloading
    active_downloads[download_id]["status"] = "downloading"
    active_downloads[download_id]["progress"] = 0
    active_downloads[download_id]["downloaded"] = 0

    logging.info(f"[RunpodDirect] Starting download {download_id} with {NUM_CONNECTIONS} connections (full speed)")

    # Notify frontend that download is starting
    await PromptServer.instance.send("server_download_progress", {
        "download_id": download_id,
        "progress": 0,
        "downloaded": 0,
        "total": 0
    })

    # Start download task
    current_download_task = asyncio.create_task(
        download_file(
            url,
            output_path,
            temp_output_path,
            download_id,
            token=token,
            expected_hash=expected_hash,
            expected_hash_type=expected_hash_type
        )
    )

    # Add completion callback to process next in queue
    current_download_task.add_done_callback(lambda t: on_download_complete(download_id))


def on_download_complete(download_id):
    """Advance the queue after a download ends, preserving its actual outcome."""
    global current_download_task

    current_download_task = None
    download = active_downloads.get(download_id, {})
    status = download.get("status", "unknown")
    if status == "completed":
        logging.info(f"[RunpodDirect] Download completed: {download_id}, processing next in queue...")
    elif status == "error":
        logging.error(
            f"[RunpodDirect] Download failed: {download_id}: {download.get('error', 'Unknown error')}, "
            "processing next in queue..."
        )
    else:
        logging.info(f"[RunpodDirect] Download ended ({status}): {download_id}, processing next in queue...")

    # Process next in queue
    asyncio.create_task(process_download_queue())


async def download_chunk(session, url, start, end, output_path, chunk_index, download_id):
    """Download a specific chunk of the file"""
    headers = {'Range': f'bytes={start}-{end}'}

    try:
        async with session.get(url, headers=headers) as response:
            if response.status not in [200, 206]:
                return None

            chunk_data = await response.read()

            # Write chunk to file at specific position
            with open(output_path, 'r+b') as f:
                f.seek(start)
                f.write(chunk_data)

            return len(chunk_data)
    except Exception as e:
        logging.error(f"Error downloading chunk {chunk_index} for {download_id}: {e}")
        return None


class HuggingFaceAccessError(Exception):
    """Authentication/access failures that must not be retried as network errors."""


def _check_hf_access(status_code, url):
    if not _is_huggingface_url(url) or status_code not in (401, 403, 451):
        return
    repo_url = url.split('/resolve/', 1)[0]
    if status_code == 401:
        message = (
            "Authentication required. Provide a valid Hugging Face token using HF_TOKEN "
            "or the HF token input. For gated models, first accept the repository's "
            f"access terms with the same account. Repo: {repo_url}"
        )
    elif status_code == 403:
        message = (
            "Access denied. Check that your Hugging Face token has read access and "
            f"that its account has accepted the model's terms at {repo_url}"
        )
    else:
        message = f"Model access is restricted. Check repository access requirements at {repo_url}"
    raise HuggingFaceAccessError(message)


async def download_file(url, output_path, temp_output_path, download_id, token=None, expected_hash=None, expected_hash_type=None):
    """Download file with multi-connection support and progress tracking"""
    import aiohttp

    logging.info(f"[RunpodDirect] Download {download_id} using {NUM_CONNECTIONS} connections (full speed)")
    working_output_path = temp_output_path or output_path

    try:
        # Initialize control for this download
        download_control[download_id] = {
            "paused": False,
            "cancelled": False,
            "total_downloaded": 0,  # Shared counter for all chunks
            "lock": asyncio.Lock(),   # Lock for thread-safe updates
            "last_progress_emit": 0.0,
        }

        # Build auth headers for gated HF models
        auth_headers = {}
        if token and _is_huggingface_url(url):
            auth_headers['Authorization'] = f'Bearer {token}'
            logging.info(f"[RunpodDirect] Using HF token for {download_id}")
        auth_headers['Accept-Encoding'] = 'identity'

        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=SOCK_CONNECT_TIMEOUT,
            sock_read=SOCK_READ_TIMEOUT,
        )
        connector = aiohttp.TCPConnector(resolver=_PublicOnlyResolver())
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=auth_headers,
            connector=connector,
        ) as session:
            # Get file size - try HEAD first, then fall back to GET with Range
            total_size = 0
            supports_range = False

            try:
                # Try HEAD request first
                async with session.head(url, allow_redirects=True) as response:
                    _check_hf_access(response.status, url)
                    if response.status == 200:
                        total_size = int(response.headers.get('content-length', 0))
                        supports_range = response.headers.get('accept-ranges') == 'bytes'
            except HuggingFaceAccessError:
                raise
            except Exception as e:
                logging.warning(f"HEAD request failed for {download_id}: {e}")

            # If HEAD didn't give us the size, try GET with Range header
            if total_size == 0:
                logging.info(f"HEAD request didn't return size, trying GET with Range for {download_id}")
                try:
                    headers = {'Range': 'bytes=0-0'}
                    async with session.get(url, headers=headers, allow_redirects=True) as response:
                        _check_hf_access(response.status, url)
                        if response.status in [200, 206]:
                            # Try to get size from Content-Range header first
                            content_range = response.headers.get('content-range', '')
                            if content_range:
                                # Format: "bytes 0-0/12345" where 12345 is total size
                                parts = content_range.split('/')
                                if len(parts) == 2:
                                    total_size = int(parts[1])
                                    supports_range = True

                            # Fallback to Content-Length
                            if total_size == 0:
                                total_size = int(response.headers.get('content-length', 0))
                except HuggingFaceAccessError:
                    raise
                except Exception as e:
                    logging.warning(f"GET with Range failed for {download_id}: {e}")

            if total_size == 0:
                raise Exception("Could not determine file size from server")

            if MAX_DOWNLOAD_BYTES > 0 and total_size > MAX_DOWNLOAD_BYTES:
                raise Exception(
                    f"Download is larger than the configured limit of {MAX_DOWNLOAD_BYTES} bytes"
                )

            destination_dir = os.path.dirname(working_output_path)
            free_bytes = shutil.disk_usage(destination_dir).free
            required_bytes = total_size + MIN_FREE_BYTES
            if free_bytes < required_bytes:
                raise Exception(
                    f"Not enough free space: need {required_bytes} bytes including reserve, "
                    f"have {free_bytes} bytes"
                )

            logging.info(f"File size for {download_id}: {total_size} bytes, supports range: {supports_range}")

            # Create file with full size
            await asyncio.to_thread(_preallocate_file, working_output_path, total_size)

            active_downloads[download_id]["total"] = total_size
            active_downloads[download_id]["downloaded"] = 0
            await _emit_download_progress(download_id, total_size, force=True)

            # Use multi-connection download if server supports range requests
            if supports_range and total_size > CHUNK_SIZE:
                logging.info(f"Using {NUM_CONNECTIONS} connections for {download_id}")

                # Calculate chunk ranges
                chunk_size = total_size // NUM_CONNECTIONS
                tasks = []

                for i in range(NUM_CONNECTIONS):
                    start = i * chunk_size
                    end = start + chunk_size - 1 if i < NUM_CONNECTIONS - 1 else total_size - 1

                    tasks.append(download_chunk_with_progress(
                        session, url, start, end, working_output_path, i, download_id, total_size
                    ))

                # Download all chunks in parallel
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Some hosts throttle parallel ranges. Retry the whole file with one
                # connection instead of leaving the user with a failed partial model.
                errors = [result for result in results if isinstance(result, Exception)]
                if errors:
                    if download_control[download_id]["cancelled"]:
                        _cleanup_partial_file(working_output_path)
                        download_control.pop(download_id, None)
                        _prune_download_history()
                        return
                    logging.warning(
                        f"[RunpodDirect] Parallel range download failed for {download_id}; "
                        f"retrying with one connection: {errors[0]}"
                    )
                    async with download_control[download_id]["lock"]:
                        download_control[download_id]["total_downloaded"] = 0
                    await asyncio.to_thread(_preallocate_file, working_output_path, total_size)
                    await asyncio.sleep(2)
                    await download_single_connection(
                        session, url, working_output_path, download_id, total_size
                    )

            else:
                # Fallback to single connection download
                logging.info(f"Using single connection for {download_id}")
                await download_single_connection(session, url, working_output_path, download_id, total_size)

            # Check if cancelled
            if download_control[download_id]["cancelled"]:
                _cleanup_partial_file(working_output_path)
                download_control.pop(download_id, None)
                _prune_download_history()
                return

            async with download_control[download_id]["lock"]:
                total_downloaded = download_control[download_id]["total_downloaded"]
            if total_downloaded != total_size:
                raise Exception(f"Incomplete download: received {total_downloaded} of {total_size} bytes")

            integrity_result = await asyncio.to_thread(
                _verify_download_integrity,
                working_output_path,
                total_size,
                expected_hash,
                expected_hash_type,
            )

            # Atomic finalize: only move into final model path after integrity checks pass.
            os.replace(working_output_path, output_path)

            # Mark as complete
            active_downloads[download_id]["status"] = "completed"
            active_downloads[download_id]["progress"] = 100
            active_downloads[download_id]["downloaded"] = total_size
            active_downloads[download_id]["size_verified"] = integrity_result["size_verified"]
            active_downloads[download_id]["hash_verified"] = integrity_result["hash_verified"]
            active_downloads[download_id]["hash_type"] = integrity_result["hash_type"]
            await _emit_download_progress(download_id, total_size, force=True)

            # Send completion message
            await PromptServer.instance.send("server_download_complete", {
                "download_id": download_id,
                "path": output_path,
                "size": total_size,
                "size_verified": integrity_result["size_verified"],
                "hash_verified": integrity_result["hash_verified"],
                "hash_type": integrity_result["hash_type"],
            })

            logging.info(f"Successfully downloaded {download_id} to {output_path}")

            # Cleanup
            del download_control[download_id]
            _prune_download_history()

    except Exception as e:
        logging.error(f"Error downloading {download_id}: {e}")
        active_downloads[download_id]["status"] = "error"
        active_downloads[download_id]["error"] = str(e)
        _cleanup_partial_file(working_output_path)

        await PromptServer.instance.send("server_download_error", {
            "download_id": download_id,
            "error": str(e)
        })

        # Cleanup
        if download_id in download_control:
            del download_control[download_id]
        _prune_download_history()


async def download_chunk_with_progress(session, url, start, end, output_path, chunk_index, download_id, total_size):
    """Download chunk with progress tracking"""
    chunk_size = end - start + 1
    chunk_downloaded = 0
    retries = 0

    try:
        while chunk_downloaded < chunk_size:
            # Check if paused/cancelled before opening a new request
            while download_control.get(download_id, {}).get("paused", False):
                await asyncio.sleep(0.5)

            if download_control.get(download_id, {}).get("cancelled", False):
                return

            range_start = start + chunk_downloaded
            headers = {'Range': f'bytes={range_start}-{end}'}

            try:
                async with session.get(url, headers=headers) as response:
                    _check_hf_access(response.status, url)
                    if response.status not in [200, 206]:
                        raise Exception(f"HTTP {response.status} for chunk {chunk_index}")
                    _validate_range_response(response, range_start, end, total_size)

                    # If server ignores ranged request in the middle, fail fast to avoid corruption.
                    if range_start > 0 and response.status == 200:
                        raise Exception(f"Server ignored range request for chunk {chunk_index} retry")

                    async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                        # Check if paused
                        while download_control.get(download_id, {}).get("paused", False):
                            await asyncio.sleep(0.5)

                        # Check if cancelled
                        if download_control.get(download_id, {}).get("cancelled", False):
                            return

                        remaining = chunk_size - chunk_downloaded
                        if remaining <= 0:
                            break
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]

                        await asyncio.to_thread(_write_at, output_path, start + chunk_downloaded, chunk)
                        chunk_len = len(chunk)
                        if chunk_len <= 0:
                            continue
                        chunk_downloaded += chunk_len

                        # Update shared progress counter with lock
                        async with download_control[download_id]["lock"]:
                            download_control[download_id]["total_downloaded"] += chunk_len

                        await _emit_download_progress(download_id, total_size)

                # Connection finished. If this chunk isn't complete yet, retry from last offset.
                if chunk_downloaded < chunk_size:
                    retries += 1
                    if retries > CHUNK_MAX_RETRIES:
                        raise Exception(
                            f"Chunk {chunk_index} stalled after {CHUNK_MAX_RETRIES} retries "
                            f"({chunk_downloaded}/{chunk_size} bytes)"
                        )
                    logging.warning(
                        f"[RunpodDirect] Chunk {chunk_index} incomplete, retry {retries}/{CHUNK_MAX_RETRIES} "
                        f"at byte {start + chunk_downloaded}"
                    )
                    await asyncio.sleep(1)
                else:
                    break

            except (asyncio.CancelledError, HuggingFaceAccessError):
                raise
            except Exception as e:
                retries += 1
                if retries > CHUNK_MAX_RETRIES:
                    raise Exception(f"Chunk {chunk_index} failed after {CHUNK_MAX_RETRIES} retries: {e}")
                logging.warning(
                    f"[RunpodDirect] Chunk {chunk_index} error retry {retries}/{CHUNK_MAX_RETRIES}: {e}"
                )
                await asyncio.sleep(1)

        if chunk_downloaded != chunk_size:
            raise Exception(
                f"Chunk {chunk_index} size mismatch: expected {chunk_size}, got {chunk_downloaded}"
            )

    except Exception as e:
        logging.error(f"Error in chunk {chunk_index} for {download_id}: {e}")
        raise


async def download_single_connection(session, url, output_path, download_id, total_size):
    """Fallback single connection download"""
    downloaded_size = 0

    async with session.get(url) as response:
        _check_hf_access(response.status, url)
        if response.status != 200:
            raise Exception(f"HTTP {response.status}")

        with open(output_path, 'wb') as f:
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                # Check if paused
                while download_control.get(download_id, {}).get("paused", False):
                    await asyncio.sleep(0.5)

                # Check if cancelled
                if download_control.get(download_id, {}).get("cancelled", False):
                    return

                await asyncio.to_thread(f.write, chunk)
                chunk_len = len(chunk)
                if chunk_len <= 0:
                    continue
                downloaded_size += chunk_len

                async with download_control[download_id]["lock"]:
                    download_control[download_id]["total_downloaded"] += chunk_len

                await _emit_download_progress(download_id, total_size)

    if downloaded_size != total_size:
        raise Exception(f"Single-connection size mismatch: expected {total_size}, got {downloaded_size}")


@PromptServer.instance.routes.get("/server_download/status")
async def get_download_status(request):
    """Get status of all downloads"""
    return web.json_response(active_downloads)


@PromptServer.instance.routes.get("/server_download/status/{download_id:.*}")
async def get_single_download_status(request):
    """Get status of a specific download"""
    download_id = request.match_info.get("download_id", "")

    if download_id in active_downloads:
        return web.json_response(active_downloads[download_id])
    else:
        return web.json_response(
            {"error": "Download not found"},
            status=404
        )


@PromptServer.instance.routes.post("/server_download/pause")
async def pause_download(request):
    """Pause an active download"""
    try:
        json_data = await request.json()
        download_id = json_data.get("download_id")

        if not download_id:
            return web.json_response(
                {"error": "Missing download_id"},
                status=400
            )

        if download_id not in download_control:
            return web.json_response(
                {"error": "Download not found or already completed"},
                status=404
            )

        download_control[download_id]["paused"] = True
        active_downloads[download_id]["status"] = "paused"

        await PromptServer.instance.send("server_download_paused", {
            "download_id": download_id
        })

        return web.json_response({"success": True, "message": "Download paused"})

    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.post("/server_download/resume")
async def resume_download(request):
    """Resume a paused download"""
    try:
        json_data = await request.json()
        download_id = json_data.get("download_id")

        if not download_id:
            return web.json_response(
                {"error": "Missing download_id"},
                status=400
            )

        if download_id not in download_control:
            return web.json_response(
                {"error": "Download not found or already completed"},
                status=404
            )

        download_control[download_id]["paused"] = False
        active_downloads[download_id]["status"] = "downloading"

        await PromptServer.instance.send("server_download_resumed", {
            "download_id": download_id
        })

        return web.json_response({"success": True, "message": "Download resumed"})

    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.post("/server_download/cancel")
async def cancel_download(request):
    """Cancel an active download"""
    global download_queue, current_download_task

    try:
        json_data = await request.json()
        download_id = json_data.get("download_id")

        if not download_id:
            return web.json_response(
                {"error": "Missing download_id"},
                status=400
            )

        # Check if download is queued (not started yet)
        was_queued = any(d["download_id"] == download_id for d in download_queue)
        download_queue[:] = [d for d in download_queue if d["download_id"] != download_id]

        # Check if download is active
        was_active = download_id in download_control
        if was_active:
            download_control[download_id]["cancelled"] = True

        if not was_queued and not was_active:
            return web.json_response(
                {"error": "Download not found or already completed"},
                status=404,
            )

        # Update status
        if download_id in active_downloads:
            active_downloads[download_id]["status"] = "cancelled"
            _prune_download_history()

        await PromptServer.instance.send("server_download_cancelled", {
            "download_id": download_id
        })

        return web.json_response({"success": True, "message": "Download cancelled"})

    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.get("/server_download/hf_token_status")
async def hf_token_status(request):
    """Check if HF_TOKEN environment variable is set (without exposing the value)"""
    has_token = bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
    return web.json_response({"has_token": has_token})


@PromptServer.instance.routes.get("/server_download/keepalive_status")
async def keepalive_status(request):
    """Get current websocket keepalive setting."""
    return web.json_response({
        "enabled": bool(_ws_keepalive_enabled),
        "interval_seconds": WS_KEEPALIVE_INTERVAL_SECONDS,
        "settings_file": _settings_file_path(),
    })


@PromptServer.instance.routes.post("/server_download/keepalive")
async def set_keepalive_status(request):
    """Enable or disable websocket keepalive loop."""
    global _ws_keepalive_enabled
    try:
        json_data = await request.json()
        previous = bool(_ws_keepalive_enabled)
        enabled = _parse_bool(json_data.get("enabled"), default=True)
        _ws_keepalive_enabled = bool(enabled)
        if not _persist_runtime_settings():
            _ws_keepalive_enabled = previous
            return web.json_response({
                "success": False,
                "error": "Failed to persist settings",
                "enabled": previous,
            }, status=500)
        return web.json_response({
            "success": True,
            "enabled": _ws_keepalive_enabled,
            "interval_seconds": WS_KEEPALIVE_INTERVAL_SECONDS,
            "settings_file": _settings_file_path(),
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.get("/server_download/cgroup_ram_patch_status")
async def cgroup_ram_patch_status(request):
    """Get current cgroup RAM patch setting."""
    limit, usage = _read_cgroup_memory_limit_and_usage()
    return web.json_response({
        "enabled": bool(_cgroup_ram_patch_enabled),
        "applied": bool(_cgroup_ram_patch_applied),
        "cgroup_limit_bytes": limit,
        "cgroup_used_bytes": usage,
        "settings_file": _settings_file_path(),
    })


@PromptServer.instance.routes.post("/server_download/cgroup_ram_patch")
async def set_cgroup_ram_patch_status(request):
    """Enable or disable cgroup-aware RAM override."""
    global _cgroup_ram_patch_enabled
    try:
        if (
            _parse_bool(os.environ.get("RPD_DISABLE_CGROUP_RAM_PATCH"), default=False)
            or not _parse_bool(os.environ.get("RPD_ENABLE_CGROUP_RAM_PATCH"), default=False)
        ):
            return web.json_response({
                "success": False,
                "enabled": False,
                "locked_by_env": True,
                "settings_file": _settings_file_path(),
            }, status=409)

        json_data = await request.json()
        previous = bool(_cgroup_ram_patch_enabled)
        enabled = _parse_bool(json_data.get("enabled"), default=True)
        _cgroup_ram_patch_enabled = bool(enabled)
        if not _persist_runtime_settings():
            _cgroup_ram_patch_enabled = previous
            return web.json_response({
                "success": False,
                "error": "Failed to persist settings",
                "enabled": previous,
                "applied": bool(_cgroup_ram_patch_applied),
                "locked_by_env": False,
            }, status=500)
        return web.json_response({
            "success": True,
            "enabled": _cgroup_ram_patch_enabled,
            "applied": bool(_cgroup_ram_patch_applied),
            "locked_by_env": False,
            "settings_file": _settings_file_path(),
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.get("/server_download/folder_paths")
async def server_download_folder_paths(request):
    """Return canonical ComfyUI folder path keys from backend runtime."""
    try:
        payload = {}
        for key, entries in folder_paths.folder_names_and_paths.items():
            normalized_key = str(key)
            normalized_entries = []
            path_entries = entries[0] if isinstance(entries, (list, tuple)) and entries else []
            if isinstance(path_entries, str):
                path_entries = [path_entries]
            if isinstance(path_entries, (list, tuple)):
                normalized_entries.extend(entry for entry in path_entries if isinstance(entry, str))
            payload[normalized_key] = normalized_entries
        return web.json_response(payload)
    except Exception as e:
        logging.error(f"Error getting folder paths: {e}")
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.post("/server_download/check_missing_models")
async def check_missing_models(request):
    """Batch-check workflow models without blocking queueing on many frontend round-trips."""
    try:
        json_data = await request.json()
        models = json_data.get("models")
        verify_hashes = _parse_bool(json_data.get("verify_hashes"), default=False)

        if not isinstance(models, list):
            return web.json_response(
                {"error": "Missing required parameter: models"},
                status=400
            )

        if len(models) > PREQUEUE_CHECK_MAX_MODELS:
            return web.json_response(
                {"error": f"Too many models requested. Max: {PREQUEUE_CHECK_MAX_MODELS}"},
                status=400
            )

        missing = []
        unresolved = []

        for raw_model in models:
            if not isinstance(raw_model, dict):
                continue

            report_model = dict(raw_model)
            filename = raw_model.get("filename") or raw_model.get("name")
            try:
                safe_filename = _sanitize_simple_filename(filename)
            except ValueError as e:
                report_model["filename"] = str(filename or "")
                report_model["reason"] = "invalid_filename"
                report_model["error"] = str(e)
                unresolved.append(report_model)
                continue

            report_model["filename"] = safe_filename

            directory = raw_model.get("directory")
            directory = str(directory) if isinstance(directory, str) and directory else None
            if directory and directory not in folder_paths.folder_names_and_paths:
                directory = None

            expected_hash = _normalize_expected_hash(raw_model.get("hash"))
            expected_hash_type = raw_model.get("hash_type")
            found_directory = None

            if directory:
                if _model_exists_in_directory(directory, safe_filename):
                    found_directory = directory
                else:
                    found_directory = _find_model_directory_anywhere(
                        safe_filename,
                        preferred_directories=[directory],
                    )
                    if not found_directory:
                        report_model["directory"] = directory
                        report_model["reason"] = "missing"
                        missing.append(report_model)
                        continue
            else:
                found_directory = _find_model_directory_anywhere(safe_filename)
                if not found_directory:
                    report_model["reason"] = "directory_unresolved"
                    unresolved.append(report_model)
                    continue

            if verify_hashes and expected_hash:
                hash_type = _resolve_hash_type(expected_hash, expected_hash_type)
                if not hash_type:
                    report_model["directory"] = found_directory
                    report_model["reason"] = "unsupported_hash_type"
                    unresolved.append(report_model)
                    continue

                _, model_path = _resolve_model_path(found_directory, safe_filename)
                actual_hash = await asyncio.to_thread(_compute_file_hash, model_path, hash_type)
                if actual_hash.lower() != expected_hash.lower():
                    report_model["directory"] = found_directory
                    report_model["corrupted"] = True
                    report_model["reason"] = "hash_mismatch"
                    missing.append(report_model)

        return web.json_response({
            "success": True,
            "missing": missing,
            "unresolved": unresolved,
            "checked": len(models),
            "verify_hashes": verify_hashes,
        })
    except Exception as e:
        logging.error(f"Error checking missing models: {e}")
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.post("/server_download/verify_model_integrity")
async def verify_model_integrity(request):
    """Verify whether a model file exists and, when hash is provided, whether it matches."""
    try:
        json_data = await request.json()
        directory = json_data.get("directory") or json_data.get("save_path")
        filename = json_data.get("filename")
        expected_hash_input = json_data.get("hash")
        expected_hash_type = json_data.get("hash_type")

        if not directory or not filename:
            return web.json_response(
                {"error": "Missing required parameters: directory, filename"},
                status=400
            )

        if directory not in folder_paths.folder_names_and_paths:
            # Keep response successful so frontend can treat unknown folders as
            # non-existent instead of surfacing noisy request errors in console.
            return web.json_response({
                "exists": False,
                "valid": False,
                "reason": "invalid_directory",
            })

        try:
            safe_filename = _sanitize_simple_filename(filename)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        output_dir = os.path.abspath(folder_paths.folder_names_and_paths[directory][0][0])
        model_path = os.path.abspath(os.path.join(output_dir, safe_filename))
        if os.path.commonpath([output_dir, model_path]) != output_dir:
            return web.json_response(
                {"error": "Security error: attempted directory escape"},
                status=400
            )

        if not os.path.exists(model_path):
            return web.json_response({
                "exists": False,
                "valid": False,
                "reason": "missing",
            })

        normalized_hash = _normalize_expected_hash(expected_hash_input)
        if expected_hash_input and not normalized_hash:
            return web.json_response(
                {"error": "Invalid hash format. Expected SHA-256 hex digest."},
                status=400
            )

        # No hash available: we can only confirm presence.
        if not normalized_hash:
            return web.json_response({
                "exists": True,
                "valid": True,
                "hash_verified": False,
                "reason": "no_hash",
            })

        hash_type = _resolve_hash_type(normalized_hash, expected_hash_type)
        if not hash_type:
            return web.json_response(
                {"error": f"Unsupported hash_type: {expected_hash_type}. Supported: sha256"},
                status=400
            )

        actual_hash = await asyncio.to_thread(_compute_file_hash, model_path, hash_type)
        hash_ok = actual_hash.lower() == normalized_hash.lower()
        return web.json_response({
            "exists": True,
            "valid": hash_ok,
            "hash_verified": hash_ok,
            "hash_type": hash_type,
            "reason": "ok" if hash_ok else "hash_mismatch",
        })
    except Exception as e:
        logging.error(f"Error verifying model integrity: {e}")
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.post("/server_download/validate_hf_token")
async def validate_hf_token(request):
    """Validate a Hugging Face token and optionally check access to specific model URLs"""
    import aiohttp

    try:
        json_data = await request.json()
        token = json_data.get("token", "")
        urls = json_data.get("urls", [])  # Optional: list of model URLs to check access

        # If token is the sentinel '__env__', use the environment variable
        if token == "__env__":
            token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""

        if not token:
            return web.json_response({"valid": False, "error": "No token provided"}, status=400)

        # Validate token against HF API
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Check token validity via whoami endpoint
            async with session.get(
                "https://huggingface.co/api/whoami-v2",
                headers={"Authorization": f"Bearer {token}"}
            ) as response:
                if response.status != 200:
                    return web.json_response({"valid": False, "error": "Invalid token"})
                user_data = await response.json()
                username = user_data.get("name", "unknown")

            # If URLs provided, check access to each
            url_access = {}
            for url in urls[:10]:  # Limit to 10 URLs
                if not _is_huggingface_url(url):
                    url_access[url] = {
                        "accessible": False,
                        "reason": "only_huggingface_urls_allowed",
                    }
                    continue
                try:
                    connector = aiohttp.TCPConnector(resolver=_PublicOnlyResolver())
                    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as safe_session:
                        async with safe_session.head(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        allow_redirects=True
                        ) as resp:
                            if resp.status == 200:
                                url_access[url] = {"accessible": True}
                            elif resp.status in (401, 403, 451):
                                repo_url = url[:url.find('/resolve/')] if '/resolve/' in url else url
                                url_access[url] = {
                                    "accessible": False,
                                    "reason": "terms_not_accepted",
                                    "repo_url": repo_url
                                }
                            else:
                                url_access[url] = {"accessible": False, "reason": f"HTTP {resp.status}"}
                except Exception:
                    url_access[url] = {"accessible": False, "reason": "request_failed"}

            return web.json_response({
                "valid": True,
                "username": username,
                "url_access": url_access
            })

    except Exception as e:
        logging.error(f"Error validating HF token: {e}")
        return web.json_response({"valid": False, "error": str(e)}, status=500)


@PromptServer.instance.routes.get("/extensions/ComfyUI-RunpodDirect/serverDownload.js")
async def serve_js_with_version(request):
    """Serve JS file with cache-busting headers"""
    js_path = os.path.join(os.path.dirname(__file__), "web", "serverDownload.js")

    response = web.FileResponse(js_path)
    # Add cache control headers to force revalidation
    response.headers['Cache-Control'] = 'no-cache, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    response.headers['X-Version'] = __version__

    return response


# Set the web directory for frontend files
WEB_DIRECTORY = "./web"

# Version for cache busting - increment this when you update the JS
__version__ = "1.0.12"

# Apply cgroup-aware RAM patch, then load persisted settings, then start keepalive.
_patch_comfy_ram_detection_for_cgroups()
_load_persisted_settings()
_ensure_ws_keepalive_task()

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
