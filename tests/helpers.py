"""Load a private copy of repoforge.py whose config, logs and apps folder all live in a temp folder."""
import importlib.util
import json
import shutil
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_repoforge(config=None):
    """-> (module, temp folder). config=None means no config.json (first run); a dict is written as config.json,
    a str is written as it is (e.g. a damaged file). The caller removes the temp folder with cleanup()."""
    tmp = Path(tempfile.mkdtemp(prefix="rf-test-"))
    shutil.copy2(REPO / "repoforge.py", tmp / "repoforge.py")
    if config is not None:
        text = config if isinstance(config, str) else json.dumps(config)
        (tmp / "config.json").write_text(text, encoding="utf-8")
    name = f"repoforge_under_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, tmp / "repoforge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, tmp


def use_temp_apps(rf, tmp: Path):
    """Point the apps folder, bin folder and registry of a loaded module at tmp/apps."""
    rf.APPS = tmp / "apps"
    rf.BIN = rf.APPS / "bin"
    rf.REGISTRY = rf.APPS / "repoforge-registry.json"


def cleanup(tmp: Path):
    shutil.rmtree(tmp, ignore_errors=True)
