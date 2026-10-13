"""Loads the pure modules of the integration without importing Home Assistant.

custom_components/solar_optimizer/__init__.py imports Home Assistant, so the pure modules are
loaded from their files into a synthetic package (their relative imports resolve inside it)."""

import importlib.util
import pathlib
import sys
import types

_DIR = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "solar_optimizer"
_PKG = "so_core"


def _load():
    if _PKG in sys.modules:
        return sys.modules[_PKG]
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    for name in ("smoothing", "optimizer_core", "supervisor_core", "controller_core"):
        spec = importlib.util.spec_from_file_location(f"{_PKG}.{name}", _DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        setattr(pkg, name, module)
    return pkg


core = _load()
optimizer_core = core.optimizer_core
supervisor_core = core.supervisor_core
controller_core = core.controller_core
