"""Focused safety and compatibility tests for the bundled RunpodDirect node."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).parent
MODULE_PATH = ROOT / "vendor" / "ComfyUI-RunpodDirect" / "__init__.py"


class _Routes:
    def get(self, _path):
        return lambda fn: fn

    def post(self, _path):
        return lambda fn: fn


class _PromptServer:
    instance = types.SimpleNamespace(routes=_Routes(), loop=None, sockets={})


def _load_module():
    os.environ["RPD_DISABLE_CGROUP_RAM_PATCH"] = "1"
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.folder_names_and_paths = {
        "checkpoints": (["/workspace/ComfyUI/models/checkpoints"], {".safetensors"}),
        "configs": (["/workspace/ComfyUI/models/configs"], [".yaml"]),
        "diffusers": (["/workspace/ComfyUI/models/diffusers"], ["folder"]),
    }
    comfy = types.ModuleType("comfy")
    model_management = types.ModuleType("comfy.model_management")
    model_management.psutil = types.SimpleNamespace(virtual_memory=lambda: None)
    comfy.model_management = model_management
    server = types.ModuleType("server")
    server.PromptServer = _PromptServer
    aiohttp = types.ModuleType("aiohttp")
    aiohttp_abc = types.ModuleType("aiohttp.abc")

    class AbstractResolver:
        pass

    class Response:
        def __init__(self, payload):
            self.text = json.dumps(payload)

    aiohttp_abc.AbstractResolver = AbstractResolver
    aiohttp.web = types.SimpleNamespace(
        json_response=lambda payload, status=200: Response(payload),
        FileResponse=lambda path: types.SimpleNamespace(path=path, headers={}),
    )
    sys.modules.update({
        "folder_paths": folder_paths,
        "comfy": comfy,
        "comfy.model_management": model_management,
        "server": server,
        "aiohttp": aiohttp,
        "aiohttp.abc": aiohttp_abc,
    })
    spec = importlib.util.spec_from_file_location("runpoddirect_audit", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunpodDirectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_url_validation(self):
        self.assertEqual(
            self.module._parse_download_url("https://huggingface.co/a/b").hostname,
            "huggingface.co",
        )
        for bad in (
            "http://example.com/model.safetensors",
            "file:///etc/passwd",
            "https://user:secret@example.com/model.safetensors",
            "not-a-url",
        ):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                self.module._parse_download_url(bad)

    def test_nested_model_paths_are_preserved_safely(self):
        self.assertEqual(
            self.module._sanitize_simple_filename("project-a\\loras/model.safetensors"),
            "project-a/loras/model.safetensors",
        )
        output_dir, model_path = self.module._resolve_model_path(
            "checkpoints", "project-a/model.safetensors"
        )
        self.assertEqual(
            os.path.commonpath([output_dir, model_path]),
            output_dir,
        )
        for bad in ("../model.safetensors", "/tmp/model.safetensors", "a//b.safetensors"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                self.module._sanitize_simple_filename(bad)

    def test_huggingface_token_host_check_is_exact(self):
        self.assertTrue(self.module._is_huggingface_url("https://huggingface.co/a"))
        self.assertTrue(self.module._is_huggingface_url("https://cdn.huggingface.co/a"))
        self.assertFalse(self.module._is_huggingface_url("https://huggingface.co.evil.test/a"))
        self.assertFalse(self.module._is_huggingface_url("https://evil-huggingface.co/a"))

    def test_cgroup_monkey_patch_is_disabled_by_default(self):
        self.assertFalse(self.module._cgroup_ram_patch_enabled)
        self.assertFalse(self.module._cgroup_ram_patch_applied)

    def test_download_history_is_bounded(self):
        self.module.active_downloads.clear()
        for index in range(600):
            self.module.active_downloads[str(index)] = {"status": "completed"}
        self.module._prune_download_history(512)
        self.assertEqual(len(self.module.active_downloads), 512)
        self.assertNotIn("0", self.module.active_downloads)
        self.assertIn("599", self.module.active_downloads)
        self.module.active_downloads.clear()

    def test_signed_url_is_redacted_from_status(self):
        self.assertEqual(
            self.module._display_url("https://example.com/model.safetensors?token=secret#part"),
            "https://example.com/model.safetensors",
        )

    def test_range_response_must_match_requested_bytes(self):
        valid = types.SimpleNamespace(
            status=206,
            headers={"content-range": "bytes 100-199/1000"},
        )
        self.module._validate_range_response(valid, 100, 199, 1000)
        invalid = types.SimpleNamespace(
            status=206,
            headers={"content-range": "bytes 0-99/1000"},
        )
        with self.assertRaises(Exception):
            self.module._validate_range_response(invalid, 100, 199, 1000)

    def test_private_address_resolver_rejects_loopback(self):
        resolver = self.module._PublicOnlyResolver()
        with self.assertRaises(OSError):
            asyncio.run(resolver.resolve("127.0.0.1", 443))
        result = asyncio.run(resolver.resolve("1.1.1.1", 443))
        self.assertEqual(result[0]["host"], "1.1.1.1")

    def test_folder_endpoint_returns_paths_only(self):
        response = asyncio.run(self.module.server_download_folder_paths(None))
        payload = json.loads(response.text)
        self.assertEqual(payload["configs"], ["/workspace/ComfyUI/models/configs"])
        self.assertEqual(payload["diffusers"], ["/workspace/ComfyUI/models/diffusers"])
        self.assertNotIn(".yaml", payload["configs"])
        self.assertNotIn("folder", payload["diffusers"])

    def test_settings_path_can_live_on_global_volume(self):
        old = os.environ.get("RPD_SETTINGS_PATH")
        try:
            os.environ["RPD_SETTINGS_PATH"] = "/workspace/.comfy-image/runpoddirect.json"
            self.assertEqual(
                self.module._settings_file_path(),
                "/workspace/.comfy-image/runpoddirect.json",
            )
        finally:
            if old is None:
                os.environ.pop("RPD_SETTINGS_PATH", None)
            else:
                os.environ["RPD_SETTINGS_PATH"] = old


if __name__ == "__main__":
    unittest.main()
