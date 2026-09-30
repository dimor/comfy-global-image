"""Tests for the model cache that do not require ComfyUI or CUDA."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('cache_launcher', ROOT / 'lazy-cache-main.py')
cache_launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache_launcher)


class LazyCacheTest(unittest.TestCase):
    def test_only_requested_model_is_copied_and_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'models'
            source.mkdir()
            requested = source / 'requested.safetensors'
            requested.write_bytes(b'requested-model')
            unused = source / 'unused.safetensors'
            unused.write_bytes(b'unused-model')
            cache = root / 'cache'
            cache.mkdir()
            status = root / 'status.json'

            class FakePaths:
                @staticmethod
                def get_full_path(_folder, filename):
                    path = source / filename
                    return str(path) if path.is_file() else None

            with patch.object(cache_launcher, 'SOURCE_ROOTS', (source,)), \
                    patch.object(cache_launcher, 'CACHE', cache), \
                    patch.object(cache_launcher, 'STATUS', status), \
                    patch.object(cache_launcher, 'MIN_BYTES', 0), \
                    patch.object(cache_launcher, 'RESERVE_BYTES', 0):
                cache_launcher.status_data.clear()
                cache_launcher.install(FakePaths)
                resolved = FakePaths.get_full_path('checkpoints', requested.name)

            self.assertEqual(Path(resolved).read_bytes(), requested.read_bytes())
            self.assertFalse((cache / 'checkpoints' / unused.name).exists())
            result = json.loads(status.read_text())
            self.assertEqual(result['state'], 'ready')
            model = result['models']['checkpoints/requested.safetensors']
            self.assertEqual(model['state'], 'ready')
            self.assertEqual(model['percent'], 100.0)
            self.assertEqual(model['source'], str(requested))
            self.assertEqual(model['local_copy'], str(cache / 'checkpoints' / requested.name))


if __name__ == '__main__':
    unittest.main(verbosity=2)
