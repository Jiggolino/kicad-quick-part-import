"""
Part search for Quick Part Import.

JLCPCB / LCSC: searched through JLCPCB's public parts search; symbol, footprint and
3D model come from EasyEDA's public component API and are converted by KiCad itself
(kicad-cli reads EasyEDA files), then go through the normal import pipeline.

Component Search Engine and SnapMagic have no public API and need your account for
downloads, so for them the part page is opened in your browser and the ZIP you
download there is picked up automatically.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import part_importer as pi

log = logging.getLogger("quick_part_import")

VERSION = "1.1.0"
USER_AGENT = f"KiCad-QuickPartImport/{VERSION} (+https://github.com/Jiggolino/kicad-quick-part-import)"
SEARCH_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"
COMPONENT_URL = "https://easyeda.com/api/products/{lcsc}/components?version=6.4.19.5"
STEP_URL = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"
EASYEDA_UNIT_MM = 0.254  # EasyEDA canvas unit = 10 mil
LCSC_NUMBER = re.compile(r"^[Cc]\d{1,9}$")


@dataclass
class Part:
    lcsc: str
    mpn: str
    manufacturer: str
    package: str
    description: str
    stock: int
    kind: str  # Basic / Preferred / Extended
    datasheet: str
    url: str

    @property
    def cse_url(self) -> str:
        return "https://componentsearchengine.com/search?term=" + urllib.parse.quote(self.mpn)

    @property
    def snapmagic_url(self) -> str:
        return "https://www.snapeda.com/search/?q=" + urllib.parse.quote(self.mpn)


def cse_search_url(query: str) -> str:
    return "https://componentsearchengine.com/search?term=" + urllib.parse.quote(query.strip())


def snapmagic_search_url(query: str) -> str:
    return "https://www.snapeda.com/search/?q=" + urllib.parse.quote(query.strip())


def _request(url: str, payload: dict | None = None, timeout: float = 20) -> bytes:
    host = urllib.parse.urlparse(url).hostname or url
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, */*"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        raise pi.PartImportError(f"{host} answered with error {e.code}.") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise pi.PartImportError(f"Could not reach {host} – check your internet connection. ({e})") from e
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def _json(url: str, payload: dict | None = None, timeout: float = 20) -> dict:
    try:
        data = json.loads(_request(url, payload, timeout).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise pi.PartImportError("The parts service sent an unexpected answer – try again later.") from e
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------------


def _part_from_jlc(item: dict) -> Part:
    kind = "Basic" if item.get("componentLibraryType") == "base" else (
        "Preferred" if item.get("preferredComponentFlag") else "Extended"
    )
    return Part(
        lcsc=str(item.get("componentCode") or ""),
        mpn=str(item.get("componentModelEn") or item.get("componentName") or ""),
        manufacturer=str(item.get("componentBrandEn") or ""),
        package=str(item.get("componentSpecificationEn") or ""),
        description=str(item.get("describe") or item.get("componentTypeEn") or ""),
        stock=int(item.get("stockCount") or 0),
        kind=kind,
        datasheet=str(item.get("dataManualUrl") or item.get("dataManualOfficialLink") or ""),
        url=str(item.get("lcscGoodsUrl") or ""),
    )


def _part_from_easyeda(lcsc: str) -> Part | None:
    """Look up a single LCSC number directly (for parts the JLC search does not list)."""
    data = _json(COMPONENT_URL.format(lcsc=urllib.parse.quote(lcsc)), timeout=30)
    result = data.get("result") or {}
    if not result.get("dataStr"):
        return None
    para = (result["dataStr"].get("head") or {}).get("c_para") or {}
    info = result.get("lcsc") or {}
    return Part(
        lcsc=lcsc,
        mpn=str(para.get("Manufacturer Part") or result.get("title") or lcsc),
        manufacturer=re.sub(r"\(.*?\)$", "", str(para.get("Manufacturer") or "")).strip(),
        package=str(para.get("package") or ""),
        description=str(result.get("description") or ""),
        stock=int(info.get("stock") or 0),
        kind="",
        datasheet="",
        url=str(info.get("url") or ""),
    )


def search(query: str, limit: int = 40) -> list[Part]:
    """Search JLCPCB/LCSC by part number, LCSC number or keyword."""
    query = query.strip()
    if not query:
        return []
    data = _json(SEARCH_URL, {"keyword": query, "currentPage": 1, "pageSize": limit})
    items = (((data.get("data") or {}).get("componentPageInfo") or {}).get("list")) or []
    parts = [_part_from_jlc(item) for item in items if item.get("componentCode")]
    if not parts and LCSC_NUMBER.match(query):
        part = _part_from_easyeda(query.upper())
        parts = [part] if part else []
    # JLC already ranks well; only pull exact hits up and push placeholder listings
    # (no stock, often without any CAD data) to the end
    q = query.lower()
    parts.sort(key=lambda p: (p.lcsc.lower() != q, not (p.mpn.lower() == q and p.stock > 0), p.stock == 0))
    return parts


# ----------------------------------------------------------------------------
# Download + conversion
# ----------------------------------------------------------------------------


def _model_info(footprint_doc: dict) -> tuple[str, tuple[float, float, float], tuple[float, float, float]] | None:
    """Return (uuid, offset mm, rotation deg) of the footprint's 3D model, in KiCad terms."""
    head = footprint_doc.get("head") or {}
    try:
        origin_x, origin_y = float(head.get("x") or 0), float(head.get("y") or 0)
    except (TypeError, ValueError):
        origin_x = origin_y = 0.0
    for shape in footprint_doc.get("shape") or []:
        if not isinstance(shape, str) or not shape.startswith("SVGNODE~"):
            continue
        try:
            node = json.loads(shape.split("~", 1)[1])
        except ValueError:
            continue
        attrs = node.get("attrs") or {}
        if not attrs.get("uuid") or attrs.get("c_etype", "outline3D") != "outline3D":
            continue
        try:
            cx, cy = (float(v) for v in (attrs.get("c_origin") or "0,0").split(",")[:2])
            x = (cx - origin_x) * EASYEDA_UNIT_MM
            y = -(cy - origin_y) * EASYEDA_UNIT_MM
            z = float(attrs.get("z") or 0) * EASYEDA_UNIT_MM
            # c_origin is sometimes stale; the drawn outline of the model is reliable
            points = [
                float(v)
                for child in node.get("childNodes") or []
                for v in str((child.get("attrs") or {}).get("points") or "").split()
            ]
            if len(points) >= 4:
                xs, ys = points[0::2], points[1::2]
                centre_x = ((min(xs) + max(xs)) / 2 - origin_x) * EASYEDA_UNIT_MM
                centre_y = -((min(ys) + max(ys)) / 2 - origin_y) * EASYEDA_UNIT_MM
                if abs(centre_x - x) > 0.1 or abs(centre_y - y) > 0.1:
                    x, y = centre_x, centre_y
            angles = [float(v) for v in (attrs.get("c_rotation") or "0,0,0").split(",")[:3]]
            angles += [0.0] * (3 - len(angles))
            rotation = tuple((360 - a) % 360 for a in angles)
        except (TypeError, ValueError):
            continue
        return str(attrs["uuid"]), (x, y, z), rotation  # type: ignore[return-value]
    return None


def _number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _model_block(file_name: str, offset: tuple[float, float, float], rotation: tuple[float, float, float]) -> str:
    xyz = lambda v: " ".join(_number(c) for c in v)  # noqa: E731
    return (
        f"\t(model {pi._quote(file_name)}\n\t\t(offset\n\t\t\t(xyz {xyz(offset)})\n\t\t)\n"
        f"\t\t(scale\n\t\t\t(xyz 1 1 1)\n\t\t)\n\t\t(rotate\n\t\t\t(xyz {xyz(rotation)})\n\t\t)\n\t)"
    )


def _convert_symbol_doc(doc: dict, path: Path, cli: pi.KicadCli) -> pi.SymbolLib:
    path.write_text(json.dumps(doc), encoding="utf-8")
    out = path.with_suffix(".kicad_sym")
    cli.sym_upgrade(path, out)  # KiCad's own EasyEDA importer
    lib = pi.SymbolLib.parse(out.read_text(encoding="utf-8"))
    if not lib.symbols:
        raise pi.PartImportError("the converted symbol is empty")
    return lib


def _join_units(name: str, units: list[str]) -> str:
    """Combine single-unit symbols (one per EasyEDA sub-part) into one multi-unit symbol."""
    _, _, kids = pi._children(units[0])
    fields = [units[0][s:e] for s, e in kids if pi._head(units[0][s:e])[0] != "symbol"]
    bodies: list[str] = []
    for number, unit in enumerate(units, 1):
        _, _, unit_kids = pi._children(unit)
        for s, e in unit_kids:
            body = unit[s:e]
            keyword, old_name, span = pi._head(body)
            if keyword != "symbol" or span is None or old_name is None:
                continue
            style = old_name.rsplit("_", 1)[-1] if re.search(r"_\d+_\d+$", old_name) else "1"
            bodies.append(body[: span[0]] + pi._quote(f"{name}_{number}_{style}") + body[span[1] :])
    return f"(symbol {pi._quote(name)}\n\t\t" + "\n\t\t".join(fields + bodies) + "\n\t)"


def _symbol_file(result: dict, tmp: Path, cli: pi.KicadCli, lcsc: str) -> pi.SourceFile:
    """Convert the EasyEDA symbol (multi-unit parts are stored as separate sub-parts)."""
    subparts = [sp["dataStr"] for sp in result.get("subparts") or [] if isinstance(sp.get("dataStr"), dict)]
    if not subparts:
        lib = _convert_symbol_doc(result["dataStr"], tmp / f"{lcsc}.json", cli)
        return pi.SourceFile(f"{lcsc}.kicad_sym", lib.render().encode("utf-8"))

    units: list[str] = []
    header: list[str] = []
    name = ""
    for number, doc in enumerate(subparts, 1):
        doc = json.loads(json.dumps(doc))  # copy: sub-parts are converted as normal symbols
        head = doc.setdefault("head", {})
        head["docType"] = "2"
        para = head.setdefault("c_para", {})
        para["pre"] = re.sub(r"\.\d+$", "", str(para.get("pre") or "U?"))
        lib = _convert_symbol_doc(doc, tmp / f"{lcsc}_unit{number}.json", cli)
        name = name or lib.symbols[0][0]
        header = header or lib.header
        units.append(lib.symbols[0][1])
    joined = pi.SymbolLib(header, [(name, _join_units(name, units))])
    return pi.SourceFile(f"{lcsc}.kicad_sym", joined.render().encode("utf-8"))


def download_part(part: Part, cli: pi.KicadCli) -> tuple[list[pi.SourceFile], dict[str, str], list[str]]:
    """Fetch and convert a part: returns (KiCad files, symbol fields, warnings)."""
    if not cli.available:
        raise pi.PartImportError("kicad-cli is needed to convert JLC/LCSC parts (it ships with KiCad).")
    data = _json(COMPONENT_URL.format(lcsc=urllib.parse.quote(part.lcsc)), timeout=30)
    result = data.get("result") or {}
    if not result.get("dataStr"):
        raise pi.PartImportError(f"There is no symbol/footprint for {part.lcsc} in the EasyEDA library.")

    files: list[pi.SourceFile] = []
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="quick_part_import_jlc_") as tmp_name:
        tmp = Path(tmp_name)

        try:
            files.append(_symbol_file(result, tmp, cli, part.lcsc))
        except (pi.PartImportError, ValueError) as e:
            warnings.append(f"Symbol could not be converted ({e})")

        footprint_doc = (result.get("packageDetail") or {}).get("dataStr")
        if isinstance(footprint_doc, dict):
            footprint_json = tmp / f"{part.lcsc}_footprint.json"
            footprint_json.write_text(json.dumps(footprint_doc), encoding="utf-8")
            pretty = tmp / "converted.pretty"
            try:
                cli.fp_upgrade(footprint_json, pretty)
            except pi.PartImportError as e:
                warnings.append(f"Footprint could not be converted ({e})")
            converted = sorted(pretty.glob("*.kicad_mod")) if pretty.is_dir() else []
            if converted:
                fp_file = converted[0]
                fp_text = fp_file.read_text(encoding="utf-8")
                model = _model_info(footprint_doc)
                if model:
                    uuid, offset, rotation = model
                    try:
                        step = _request(STEP_URL.format(uuid=urllib.parse.quote(uuid)), timeout=60)
                    except pi.PartImportError as e:
                        step = b""
                        warnings.append(f"3D model not downloaded ({e})")
                    if step.lstrip()[:12].upper().startswith(b"ISO-10303"):
                        model_name = pi.clean_file_name(fp_file.stem) + ".step"
                        files.append(pi.SourceFile(model_name, step))
                        _, root_close, _ = pi._children(fp_text)
                        fp_text = (
                            fp_text[:root_close].rstrip() + "\n" + _model_block(model_name, offset, rotation)
                            + "\n" + fp_text[root_close:]
                        )
                    elif step:
                        warnings.append("EasyEDA has no STEP model for this part.")
                files.append(pi.SourceFile(fp_file.name, fp_text.encode("utf-8")))
        else:
            warnings.append("EasyEDA has no footprint for this part.")

    if not files:
        raise pi.PartImportError(f"{part.lcsc} could not be converted: " + "; ".join(warnings))

    description = str(result.get("description") or "").strip() or part.description
    fields = {
        "LCSC Part #": part.lcsc,  # read by JLC fabrication/BOM plugins
        "Manufacturer": part.manufacturer,
        "Datasheet": part.datasheet or f"https://www.lcsc.com/datasheet/{part.lcsc}.pdf",
        "Description": description,
    }
    return files, fields, warnings


def import_part(library: pi.ProjectLibrary, part: Part) -> pi.ImportResult:
    files, fields, warnings = download_part(part, library.cli)
    result = library.import_files(files, f"{part.mpn} ({part.lcsc})", part.mpn, fields)
    result.warnings = warnings + result.warnings
    return result
