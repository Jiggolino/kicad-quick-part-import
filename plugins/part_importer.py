"""
Core of Quick Part Import (no GUI code in here).

Takes the ZIP files you download from Component Search Engine (SamacSys), SnapEDA,
UltraLibrarian or Octopart and merges their KiCad files into ONE library that lives
inside the project folder:

    <project>/lib/<Project>.kicad_sym     all symbols
    <project>/lib/<Project>.pretty/       all footprints
    <project>/lib/<Project>.3dshapes/     all 3D models

The library is registered in the project's sym-lib-table / fp-lib-table with
${KIPRJMOD} paths, symbols are linked to their footprints and footprints to their
3D models. Every file is written atomically and rolled back if anything fails, so a
broken download can never damage an existing library.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator

log = logging.getLogger("quick_part_import")

LIB_DIR = "lib"
TABLE_DESCR = "Parts imported with Quick Part Import"
MODEL_EXTS = (".step", ".stp", ".stpz", ".wrl", ".wrz")  # preference order
WANTED_EXTS = (".kicad_sym", ".lib", ".dcm", ".kicad_mod", ".mod", ".zip") + MODEL_EXTS
MAX_FILE_SIZE = 200 * 1024 * 1024
MAX_NESTED_ZIP_DEPTH = 2


class PartImportError(Exception):
    """An import problem with a message that can be shown to the user as-is."""


# ----------------------------------------------------------------------------
# S-expression helpers (text based, so nothing KiCad wrote is ever lost)
# ----------------------------------------------------------------------------

_ATOM = r'(?:"(?:[^"\\]|\\.)*"|[^\s()"]+)'
_HEAD = re.compile(rf'\(\s*([^\s()"]+)(?:\s+({_ATOM}))?')
_SPECIAL = re.compile(r'[()"\\]')


def _parens(text: str) -> Iterator[tuple[int, int]]:
    """Yield (index, depth) for every parenthesis outside of string literals.

    An opening parenthesis reports its own depth (top level = 1), a closing one the
    depth after closing (top level = 0).
    """
    depth = 0
    in_string = False
    escaped_pos = -1
    for match in _SPECIAL.finditer(text):
        i = match.start()
        char = text[i]
        if in_string:
            if i == escaped_pos:
                continue
            if char == "\\":
                escaped_pos = i + 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
            yield i, depth
        elif char == ")":
            depth -= 1
            yield i, depth


def _children(text: str) -> tuple[int, int, list[tuple[int, int]]]:
    """Return (root_open, root_close, [(start, end) of each direct child list]).

    Raises ValueError unless text is exactly one closed S-expression.
    """
    root_open = root_close = -1
    child_start = -1
    kids: list[tuple[int, int]] = []
    for i, depth in _parens(text):
        if text[i] == "(":
            if depth == 1:
                if root_open != -1:
                    raise ValueError("more than one top-level expression")
                root_open = i
            elif depth == 2:
                child_start = i
        elif depth == 1:
            kids.append((child_start, i + 1))
        elif depth == 0:
            root_close = i
        elif depth < 0:
            raise ValueError("unbalanced parentheses")
    if root_open == -1 or root_close == -1 or text[root_close + 1 :].strip():
        raise ValueError("not a single closed S-expression")
    if text[:root_open].strip():
        raise ValueError("unexpected data before the S-expression")
    return root_open, root_close, kids


def _head(text: str, pos: int = 0) -> tuple[str, str | None, tuple[int, int] | None]:
    """Return (keyword, first atom unquoted, span of that atom) of the list at pos."""
    match = _HEAD.match(text, pos)
    if not match:
        return "", None, None
    atom = match.group(2)
    if atom is None:
        return match.group(1), None, None
    return match.group(1), _unquote(atom), match.span(2)


def _unquote(atom: str) -> str:
    if len(atom) >= 2 and atom.startswith('"') and atom.endswith('"'):
        return re.sub(r"\\(.)", r"\1", atom[1:-1])
    return atom


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _items(text: str) -> dict[str, str]:
    """Map keyword -> first atom for the direct children of a list like (lib (name x)(uri y))."""
    _, _, kids = _children(text)
    result: dict[str, str] = {}
    for start, end in kids:
        keyword, atom, _ = _head(text[start:end])
        if keyword and atom is not None and keyword not in result:
            result[keyword] = atom
    return result


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------
# Symbol libraries
# ----------------------------------------------------------------------------


@dataclass
class SymbolLib:
    header: list[str]  # non-symbol children such as (version ...) (generator ...)
    symbols: list[tuple[str, str]]  # (name, full "(symbol ...)" text)

    @classmethod
    def parse(cls, text: str) -> SymbolLib:
        text = text.lstrip("\ufeff")
        root_open, _, kids = _children(text)
        if _head(text, root_open)[0] != "kicad_symbol_lib":
            raise ValueError("not a KiCad symbol library")
        header: list[str] = []
        symbols: list[tuple[str, str]] = []
        for start, end in kids:
            block = text[start:end]
            keyword, name, _ = _head(block)
            if keyword == "symbol" and name:
                symbols.append((name, block))
            else:
                header.append(block)
        return cls(header, symbols)

    def render(self) -> str:
        lines = ["(kicad_symbol_lib"]
        lines += ["\t" + block for block in self.header]
        lines += ["\t" + block for _, block in self.symbols]
        return "\n".join(lines) + "\n)\n"


def get_symbol_property(block: str, name: str) -> str:
    _, _, kids = _children(block)
    for start, end in kids:
        child = block[start:end]
        keyword, key, _ = _head(child)
        if keyword == "property" and key == name:
            value = re.match(rf'\(\s*property\s+{_ATOM}\s+({_ATOM})', child)
            return _unquote(value.group(1)) if value else ""
    return ""


def set_symbol_property(block: str, name: str, value: str) -> str:
    """Set a symbol field's value, adding it as a hidden field if the symbol lacks it."""
    _, _, kids = _children(block)
    last_property_end = -1
    for start, end in kids:
        child = block[start:end]
        keyword, key, _ = _head(child)
        if keyword != "property":
            continue
        last_property_end = end
        if key == name:
            match = re.match(rf'(\(\s*property\s+{_ATOM}\s+)({_ATOM})', child)
            if match:
                a, b = match.span(2)
                return block[: start + a] + _quote(value) + block[start + b :]
    new_property = (
        f"(property {_quote(name)} {_quote(value)} (at 0 0 0)"
        " (effects (font (size 1.27 1.27)) (hide yes)))"
    )
    insert_at = last_property_end if last_property_end != -1 else _children(block)[1]
    return block[:insert_at] + "\n\t\t" + new_property + block[insert_at:]


def get_symbol_footprint(block: str) -> str:
    return get_symbol_property(block, "Footprint")


def set_symbol_footprint(block: str, footprint: str) -> str:
    """Point the symbol's Footprint field at footprint ("Lib:Name"), adding it if needed."""
    return set_symbol_property(block, "Footprint", footprint)


# ----------------------------------------------------------------------------
# Footprints
# ----------------------------------------------------------------------------


def footprint_name(text: str) -> str | None:
    root_open, _, _ = _children(text.lstrip("\ufeff"))
    keyword, name, _ = _head(text.lstrip("\ufeff"), root_open)
    if keyword not in ("footprint", "module"):
        return None
    return name.strip() if name and name.strip() else None


def set_footprint_name(text: str, name: str) -> str:
    text = text.lstrip("\ufeff")
    root_open, _, _ = _children(text)
    _, _, span = _head(text, root_open)
    if span is None:
        return text
    return text[: span[0]] + _quote(name) + text[span[1] :]


def _model_block(path: str) -> str:
    return (
        f"\t(model {_quote(path)}\n\t\t(offset\n\t\t\t(xyz 0 0 0)\n\t\t)\n"
        "\t\t(scale\n\t\t\t(xyz 1 1 1)\n\t\t)\n\t\t(rotate\n\t\t\t(xyz 0 0 0)\n\t\t)\n\t)"
    )


def rewrite_footprint_models(
    text: str, resolve: Callable[[str | None], str | None]
) -> tuple[str, list[str]]:
    """Point every (model ...) at resolve(old_path); add one if there is none.

    resolve(None) is asked for a model when the footprint has no model reference.
    References that end up pointing at the same file are merged into one, keeping the
    block (with its offset/rotation) whose original file type matches the new file.
    Returns (text, used paths).
    """
    _, root_close, kids = _children(text)
    blocks: list[tuple[int, int, tuple[int, int], str, str | None]] = []
    for start, end in kids:
        keyword, old_path, span = _head(text[start:end])
        if keyword == "model" and span is not None and old_path is not None:
            blocks.append((start, end, span, old_path, resolve(old_path)))

    if not blocks:
        new_path = resolve(None)
        if not new_path:
            return text, []
        text = text[:root_close].rstrip() + "\n" + _model_block(new_path) + "\n" + text[root_close:]
        return text, [new_path]

    keep: dict[str, tuple[int, bool]] = {}
    for i, (_, _, _, old_path, new_path) in enumerate(blocks):
        if new_path is None:
            continue  # leave references we cannot improve untouched
        old_ext = PurePosixPath(old_path.replace("\\", "/")).suffix.lower()
        same_type = old_ext == PurePosixPath(new_path).suffix.lower()
        if new_path not in keep or (same_type and not keep[new_path][1]):
            keep[new_path] = (i, same_type)
    keep_index = {i for i, _ in keep.values()}

    pieces: list[str] = []
    last = 0
    for i, (start, end, span, _, new_path) in enumerate(blocks):
        if new_path is None:
            continue
        if i in keep_index:
            block = text[start:end]
            pieces.append(text[last:start])
            pieces.append(block[: span[0]] + _quote(new_path) + block[span[1] :])
            last = end
        else:  # drop the duplicate including its line
            pieces.append(text[last:start].rstrip(" \t"))
            last = end
            while last < len(text) and text[last] in " \t":
                last += 1
            if text.startswith("\r\n", last):
                last += 2
            elif text.startswith("\n", last):
                last += 1
    pieces.append(text[last:])
    return "".join(pieces), list(keep)


# ----------------------------------------------------------------------------
# kicad-cli
# ----------------------------------------------------------------------------


def find_kicad_cli(hint: str | None = None) -> str | None:
    exe = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint))
    if os.environ.get("KICAD_CLI"):
        candidates.append(Path(os.environ["KICAD_CLI"]))
    # KiCad's bundled Python lives next to kicad-cli (also for plugin venvs)
    for base in {sys.base_prefix, sys.prefix, os.path.dirname(sys.executable)}:
        candidates += [Path(base) / exe, Path(base) / "bin" / exe]
    on_path = shutil.which("kicad-cli")
    if on_path:
        candidates.append(Path(on_path))
    if sys.platform == "win32":
        for root in {os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files"}:
            found = sorted(
                Path(root).glob(r"KiCad\*\bin\kicad-cli.exe"),
                key=lambda p: [int(x) if x.isdigit() else 0 for x in p.parent.parent.name.split(".")],
                reverse=True,
            )
            candidates += found
    elif sys.platform == "darwin":
        candidates += sorted(Path("/Applications").glob("KiCad*/KiCad.app/Contents/MacOS/kicad-cli"), reverse=True)
    else:
        candidates += [Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli")]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


class KicadCli:
    def __init__(self, exe: str | None) -> None:
        self.exe = exe

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def _run(self, *args: str) -> str:
        if not self.exe:
            raise PartImportError("kicad-cli was not found (it ships with KiCad 8 or newer).")
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [self.exe, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            raise PartImportError("kicad-cli did not finish in time.") from e
        except OSError as e:
            raise PartImportError(f"kicad-cli could not be started: {e}") from e
        output = (proc.stdout + proc.stderr).strip()
        log.debug("kicad-cli %s -> %s %s", args, proc.returncode, output)
        if proc.returncode != 0:
            raise PartImportError(f"kicad-cli failed: {output or proc.returncode}")
        return output

    def sym_upgrade(self, src: Path, dst: Path) -> None:
        self._run("sym", "upgrade", str(src), "-o", str(dst), "--force")
        if not dst.is_file() or not dst.read_text(encoding="utf-8", errors="replace").lstrip(
            "\ufeff \t\r\n"
        ).startswith("(kicad_symbol_lib"):
            raise PartImportError(f"kicad-cli could not read the symbol file {src.name}.")

    def fp_upgrade(self, src: Path, dst: Path | None = None) -> None:
        args = ["fp", "upgrade", str(src)]
        if dst is not None:
            args += ["-o", str(dst)]
        self._run(*args, "--force")


# ----------------------------------------------------------------------------
# Reading the dropped files
# ----------------------------------------------------------------------------


@dataclass
class SourceFile:
    path: str  # path inside the archive / relative to the dropped folder
    data: bytes

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def stem(self) -> str:
        return PurePosixPath(self.path).stem

    @property
    def ext(self) -> str:
        return PurePosixPath(self.path).suffix.lower()

    @property
    def folder(self) -> str:
        return str(PurePosixPath(self.path).parent)

    def kicad_rank(self) -> int:
        """0 if the file sits in a KiCad specific folder (CSE/UltraLibrarian), else 1."""
        return 0 if any("kicad" in part.lower() for part in PurePosixPath(self.path).parts[:-1]) else 1


def _junk(name: str) -> bool:
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return any(p == "__MACOSX" or p.startswith("._") for p in parts)


def _read_zip(data_or_path: bytes | Path, prefix: str, depth: int) -> list[SourceFile]:
    source =io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path
    files: list[SourceFile] = []
    try:
        with zipfile.ZipFile(source) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if info.is_dir() or _junk(name):
                    continue
                ext = PurePosixPath(name).suffix.lower()
                if ext not in WANTED_EXTS or info.file_size > MAX_FILE_SIZE:
                    continue
                if ext == ".zip":
                    if depth < MAX_NESTED_ZIP_DEPTH:
                        files += _read_zip(zf.read(info), f"{prefix}{name}/", depth + 1)
                    continue
                files.append(SourceFile(prefix + name, zf.read(info)))
    except zipfile.BadZipFile as e:
        raise PartImportError("This is not a valid ZIP file (maybe the download is incomplete).") from e
    except (OSError, RuntimeError, NotImplementedError) as e:
        raise PartImportError(f"The ZIP file could not be read: {e}") from e
    return files


def read_sources(paths: Iterable[Path]) -> list[SourceFile]:
    files: list[SourceFile] = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            for item in sorted(path.rglob("*")):
                if item.is_file() and not _junk(item.name):
                    rel = item.relative_to(path).as_posix()
                    if item.suffix.lower() == ".zip":
                        files += _read_zip(item, rel + "/", 1)
                    elif item.suffix.lower() in WANTED_EXTS and item.stat().st_size <= MAX_FILE_SIZE:
                        files.append(SourceFile(rel, item.read_bytes()))
        elif path.suffix.lower() == ".zip" or zipfile.is_zipfile(path):
            files += _read_zip(path, "", 0)
        elif path.suffix.lower() in WANTED_EXTS:
            files.append(SourceFile(path.name, path.read_bytes()))
    return files


def _starts_with(data: bytes, *markers: str) -> bool:
    head = data[:200].lstrip(b"\xef\xbb\xbf \t\r\n").decode("latin-1")
    return any(head.startswith(m) for m in markers)


@dataclass
class _Plan:
    symbols: list[SourceFile]
    dcms: list[SourceFile]
    footprints: list[SourceFile]
    legacy_footprints: list[SourceFile]
    models: list[SourceFile]


def _plan(files: list[SourceFile]) -> _Plan:
    def order(f: SourceFile) -> tuple[int, str]:
        return f.kicad_rank(), f.path.lower()

    kicad_sym = [f for f in files if f.ext == ".kicad_sym" and _starts_with(f.data, "(kicad_symbol_lib")]
    legacy_sym = [f for f in files if f.ext == ".lib" and _starts_with(f.data, "EESchema-LIBRARY")]
    kicad_mod = [f for f in files if f.ext == ".kicad_mod" and _starts_with(f.data, "(footprint", "(module")]
    legacy_mod = [f for f in files if f.ext == ".mod" and _starts_with(f.data, "PCBNEW-LibModule")]
    models = [f for f in files if f.ext in MODEL_EXTS and len(f.data) > 0]
    return _Plan(
        symbols=sorted(kicad_sym, key=order) or sorted(legacy_sym, key=order),
        dcms=[f for f in files if f.ext == ".dcm"],
        footprints=sorted(kicad_mod, key=order),
        legacy_footprints=[] if kicad_mod else sorted(legacy_mod, key=order),
        models=sorted(models, key=lambda f: (MODEL_EXTS.index(f.ext), f.kicad_rank(), f.path.lower())),
    )


def looks_like_part_zip(path: Path | io.BytesIO, _depth: int = 0) -> bool:
    """Cheap check used by the download watcher: does the ZIP contain KiCad files?"""
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if not _junk(n)]
            lower = [n.lower() for n in names]
            if any(n.endswith((".kicad_sym", ".kicad_mod")) for n in lower) or any(
                n.endswith((".lib", ".mod")) and "kicad" in n for n in lower
            ):
                return True
            if _depth == 0:
                return any(
                    looks_like_part_zip(io.BytesIO(zf.read(n)), 1)
                    for n, low in zip(names, lower)
                    if low.endswith(".zip") and zf.getinfo(n).file_size < MAX_FILE_SIZE
                )
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        return False
    return False


# ----------------------------------------------------------------------------
# Names, library tables
# ----------------------------------------------------------------------------

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}


def clean_file_name(name: str) -> str:
    """Make a footprint/model name safe as a file name (KiCad uses the file name as the ID)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ") or "_"
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


def clean_lib_name(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_\-.+]', "_", name).strip("._") or "Project"
    return name


def _norm_uri(uri: str) -> str:
    uri = uri.replace("\\", "/").rstrip("/")
    return uri.lower() if sys.platform == "win32" else uri


@dataclass
class _TableChange:
    nickname: str
    new_text: str | None  # None = table already correct


def _plan_table(table: Path, kind: str, wanted: str, uri: str) -> _TableChange:
    """Find our library in a lib table or plan to add it (never touches other entries)."""
    root = "sym_lib_table" if kind == "sym" else "fp_lib_table"
    entry_for = lambda nick: (  # noqa: E731
        f"(lib (name {_quote(nick)})(type \"KiCad\")(uri {_quote(uri)})"
        f"(options \"\")(descr {_quote(TABLE_DESCR)}))"
    )
    if not table.exists():
        text = f"({root}\n  (version 7)\n  {entry_for(wanted)}\n)\n"
        return _TableChange(wanted, text)

    text = _decode(table.read_bytes())
    try:
        root_open, root_close, kids = _children(text)
    except ValueError as e:
        raise PartImportError(f"The project's {table.name} is damaged ({e}); please check it in KiCad.") from e
    if _head(text, root_open)[0] != root:
        raise PartImportError(f"The project's {table.name} is not a KiCad library table.")

    taken: set[str] = set()
    for start, end in kids:
        if _head(text[start:end])[0] != "lib":
            continue
        try:
            items = _items(text[start:end])
        except ValueError:
            continue
        taken.add(items.get("name", ""))
        if _norm_uri(items.get("uri", "")) == _norm_uri(uri):
            return _TableChange(items.get("name", wanted), None)

    nickname = wanted
    counter = 2
    while nickname in taken:
        nickname = f"{wanted}_{counter}"
        counter += 1
    new_text = text[:root_close].rstrip() + "\n  " + entry_for(nickname) + "\n" + text[root_close:]
    return _TableChange(nickname, new_text)


# ----------------------------------------------------------------------------
# Transactional writing
# ----------------------------------------------------------------------------


class _Commit:
    """Writes files atomically; rollback() restores everything written so far."""

    def __init__(self) -> None:
        self.done: list[tuple[Path, Path | None]] = []
        self.created_dirs: list[Path] = []

    def mkdir(self, folder: Path) -> None:
        missing = []
        probe = folder
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        folder.mkdir(parents=True, exist_ok=True)
        self.created_dirs += reversed(missing)

    def write(self, target: Path, data: bytes) -> None:
        self.mkdir(target.parent)
        tmp = target.with_name(f".{target.name}.qpi-tmp")
        tmp.write_bytes(data)
        backup = None
        try:
            if target.exists():
                backup = target.with_name(f".{target.name}.qpi-bak")
                shutil.copy2(target, backup)
            os.replace(tmp, target)
        except PermissionError as e:
            tmp.unlink(missing_ok=True)
            if backup:
                backup.unlink(missing_ok=True)
            raise PartImportError(f"{target.name} is locked by another program: {e}") from e
        except BaseException:
            tmp.unlink(missing_ok=True)
            if backup:
                backup.unlink(missing_ok=True)
            raise
        self.done.append((target, backup))

    def rollback(self) -> None:
        for target, backup in reversed(self.done):
            try:
                if backup:
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as e:
                log.error("Rollback of %s failed: %s", target, e)
        for folder in reversed(self.created_dirs):
            try:
                folder.rmdir()
            except OSError:
                pass

    def finish(self) -> None:
        for _, backup in self.done:
            if backup:
                backup.unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# The importer
# ----------------------------------------------------------------------------


@dataclass
class ImportResult:
    source: str
    part: str = ""
    library: str = ""
    symbols: list[str] = field(default_factory=list)
    footprints: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (symbol, "Lib:Footprint")
    library_added: bool = False

    def summary(self) -> str:
        parts = []
        if self.symbols:
            parts.append("symbol" if len(self.symbols) == 1 else f"{len(self.symbols)} symbols")
        if self.footprints:
            parts.append("footprint" if len(self.footprints) == 1 else f"{len(self.footprints)} footprints")
        if self.models:
            parts.append("3D model" if len(self.models) == 1 else f"{len(self.models)} 3D models")
        text = " + ".join(parts) if parts else "nothing"
        if self.updated:
            text += " (updated existing)"
        return text


class ProjectLibrary:
    """The one library of a KiCad project: <project>/lib/<Name>.*"""

    def __init__(self, project_file: Path, cli: KicadCli) -> None:
        self.project_file = Path(project_file)
        self.project_dir = self.project_file.parent
        self.name = clean_lib_name(self.project_file.stem)
        self.cli = cli
        self.lib_dir = self.project_dir / LIB_DIR
        self.sym_file = self.lib_dir / f"{self.name}.kicad_sym"
        self.pretty_dir = self.lib_dir / f"{self.name}.pretty"
        self.shapes_dir = self.lib_dir / f"{self.name}.3dshapes"
        prefix = "${KIPRJMOD}/" + LIB_DIR
        self.sym_uri = f"{prefix}/{self.name}.kicad_sym"
        self.fp_uri = f"{prefix}/{self.name}.pretty"
        self.shapes_uri = f"{prefix}/{self.name}.3dshapes"
        self._header: list[str] | None = None

    # -- public ---------------------------------------------------------------

    def import_paths(self, paths: list[Path], label: str | None = None) -> ImportResult:
        """Import dropped ZIPs, folders or loose KiCad files."""
        paths = [Path(p) for p in paths]
        if not self.project_dir.is_dir():
            raise PartImportError(f"The project folder does not exist: {self.project_dir}")
        fallback = re.sub(r"^(LIB_|ul_)", "", paths[0].stem) if paths else "part"
        return self.import_files(read_sources(paths), label or ", ".join(p.name for p in paths), fallback)

    def import_files(
        self,
        files: list[SourceFile],
        label: str,
        fallback_name: str = "part",
        fields: dict[str, str] | None = None,
    ) -> ImportResult:
        """Import already-read files; fields are set on every imported symbol (empty = skip)."""
        result = ImportResult(source=label)
        if not self.project_dir.is_dir():
            raise PartImportError(f"The project folder does not exist: {self.project_dir}")
        plan = _plan(files)
        if not (plan.symbols or plan.footprints or plan.legacy_footprints or plan.models):
            raise PartImportError(
                "No KiCad symbol, footprint or 3D model found. "
                "Make sure the download contains the KiCad format."
            )

        with tempfile.TemporaryDirectory(prefix="quick_part_import_") as tmp_name:
            tmp = Path(tmp_name)
            sym_change = _plan_table(self.project_dir / "sym-lib-table", "sym", self.name, self.sym_uri)
            fp_change = _plan_table(self.project_dir / "fp-lib-table", "fp", self.name, self.fp_uri)
            result.library = sym_change.nickname

            footprints = self._load_footprints(plan, tmp, result)
            models_used = self._link_models(footprints, plan.models, result)
            symbols = self._load_symbols(plan, tmp, result)
            for key, value in (fields or {}).items():
                if value:
                    symbols = [(name, set_symbol_property(block, key, value)) for name, block in symbols]
            result.links = self._link_footprints(symbols, footprints, fp_change.nickname)
            merged = self._merge_symbols(symbols, tmp, result) if symbols else None

            if not (symbols or footprints or models_used):
                raise PartImportError("Nothing usable could be extracted from this file.")

            commit = _Commit()
            try:
                for model in models_used:
                    target = self.shapes_dir / model[0]
                    if target.exists():
                        result.updated.append(target.name)
                    commit.write(target, model[1])
                    result.models.append(target.name)
                for fp_name, fp_text in footprints:
                    target = self.pretty_dir / f"{fp_name}.kicad_mod"
                    if target.exists():
                        result.updated.append(target.name)
                    commit.write(target, fp_text.encode("utf-8"))
                    result.footprints.append(fp_name)
                if merged is not None:
                    commit.write(self.sym_file, merged.encode("utf-8"))
                    result.symbols = [name for name, _ in symbols]
                # (re-)register whenever the library exists, so a lost table entry heals itself
                if self.sym_file.exists() and sym_change.new_text is not None:
                    commit.write(self.project_dir / "sym-lib-table", sym_change.new_text.encode("utf-8"))
                    result.library_added = True
                if self.pretty_dir.is_dir() and fp_change.new_text is not None:
                    commit.write(self.project_dir / "fp-lib-table", fp_change.new_text.encode("utf-8"))
                    result.library_added = True
            except BaseException:
                commit.rollback()
                raise
            commit.finish()

        result.part = (
            (result.symbols[0] if result.symbols else "")
            or (result.footprints[0] if result.footprints else "")
            or fallback_name
        )
        log.info("Imported %s into %s: %s", result.source, self.sym_file, result.summary())
        return result

    # -- footprints -----------------------------------------------------------

    def _load_footprints(self, plan: _Plan, tmp: Path, result: ImportResult) -> list[tuple[str, str]]:
        pretty = tmp / "in.pretty"
        pretty.mkdir()
        names: list[str] = []

        if plan.footprints:
            for src in plan.footprints:
                text = _decode(src.data)
                try:
                    name = footprint_name(text) or src.stem
                except ValueError as e:
                    result.warnings.append(f"Skipped damaged footprint {src.name} ({e})")
                    continue
                name = clean_file_name(name)
                if name.lower() in (n.lower() for n in names):
                    continue  # same footprint shipped twice (e.g. two KiCad versions)
                names.append(name)
                (pretty / f"{name}.kicad_mod").write_text(set_footprint_name(text, name), encoding="utf-8")
            if names and self.cli.available:
                try:
                    self.cli.fp_upgrade(pretty)
                except PartImportError as e:
                    log.warning("Footprint upgrade failed, using files as downloaded: %s", e)
        elif plan.legacy_footprints and not self.cli.available:
            result.warnings.append("Old .mod footprint skipped: kicad-cli is needed to convert it.")
        elif plan.legacy_footprints:
            for i, src in enumerate(plan.legacy_footprints):
                legacy = tmp / f"legacy{i}" / f"{clean_file_name(src.stem)}.mod"
                legacy.parent.mkdir()
                legacy.write_bytes(src.data)
                converted = tmp / f"legacy{i}_out.pretty"  # kicad-cli wants a new folder
                try:
                    self.cli.fp_upgrade(legacy, converted)
                except PartImportError as e:
                    result.warnings.append(f"Could not convert {src.name}: {e}")
                    continue
                for fp_file in sorted(converted.glob("*.kicad_mod")):
                    if not (pretty / fp_file.name).exists():
                        shutil.move(str(fp_file), str(pretty / fp_file.name))
            names = [p.stem for p in sorted(pretty.glob("*.kicad_mod"))]

        footprints: list[tuple[str, str]] = []
        for name in names:
            path = pretty / f"{name}.kicad_mod"
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                footprints.append((name, text))
        return footprints

    def _link_models(
        self, footprints: list[tuple[str, str]], models: list[SourceFile], result: ImportResult
    ) -> list[tuple[str, bytes]]:
        """Rewrite the footprints' model paths in place; return the model files to copy."""
        by_stem: dict[str, SourceFile] = {}
        for model in models:
            by_stem.setdefault(model.stem.lower(), model)
        chosen: dict[str, bytes] = {}

        def use(model: SourceFile) -> str:
            file_name = clean_file_name(model.name)
            chosen[file_name] = model.data
            return f"{self.shapes_uri}/{file_name}"

        def resolve(old: str | None) -> str | None:
            if not models:
                return None
            if old:
                stem = PurePosixPath(old.replace("\\", "/")).stem.lower()
                if stem.endswith((".step", ".stp", ".wrl")):  # e.g. name.step.gz
                    stem = stem.rsplit(".", 1)[0]
                if stem in by_stem:
                    return use(by_stem[stem])
            return use(models[0])

        for i, (name, text) in enumerate(footprints):
            try:
                new_text, _ = rewrite_footprint_models(text, resolve)
            except ValueError as e:
                result.warnings.append(f"3D model of {name} not linked ({e})")
                continue
            footprints[i] = (name, new_text)

        if models and not footprints:
            use(models[0])  # model-only download: still store it
        return list(chosen.items())

    # -- symbols --------------------------------------------------------------

    def _load_symbols(self, plan: _Plan, tmp: Path, result: ImportResult) -> list[tuple[str, str]]:
        symbols: list[tuple[str, str]] = []
        seen: set[str] = set()
        for i, src in enumerate(plan.symbols):
            folder = tmp / f"sym{i}"
            folder.mkdir()
            stem = clean_file_name(src.stem)
            source = folder / f"{stem}{src.ext}"
            source.write_bytes(src.data)
            if src.ext == ".lib":
                dcm = next((d for d in plan.dcms if d.folder == src.folder and d.stem == src.stem), None)
                if dcm:
                    (folder / f"{stem}.dcm").write_bytes(dcm.data)
            try:
                if self.cli.available:
                    upgraded = folder / "upgraded.kicad_sym"
                    self.cli.sym_upgrade(source, upgraded)
                    text = upgraded.read_text(encoding="utf-8")
                elif src.ext == ".kicad_sym":
                    text = _decode(src.data)
                else:
                    raise PartImportError("kicad-cli is needed to convert old .lib symbols.")
                lib = SymbolLib.parse(text)
            except (PartImportError, ValueError) as e:
                result.warnings.append(f"Skipped symbol file {src.name}: {e}")
                continue
            if self._header is None:
                self._header = lib.header
            for name, block in lib.symbols:
                if name not in seen:
                    seen.add(name)
                    symbols.append((name, block))
        if plan.symbols and not symbols:
            raise PartImportError("The symbol could not be read: " + "; ".join(result.warnings))
        return symbols

    def _link_footprints(
        self, symbols: list[tuple[str, str]], footprints: list[tuple[str, str]], nickname: str
    ) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        names = [name for name, _ in footprints]
        lower = {name.lower(): name for name in names}
        for i, (sym_name, block) in enumerate(symbols):
            try:
                current = get_symbol_footprint(block)
            except ValueError:
                continue
            wanted = clean_file_name(current.split(":", 1)[-1]) if current else ""
            target = lower.get(wanted.lower())
            if target is None and names:
                target = names[0]  # the footprint that came with this download
            if target is None and wanted and (self.pretty_dir / f"{wanted}.kicad_mod").exists():
                target = wanted  # footprint from an earlier import of this project
            if target:
                symbols[i] = (sym_name, set_symbol_footprint(block, f"{nickname}:{target}"))
                links.append((sym_name, f"{nickname}:{target}"))
        return links

    def _merge_symbols(self, symbols: list[tuple[str, str]], tmp: Path, result: ImportResult) -> str:
        if self.sym_file.exists():
            try:
                if self.cli.available:
                    current = tmp / "current.kicad_sym"
                    self.cli.sym_upgrade(self.sym_file, current)
                    text = current.read_text(encoding="utf-8")
                else:
                    text = _decode(self.sym_file.read_bytes())
                lib = SymbolLib.parse(text)
            except (PartImportError, ValueError) as e:
                raise PartImportError(
                    f"The existing library {self.sym_file.name} could not be read ({e}). "
                    "Nothing was changed."
                ) from e
        else:
            lib = SymbolLib(header=list(self._header or ["(version 20211014)", '(generator "quick_part_import")']), symbols=[])

        index = {name: i for i, (name, _) in enumerate(lib.symbols)}
        for name, block in symbols:
            if name in index:
                lib.symbols[index[name]] = (name, block)  # replace in place (keeps parents first)
                result.updated.append(name)
            else:
                index[name] = len(lib.symbols)
                lib.symbols.append((name, block))

        merged = lib.render()
        if self.cli.available:  # let KiCad itself validate and format the result
            draft = tmp / "merged_draft.kicad_sym"
            final = tmp / "merged.kicad_sym"
            draft.write_text(merged, encoding="utf-8")
            try:
                self.cli.sym_upgrade(draft, final)
            except PartImportError as e:
                raise PartImportError(f"KiCad rejected the merged library ({e}). Nothing was changed.") from e
            merged = final.read_text(encoding="utf-8")
        check = {name for name, _ in SymbolLib.parse(merged).symbols}
        missing = [name for name, _ in symbols if name not in check]
        if missing:
            raise PartImportError(f"Symbols missing after merge: {', '.join(missing)}. Nothing was changed.")
        return merged


def import_into_project(
    project_file: Path, paths: list[Path], cli_path: str | None = None, label: str | None = None
) -> ImportResult:
    return ProjectLibrary(project_file, KicadCli(cli_path or find_kicad_cli())).import_paths(paths, label)
