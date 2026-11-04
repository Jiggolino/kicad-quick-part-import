"""
Tests for the JLC / LCSC search (offline; set QPI_ONLINE=1 to also hit the real services).

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" tests/test_jlc_parts.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jlc_parts as jlc  # noqa: E402
import part_importer as pi  # noqa: E402
from test_importer import Workspace  # noqa: E402


def jlc_item(code: str, mpn: str, stock: int, library: str = "expand", preferred: bool = False) -> dict:
    return {
        "componentCode": code,
        "componentModelEn": mpn,
        "componentBrandEn": "Texas Instruments",
        "componentSpecificationEn": "SOIC-8",
        "stockCount": stock,
        "componentLibraryType": library,
        "preferredComponentFlag": preferred,
        "describe": "buck converter",
        "dataManualUrl": "https://example.invalid/ds.pdf",
    }


def fake_search(items: list[dict]):
    original = jlc._json
    jlc._json = lambda url, payload=None, timeout=20: {"data": {"componentPageInfo": {"list": items}}}
    return original


def test_ranking_keeps_jlc_order_but_drops_placeholders(ws: Workspace) -> None:
    original = fake_search([
        jlc_item("C9865", "TPS54331DR", 392955, preferred=True),
        jlc_item("C90761", "TPS54331DDAR", 47084),
        jlc_item("C9900006812", "TPS54331", 0),  # exact name, but an empty placeholder listing
        jlc_item("C2071648", "TPS54331DRG4", 0),
        jlc_item("C25804", "0603WAF1002T5E", 100, library="base"),
    ])
    try:
        parts = jlc.search("TPS54331")
    finally:
        jlc._json = original
    assert [p.lcsc for p in parts] == ["C9865", "C90761", "C25804", "C9900006812", "C2071648"]
    assert parts[0].kind == "Preferred" and parts[2].kind == "Basic" and parts[1].kind == "Extended"


def test_exact_lcsc_number_comes_first(ws: Workspace) -> None:
    original = fake_search([jlc_item("C1", "A", 10), jlc_item("C9865", "TPS54331DR", 5)])
    try:
        parts = jlc.search("c9865")
    finally:
        jlc._json = original
    assert parts[0].lcsc == "C9865"


def test_3d_model_placement(ws: Workspace) -> None:
    def node(origin: str, rotation: str, z: str, points: str | None = None) -> str:
        data = {"attrs": {"uuid": "abc", "c_etype": "outline3D", "c_origin": origin, "c_rotation": rotation, "z": z}}
        if points:
            data["childNodes"] = [{"attrs": {"points": points}}]
        return "SVGNODE~" + json.dumps(data)

    doc = {"head": {"x": "4000", "y": "3000"}, "shape": ["TRACK~1~3~~4000 3000 4010 3000", node("4000,3000", "0,0,90", "0")]}
    uuid, offset, rotation = jlc._model_info(doc)
    assert uuid == "abc" and offset == (0.0, -0.0, 0.0) and rotation == (0.0, 0.0, 270.0)

    # c_origin 10 units right and 4 down -> +2.54 mm x, -1.016 mm y; z in canvas units too
    _, offset, _ = jlc._model_info({"head": {"x": "0", "y": "0"}, "shape": [node("10,4", "0,0,0", "-3")]})
    assert [round(v, 3) for v in offset] == [2.54, -1.016, -0.762]

    # a stale c_origin is corrected from the drawn outline (centre at 20,0)
    stale = node("0,0", "0,0,0", "0", points="15 -5 25 -5 25 5 15 5")
    _, offset, _ = jlc._model_info({"head": {"x": "0", "y": "0"}, "shape": [stale]})
    assert round(offset[0], 3) == 5.08 and round(offset[1], 3) == 0

    assert jlc._model_info({"head": {}, "shape": ["TRACK~1"]}) is None


def test_symbol_fields_are_set(ws: Workspace) -> None:
    from test_importer import FOOTPRINT, SYMBOL, lib_text

    project = ws.project("Fields")
    library = pi.ProjectLibrary(project, pi.KicadCli(pi.find_kicad_cli()))
    files = [pi.SourceFile("a.kicad_sym", SYMBOL.encode()), pi.SourceFile("b.kicad_mod", FOOTPRINT.encode())]
    fields = {"LCSC Part #": "C123", "Manufacturer": "ACME", "Datasheet": "https://example.invalid/x.pdf", "Description": ""}
    library.import_files(files, "test", "TESTPART", fields)
    text = lib_text(project)
    assert '(property "LCSC Part #" "C123"' in text and '(property "Manufacturer" "ACME"' in text
    assert '(property "Datasheet" "https://example.invalid/x.pdf"' in text


def test_sub_parts_become_units(ws: Workspace) -> None:
    from test_importer import SYMBOL, lib_text

    block = pi.SymbolLib.parse(SYMBOL).symbols[0][1]
    second = block.replace('(number "1"', '(number "4"').replace('(number "2"', '(number "5"').replace('(number "3"', '(number "6"')
    joined = jlc._join_units("DUAL", [block, second])
    assert joined.count('(property "Reference"') == 1
    assert '(symbol "DUAL_1_1"' in joined and '(symbol "DUAL_2_1"' in joined and "TESTPART_" not in joined

    project = ws.project("Units")  # KiCad itself must accept the joined symbol
    library = pi.ProjectLibrary(project, pi.KicadCli(pi.find_kicad_cli()))
    lib = pi.SymbolLib(pi.SymbolLib.parse(SYMBOL).header, [("DUAL", joined)])
    library.import_files([pi.SourceFile("dual.kicad_sym", lib.render().encode())], "dual")
    text = lib_text(project)
    assert '(symbol "DUAL_2_1"' in text and text.count("(pin ") == 6


def test_online_import(ws: Workspace) -> None:
    if not os.environ.get("QPI_ONLINE"):
        print("      (skipped – set QPI_ONLINE=1 to query JLC/EasyEDA)")
        return
    project = ws.project("Online")
    library = pi.ProjectLibrary(project, pi.KicadCli(pi.find_kicad_cli()))
    part = jlc.search("C9865")[0]
    result = jlc.import_part(library, part)
    assert result.symbols == ["TPS54331DR"] and result.footprints and result.models
    assert result.links[0][1].startswith("Online:SOIC-8")
    dual = jlc.import_part(library, jlc.search("C513508")[0])  # op-amp stored as two sub-parts
    text = lib_text_of(project)
    assert f'(symbol "{dual.symbols[0]}_2_' in text


def lib_text_of(project: Path) -> str:
    from test_importer import lib_text

    return lib_text(project)


def main() -> int:
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, test in tests:
        ws = Workspace()
        try:
            test(ws)
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
        finally:
            ws.close()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
