"""Project reconnaissance: what is this project, and how would its tests be run. Read-only, uses the Workspace path policy.

Two audiences, deliberately separated:
  * the model gets `overview(...)` (facts + *suggested* commands, for orientation and for telling the user what to enable);
  * the host gets `suggest_test_commands(...)`: a ready-to-review `{name: argv}` dict. Nothing here is ever executed, and
    the model cannot make a suggestion runnable: only commands the host passes as `test_commands` can run.
"""
import json
import sys
from collections import Counter
from pathlib import PurePosixPath

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

SCAN_LIMIT = 5000
MARKERS = {
    "python": ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini", "pytest.ini", "Pipfile"],
    "node": ["package.json"], "dotnet": [".sln", ".csproj", ".fsproj"], "java-maven": ["pom.xml"], "java-gradle": ["build.gradle", "build.gradle.kts", "gradlew"],
    "go": ["go.mod"], "rust": ["Cargo.toml"], "ruby": ["Gemfile"], "php": ["composer.json"], "make": ["Makefile"], "cmake": ["CMakeLists.txt"],
}
TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "__tests__", "e2e"}
CONFIG_NAMES = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "package.json", "tsconfig.json", ".eslintrc", ".eslintrc.json", "pom.xml", "build.gradle",
                "Makefile", "Dockerfile", "docker-compose.yml", ".editorconfig", "settings.json", "launch.json", ".gitignore", "README.md"}

def _venv_python(workspace):
    """A project-local interpreter, looked up directly because walking skips environment folders."""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe", "venv/bin/python"):
        try:
            if workspace.resolve(rel).is_file():
                return rel
        except ValueError:
            continue
    return None

def _read_json(workspace, rel):
    try:
        data = json.loads(workspace.resolve(rel).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}

def _read_toml(workspace, rel):
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(workspace.resolve(rel).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}

def _python(workspace, names, evidence):
    py = _venv_python(workspace) or "python"
    uses_pytest = any(n in names for n in ("pytest.ini", "tox.ini")) or "pytest" in json.dumps(_read_toml(workspace, "pyproject.toml")).lower()
    if not uses_pytest and "requirements.txt" in names:
        try:
            uses_pytest = "pytest" in workspace.resolve("requirements.txt").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            pass
    return {"unit": [py, "-m", "pytest", "-q"] if uses_pytest else [py, "-m", "unittest", "discover"]}

def _node(workspace, names, evidence):
    scripts = _read_json(workspace, "package.json").get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    manager = "pnpm" if "pnpm-lock.yaml" in names else "yarn" if "yarn.lock" in names else "npm"
    exe = manager + (".cmd" if sys.platform == "win32" else "")
    out = {}
    for key in ("test", "lint", "typecheck", "build"):
        if isinstance(scripts.get(key), str):
            out[key if key != "test" else "unit"] = [exe, "test"] if key == "test" else [exe, "run", key]
    return out

def _simple(argv):
    return lambda workspace, names, evidence: {"unit": argv}

_SUGGESTERS = {
    "python": _python, "node": _node, "dotnet": _simple(["dotnet", "test"]), "java-maven": _simple(["mvn", "-q", "test"]),
    "java-gradle": lambda w, n, e: {"unit": ["gradlew.bat" if sys.platform == "win32" else "./gradlew", "test"] if "gradlew" in n else ["gradle", "test"]},
    "go": _simple(["go", "test", "./..."]), "rust": _simple(["cargo", "test"]), "ruby": _simple(["bundle", "exec", "rake", "test"]),
    "php": lambda w, n, e: {"unit": ["composer", "test"]} if "test" in (_read_json(w, "composer.json").get("scripts") or {}) else {},
    "make": lambda w, n, e: {"unit": ["make", "test"]} if _makefile_has_test(w) else {},
}

def _makefile_has_test(workspace):
    try:
        return any(line.startswith("test:") for line in workspace.resolve("Makefile").read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return False

def overview(workspace, skip_dirs=()):
    """Facts about the project plus per-ecosystem suggested commands. Bounded by SCAN_LIMIT files."""
    root = workspace.root
    files = []
    for f in workspace.iter_files(".", skip_dirs, SCAN_LIMIT):
        files.append(f.relative_to(root).as_posix())
    truncated = bool(getattr(workspace, "scan_truncated", False))
    top = {PurePosixPath(f).name for f in files if "/" not in f}
    names = set(files)
    suffixes = Counter(PurePosixPath(f).suffix.lower() for f in files if PurePosixPath(f).suffix)
    ecosystems = []
    for eco, markers in MARKERS.items():
        evidence = sorted(f for f in files if "/" not in f and any(f == m or f.endswith(m) for m in markers))[:5]
        if evidence:
            suggested = _SUGGESTERS.get(eco, lambda *_: {})(workspace, top | names, evidence)
            ecosystems.append({"name": eco, "evidence": evidence, "suggested_commands": suggested})
    test_dirs = sorted({str(PurePosixPath(f).parent) for f in files if any(part in TEST_DIR_NAMES for part in PurePosixPath(f).parts[:-1])})[:20]
    test_dirs = sorted({d.split("/")[0] if d.split("/")[0] in TEST_DIR_NAMES else d for d in test_dirs})
    configs = sorted(f for f in files if PurePosixPath(f).name in CONFIG_NAMES and f.count("/") <= 2)[:30]
    return {"files_scanned": len(files), "scan_truncated": truncated,
            "languages": dict(suffixes.most_common(12)), "ecosystems": ecosystems, "test_dirs": test_dirs, "config_files": configs,
            "note": "suggested_commands are NOT runnable by the model; only commands the host configured as test_commands can run"}

def suggest_test_commands(workspace, skip_dirs=()):
    """Host helper: merge the first suggestion of each ecosystem into {name: argv} for the host to REVIEW and pass explicitly."""
    merged = {}
    for eco in overview(workspace, skip_dirs)["ecosystems"]:
        for name, argv in eco["suggested_commands"].items():
            key = name if name not in merged else f"{eco['name']}-{name}"
            merged.setdefault(key, argv)
    return merged
