"""Adapt ComfyUI Manager file copies to Runpod Global Volume semantics."""
from importlib.util import find_spec
from pathlib import Path
import sys


TARGETS = (
    'glob/manager_core.py',
    'glob/manager_server.py',
    'legacy/manager_core.py',
    'legacy/manager_server.py',
)


def patch_manager(root: Path) -> None:
    for relative in TARGETS:
        target = root / relative
        source = target.read_text(encoding='utf-8')
        count = source.count('shutil.copy(')
        if count != 1:
            raise RuntimeError(f'{relative}: expected one shutil.copy call, found {count}')
        target.write_text(source.replace('shutil.copy(', 'shutil.copyfile('), encoding='utf-8')
        print(f'Patched {relative}: shutil.copy -> shutil.copyfile')


def package_root() -> Path:
    spec = find_spec('comfyui_manager')
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError('comfyui_manager package is not installed')
    return Path(next(iter(spec.submodule_search_locations)))


if __name__ == '__main__':
    patch_manager(Path(sys.argv[1]) if len(sys.argv) > 1 else package_root())
