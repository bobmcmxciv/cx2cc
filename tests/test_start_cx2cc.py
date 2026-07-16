import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_start_module():
    spec = importlib.util.spec_from_file_location("start_cx2cc", ROOT / "start-cx2cc.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_serve_command_uses_frozen_executable(monkeypatch):
    module = load_start_module()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/tmp/cx2cc")

    assert module._serve_command() == ["/tmp/cx2cc", "serve"]


def test_serve_command_uses_python_for_source(monkeypatch):
    module = load_start_module()
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    command = module._serve_command()

    assert command[0] == "/usr/bin/python3"
    assert Path(command[1]).name == "start-cx2cc.py"
    assert command[2] == "serve"
