"""
Finds the KiCad project the user is working on.

KiCad 10's API only reports the open schematic's *file name* (no folder), so the
answer is combined from several sources, best first:
  1. the IPC API of the KiCad instance that launched the plugin
  2. the command lines of running KiCad processes
  3. KiCad's own list of open / recent projects (kicad.json)
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("quick_part_import")

KICAD_SUFFIXES = (".kicad_pro", ".kicad_sch", ".kicad_pcb")


def _add_fallback_site_packages() -> None:
    """If this plugin's own venv lacks kipy/psutil, borrow them from another KiCad plugin venv."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    for env_root in sorted(Path(base, "KiCad").glob("*/python-environments/*"), reverse=True):
        site = env_root / "Lib" / "site-packages"
        cfg = env_root / "pyvenv.cfg"
        try:
            same_python = f"version = {version}" in cfg.read_text(encoding="utf-8")
        except OSError:
            continue
        if same_python and (site / "kipy").is_dir() and str(site) not in sys.path:
            sys.path.append(str(site))
            log.info("Using packages from %s", site)
            return


def project_for(path: Path) -> Path | None:
    """Return the .kicad_pro belonging to a .kicad_pro/.kicad_sch/.kicad_pcb file or folder."""
    path = Path(path)
    if path.suffix.lower() == ".kicad_pro":
        return path if path.is_file() else None
    folder = path if path.is_dir() else path.parent
    same_name = folder / f"{path.stem}.kicad_pro"
    if same_name.is_file():
        return same_name
    return next(iter(sorted(folder.glob("*.kicad_pro"))), None)


# -- 1. IPC API ----------------------------------------------------------------


def _ipc_documents(api_socket: str | None, api_token: str | None) -> tuple[list[Path], list[str]]:
    """Return (project files reported by KiCad, bare file names of open documents)."""
    try:
        from kipy import KiCad
        from kipy.proto.common.types import DocumentType
    except Exception:
        _add_fallback_site_packages()
        try:
            from kipy import KiCad
            from kipy.proto.common.types import DocumentType
        except Exception as e:
            log.info("kipy not available: %s", e)
            return [], []

    projects: list[Path] = []
    names: list[str] = []
    try:
        kicad = KiCad(socket_path=api_socket, kicad_token=api_token, timeout_ms=800)
    except Exception as e:
        log.info("KiCad API not reachable: %s", e)
        return [], []
    for doc_type in (DocumentType.DOCTYPE_PCB, DocumentType.DOCTYPE_SCHEMATIC):
        try:
            docs = kicad.get_open_documents(doc_type)
        except Exception as e:
            log.debug("get_open_documents(%s): %s", doc_type, e)
            continue
        for doc in docs:
            if doc.project.path:
                folder = Path(doc.project.path)
                pro = folder / f"{doc.project.name}.kicad_pro" if doc.project.name else folder
                found = project_for(pro)
                if found:
                    projects.append(found)
            file_name = getattr(doc, "board_filename", "") or ""
            if file_name:
                names.append(file_name)
    log.debug("IPC: projects=%s files=%s", projects, names)
    return projects, names


# -- 2. running processes -----------------------------------------------------


def _process_command_lines() -> list[list[str]]:
    try:
        import psutil
    except ImportError:
        _add_fallback_site_packages()
        try:
            import psutil
        except ImportError:
            psutil = None  # type: ignore[assignment]

    result: list[list[str]] = []
    if psutil is not None:
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (proc.info["name"] or "").lower()
                if name.startswith(("kicad", "eeschema", "pcbnew")) and "cli" not in name:
                    result.append(list(proc.info["cmdline"] or []))
            except Exception:
                continue
        return result

    if sys.platform == "win32":  # no psutil: ask Windows directly
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='kicad.exe' OR Name='eeschema.exe' "
            "OR Name='pcbnew.exe'\" | ForEach-Object { $_.CommandLine }"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=0x08000000,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        for line in out.splitlines():
            args = [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', line)]
            if args:
                result.append(args)
    return result


def _process_projects() -> list[Path]:
    projects: list[Path] = []
    for args in _process_command_lines():
        for arg in args[1:]:
            path = Path(arg.strip('"'))
            if path.suffix.lower() in KICAD_SUFFIXES and path.exists():
                pro = project_for(path)
                if pro and pro not in projects:
                    projects.append(pro)
    return projects


# -- 3. kicad.json --------------------------------------------------------------


def kicad_config_dir() -> Path | None:
    if os.environ.get("KICAD_CONFIG_HOME"):
        base = Path(os.environ["KICAD_CONFIG_HOME"])
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "kicad"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Preferences" / "kicad"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kicad"
    # prefer the version of the KiCad we run under (its Python lives in .../KiCad/10.0/bin)
    running = next((p for p in Path(sys.base_prefix).parts if re.fullmatch(r"\d+\.\d+", p)), None)
    if running and (base / running).is_dir():
        return base / running
    versions = [d for d in base.glob("*") if d.is_dir() and re.fullmatch(r"\d+\.\d+", d.name)]
    if not versions:
        return None
    return max(versions, key=lambda d: tuple(int(x) for x in d.name.split(".")))


def _recent_projects() -> list[Path]:
    folder = kicad_config_dir()
    if not folder:
        return []
    try:
        data = json.loads((folder / "kicad.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    system = data.get("system") or {}
    projects: list[Path] = []
    for entry in (system.get("open_projects") or []) + (system.get("file_history") or []):
        pro = project_for(Path(entry)) if isinstance(entry, str) else None
        if pro and pro not in projects:
            projects.append(pro)
    return projects


# -- combined ------------------------------------------------------------------


def detect_project(api_socket: str | None = None, api_token: str | None = None) -> Path | None:
    """Best guess for the project the user works on, or None."""
    ipc_projects, open_files = _ipc_documents(api_socket, api_token)
    if ipc_projects:
        return ipc_projects[0]

    running = _process_projects()
    candidates = running + [p for p in _recent_projects() if p not in running]
    for file_name in open_files:
        for pro in candidates:
            if (pro.parent / file_name).is_file():
                return pro
        stem = Path(file_name).stem.lower()
        for pro in candidates:
            if pro.stem.lower() == stem:
                return pro
    if running:
        return running[0]
    return None
