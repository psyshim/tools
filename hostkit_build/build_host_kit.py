"""Build the pinned, installable host kit: a pure-Python wheel containing `host_client` and `protocol` and nothing else.

    python -m hostkit_build.build_host_kit --out dist/host_kit

The wheel is reproducible (sorted entries, fixed timestamps) and carries a manifest with the exact source revision and the
sha256 of every file, so a host maintainer can verify what they installed. It has no dependencies: installing it brings in
no agent, tool, file, Git, process or model code. Remove it with `pip uninstall code-host-kit`; ordinary chat is unaffected.
"""
import argparse
import base64
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME, DIST = "code-host-kit", "code_host_kit"
FIXED_TIME = (2026, 10, 24, 0, 0, 0)
SOURCES = {"host_client": ROOT / "packages" / "host_client" / "host_client", "protocol": ROOT / "protocol"}

def version():
    text = (SOURCES["host_client"] / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)

def git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

def digest(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()

def collect():
    files = {}
    for package, directory in SOURCES.items():
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" not in path.parts:
                files[f"{package}/{path.relative_to(directory).as_posix()}"] = path.read_bytes()
    return files

def build(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ver, files = version(), collect()
    info = f"{DIST}-{ver}.dist-info"
    manifest = {"name": NAME, "version": ver, "source_revision": git("rev-parse", "HEAD"), "tracked_files_modified": bool(git("status", "--porcelain", "--untracked-files=no")),
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    metadata = (f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {ver}\nSummary: Powerless host client and wire contract for the coding service\n"
                f"Requires-Python: >=3.9\n").encode()
    wheel = b"Wheel-Version: 1.0\nGenerator: hostkit_build.build_host_kit\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    extra = {f"{info}/METADATA": metadata, f"{info}/WHEEL": wheel, f"{info}/top_level.txt": b"host_client\nprotocol\n",
             f"{info}/KIT_MANIFEST.json": json.dumps(manifest, indent=1, sort_keys=True).encode()}
    entries = {**files, **extra}
    record = "".join(f"{name},sha256={digest(data)},{len(data)}\n" for name, data in sorted(entries.items())) + f"{info}/RECORD,,\n"
    entries[f"{info}/RECORD"] = record.encode()
    wheel_path = out / f"{DIST}-{ver}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            item = zipfile.ZipInfo(name, FIXED_TIME)
            item.compress_type, item.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            archive.writestr(item, data)
    sums = f"{hashlib.sha256(wheel_path.read_bytes()).hexdigest()}  {wheel_path.name}\n"
    (out / "SHA256SUMS").write_text(sums, encoding="utf-8")
    (out / "KIT_MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return wheel_path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "dist" / "host_kit"))
    args = parser.parse_args()
    path = build(args.out)
    print(f"built {path} ({path.stat().st_size} bytes)\n{(path.parent / 'SHA256SUMS').read_text().strip()}")

if __name__ == "__main__":
    main()
