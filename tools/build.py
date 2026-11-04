"""
Builds the KiCad Plugin and Content Manager (PCM) package and repository files.

    python tools/build.py

Writes into pcm/:
    kicad-quick-part-import-<version>.zip   "Install from File..." package
    packages.json, resources.zip            package index + icon for the repository
    repository.json                         the URL you add in PCM "Manage..."

Bump "version" in metadata.json before building a new release.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PCM = ROOT / "pcm"
RAW_URL = "https://raw.githubusercontent.com/Jiggolino/kicad-quick-part-import/main/pcm"
SKIP = {"__pycache__", "settings.json"}


def deterministic_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """Same input -> byte-identical zip (fixed timestamps and order)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buffer.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate(document: dict, definition: str | None = None) -> None:
    """Check against KiCad's own PCM schema when jsonschema and KiCad are available."""
    try:
        import jsonschema
    except ImportError:
        print("  (jsonschema not installed - schema check skipped)")
        return
    candidates = sorted(Path("C:/Program Files/KiCad").glob("*/share/kicad/schemas/pcm.v1.schema.json"))
    candidates += [Path("/usr/share/kicad/schemas/pcm.v1.schema.json")]
    schema_file = next((p for p in candidates if p.is_file()), None)
    if not schema_file:
        print("  (KiCad PCM schema not found - schema check skipped)")
        return
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    if definition:
        schema = {**schema, "$ref": f"#/definitions/{definition}"}
    jsonschema.validate(document, schema)
    print(f"  valid against {schema_file.name}" + (f" ({definition})" if definition else ""))


def main() -> int:
    metadata = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    plugin = json.loads((ROOT / "plugins" / "plugin.json").read_text(encoding="utf-8"))
    identifier = metadata["identifier"]
    if plugin["identifier"] != identifier:
        print(f"plugin.json identifier {plugin['identifier']} != metadata.json {identifier}")
        return 1
    version = metadata["versions"][0]["version"]
    code_version = re.search(r'^VERSION = "([^"]+)"', (ROOT / "plugins" / "jlc_parts.py").read_text(encoding="utf-8"), re.M)
    if not code_version or code_version.group(1) != version:
        print(f"plugins/jlc_parts.py VERSION must be {version}")
        return 1
    print(f"Building {identifier} {version}")
    validate(metadata)

    entries = [("metadata.json", (ROOT / "metadata.json").read_bytes())]
    for path in sorted((ROOT / "plugins").rglob("*")):
        if path.is_file() and not SKIP.intersection(path.parts) and path.suffix not in (".log", ".pyc"):
            entries.append((f"plugins/{path.relative_to(ROOT / 'plugins').as_posix()}", path.read_bytes()))
    entries.append(("resources/icon.png", (ROOT / "resources" / "icon.png").read_bytes()))
    package = deterministic_zip(entries)
    install_size = sum(len(data) for _, data in entries)

    PCM.mkdir(exist_ok=True)
    package_name = f"kicad-quick-part-import-{version}.zip"
    (PCM / package_name).write_bytes(package)
    print(f"  {package_name}: {len(package)} bytes, {len(entries)} files")

    # packages.json keeps every released version, newest first
    packages_file = PCM / "packages.json"
    old_versions = []
    if packages_file.exists():
        old = json.loads(packages_file.read_text(encoding="utf-8"))
        old_versions = [v for p in old.get("packages", []) for v in p.get("versions", []) if v["version"] != version]
    release = dict(metadata["versions"][0])
    release.update(
        download_url=f"{RAW_URL}/{package_name}",
        download_sha256=sha256(package),
        download_size=len(package),
        install_size=install_size,
    )
    entry = {k: v for k, v in metadata.items() if k not in ("$schema", "versions")}
    entry["versions"] = [release] + old_versions
    packages = {"packages": [entry]}
    validate(packages, "PackageArray")
    packages_bytes = (json.dumps(packages, indent=2) + "\n").encode()
    packages_file.write_bytes(packages_bytes)

    resources = deterministic_zip([(f"{identifier}/icon.png", (ROOT / "resources" / "icon.png").read_bytes())])
    (PCM / "resources.zip").write_bytes(resources)

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    stamp = {"update_timestamp": int(now.timestamp()), "update_time_utc": now.strftime("%Y-%m-%d %H:%M:%S")}
    repository = {
        "$schema": "https://go.kicad.org/pcm/schemas/v1#/definitions/Repository",
        "name": "Quick Part Import by Jiggolino",
        "maintainer": metadata["maintainer"],
        "packages": {"url": f"{RAW_URL}/packages.json", "sha256": sha256(packages_bytes), **stamp},
        "resources": {"url": f"{RAW_URL}/resources.zip", "sha256": sha256(resources), **stamp},
    }
    validate(repository, "Repository")
    (PCM / "repository.json").write_text(json.dumps(repository, indent=2) + "\n", encoding="utf-8")
    print("  packages.json, resources.zip, repository.json updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
