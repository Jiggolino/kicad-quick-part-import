"""
Tests for part_importer with small hand-made KiCad files (needs kicad-cli).

    "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" tests/test_importer.py

Also works with pytest.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins"))
import part_importer as pi  # noqa: E402

CLI = pi.find_kicad_cli()

SYMBOL = """(kicad_symbol_lib (version 20211014) (generator SamacSys_ECAD_Model)
  (symbol "TESTPART" (in_bom yes) (on_board yes)
    (property "Reference" "U" (id 0) (at 0 5.08 0) (effects (font (size 1.27 1.27))))
    (property "Value" "TESTPART" (id 1) (at 0 -7.62 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "SOT23_TEST" (id 2) (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "" (id 3) (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "TESTPART_0_1"
      (rectangle (start -5.08 2.54) (end 5.08 -2.54) (stroke (width 0.254) (type default) (color 0 0 0 0)) (fill (type background)))
    )
    (symbol "TESTPART_1_1"
      (pin input line (at -7.62 0 0) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin output line (at 7.62 0 180) (length 2.54) (name "OUT" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      (pin power_in line (at 0 -5.08 90) (length 2.54) (name "GND" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""

FOOTPRINT = """(module "SOT23_TEST" (layer F.Cu)
  (fp_text reference REF** (at 0 -2) (layer F.SilkS) (effects (font (size 1 1) (thickness 0.15))))
  (fp_text value SOT23_TEST (at 0 2) (layer F.Fab) (effects (font (size 1 1) (thickness 0.15))))
  (pad 1 smd rect (at -0.95 1) (size 0.6 0.7) (layers F.Cu F.Paste F.Mask))
  (pad 2 smd rect (at 0.95 1) (size 0.6 0.7) (layers F.Cu F.Paste F.Mask))
  (pad 3 smd rect (at 0 -1) (size 0.6 0.7) (layers F.Cu F.Paste F.Mask))
  (model TESTPART.stp (at (xyz 0 0 0.02)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
)
"""

STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"


def rename(text: str, old: str, new: str) -> str:
    return text.replace(f'"{old}"', f'"{new}"').replace(f" {old} ", f" {new} ")


class Workspace:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="qpi_test_"))

    def zip(self, name: str, files: dict[str, str | bytes]) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as zf:
            for member, data in files.items():
                zf.writestr(member, data)
        return path

    def project(self, name: str, tables: dict[str, str] | None = None) -> Path:
        folder = self.root / name
        folder.mkdir()
        (folder / f"{name}.kicad_pro").write_text("{}")
        for file_name, text in (tables or {}).items():
            (folder / file_name).write_text(text)
        return folder / f"{name}.kicad_pro"

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def lib_text(project: Path) -> str:
    return (project.parent / "lib" / f"{pi.clean_lib_name(project.stem)}.kicad_sym").read_text(encoding="utf-8")


def footprint_fields(text: str) -> list[str]:
    return re.findall(r'\(property "Footprint" "([^"]*)"', text)


def symbol_names(text: str) -> list[str]:
    return re.findall(r'^\t\(symbol "([^"]*)"', text, re.M)


def run(project: Path, *paths: Path) -> pi.ImportResult:
    return pi.import_into_project(project, list(paths), CLI)


# ---------------------------------------------------------------------------


def test_component_search_engine_layout(ws: Workspace) -> None:
    part = ws.zip(
        "LIB_TESTPART.zip",
        {
            "TESTPART/KiCad/TESTPART.kicad_sym": SYMBOL,
            "TESTPART/KiCad/SOT23_TEST.kicad_mod": FOOTPRINT,
            "TESTPART/CADSTAR/TESTPART.lib": "not a kicad library",
            "TESTPART/3D/TESTPART.stp": STEP,
            "TESTPART/Altium/TESTPART.SchLib": b"\x00",
        },
    )
    project = ws.project("My Board")
    result = run(project, part)
    assert result.symbols == ["TESTPART"] and result.footprints == ["SOT23_TEST"]
    assert result.models == ["TESTPART.stp"] and result.library_added
    assert footprint_fields(lib_text(project)) == ["My_Board:SOT23_TEST"]
    fp = (project.parent / "lib" / "My_Board.pretty" / "SOT23_TEST.kicad_mod").read_text(encoding="utf-8")
    assert fp.startswith('(footprint "SOT23_TEST"')
    assert '(model "${KIPRJMOD}/lib/My_Board.3dshapes/TESTPART.stp"' in fp
    assert (project.parent / "lib" / "My_Board.3dshapes" / "TESTPART.stp").read_bytes() == STEP
    sym_table = (project.parent / "sym-lib-table").read_text()
    assert '(uri "${KIPRJMOD}/lib/My_Board.kicad_sym")' in sym_table
    assert '(uri "${KIPRJMOD}/lib/My_Board.pretty")' in (project.parent / "fp-lib-table").read_text()

    again = run(project, part)  # same part again: updated, not duplicated
    assert "TESTPART" in again.updated and not again.library_added
    assert symbol_names(lib_text(project)) == ["TESTPART"]
    assert sym_table == (project.parent / "sym-lib-table").read_text()


def test_snapeda_without_model_reference(ws: Workspace) -> None:
    no_model = re.sub(r"\n  \(model .*\)\n", "\n", FOOTPRINT)
    part = ws.zip("TESTPART.zip", {"TESTPART.kicad_sym": SYMBOL, "SOT23_TEST.kicad_mod": no_model, "TESTPART.step": STEP, "how-to-import.htm": "<html/>"})
    project = ws.project("Snap")
    run(project, part)
    fp = (project.parent / "lib" / "Snap.pretty" / "SOT23_TEST.kicad_mod").read_text(encoding="utf-8")
    assert '(model "${KIPRJMOD}/lib/Snap.3dshapes/TESTPART.step"' in fp


def test_ultralibrarian_variants_and_prefixed_footprint(ws: Workspace) -> None:
    symbol = SYMBOL.replace('"Footprint" "SOT23_TEST"', '"Footprint" "footprints:SOT23_TEST-M"')
    files = {"KiCADv6/2026-01-01_00-00-00.kicad_sym": symbol, "STEP/TESTPART.step": STEP}
    for variant in ("SOT23_TEST", "SOT23_TEST-L", "SOT23_TEST-M"):
        files[f"KiCADv6/footprints.pretty/{variant}.kicad_mod"] = rename(FOOTPRINT, "SOT23_TEST", variant)
    project = ws.project("UL")
    result = run(project, ws.zip("ul_TESTPART.zip", files))
    assert sorted(result.footprints) == ["SOT23_TEST", "SOT23_TEST-L", "SOT23_TEST-M"]
    assert footprint_fields(lib_text(project)) == ["UL:SOT23_TEST-M"]


def test_wrl_and_step_reference_collapse_to_one(ws: Workspace) -> None:
    two = FOOTPRINT.replace(
        "  (model TESTPART.stp",
        "  (model TESTPART.wrl (at (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))\n  (model C:/SamacSys/TESTPART.stp",
    )
    part = ws.zip("p.zip", {"KiCad/P.kicad_sym": SYMBOL, "KiCad/F.kicad_mod": two, "3D/TESTPART.wrl": "#VRML V2.0 utf8\n", "3D/TESTPART.stp": STEP})
    project = ws.project("Two")
    run(project, part)
    fp = (project.parent / "lib" / "Two.pretty" / "SOT23_TEST.kicad_mod").read_text(encoding="utf-8")
    assert fp.count("(model ") == 1 and "TESTPART.stp" in fp
    assert re.search(r"\(xyz 0 0 0\.50(8|79)", fp)  # offset of the STEP reference (0.02 inch) was kept


def test_existing_tables_are_kept_and_name_clash_is_avoided(ws: Workspace) -> None:
    sym = '(sym_lib_table\n  (version 7)\n  (lib (name "Other")(type "KiCad")(uri "${KIPRJMOD}/other.kicad_sym")(options "")(descr ""))\n)\n'
    fp = '(fp_lib_table\n  (version 7)\n  (lib (name Tables)(type KiCad)(uri ${KIPRJMOD}/mine.pretty)(options "")(descr ""))\n)\n'
    project = ws.project("Tables", {"sym-lib-table": sym, "fp-lib-table": fp})
    run(project, ws.zip("p.zip", {"a.kicad_sym": SYMBOL, "b.kicad_mod": FOOTPRINT}))
    new_sym = (project.parent / "sym-lib-table").read_text()
    new_fp = (project.parent / "fp-lib-table").read_text()
    assert "other.kicad_sym" in new_sym and new_sym.count("(lib ") == 2
    assert "mine.pretty" in new_fp and '(name "Tables_2")' in new_fp
    assert footprint_fields(lib_text(project)) == ["Tables_2:SOT23_TEST"]


def test_second_part_and_symbol_only_download(ws: Workspace) -> None:
    project = ws.project("Multi")
    run(project, ws.zip("one.zip", {"a.kicad_sym": SYMBOL, "b.kicad_mod": FOOTPRINT}))
    second = rename(SYMBOL, "TESTPART", "SECOND").replace('"TESTPART_', '"SECOND_')
    result = run(project, ws.zip("two.zip", {"a.kicad_sym": second}))
    assert result.symbols == ["SECOND"] and not result.footprints
    text = lib_text(project)
    assert sorted(symbol_names(text)) == ["SECOND", "TESTPART"]
    assert footprint_fields(text) == ["Multi:SOT23_TEST", "Multi:SOT23_TEST"]  # linked to existing footprint


def test_bad_inputs_change_nothing(ws: Workspace) -> None:
    project = ws.project("Bad")
    junk = ws.zip("photos.zip", {"a.txt": "hi", "__MACOSX/._x.kicad_sym": b"\x00"})
    corrupt = ws.root / "corrupt.zip"
    corrupt.write_bytes(ws.zip("tmp.zip", {"a.kicad_sym": SYMBOL}).read_bytes()[:40])
    for path in (junk, corrupt):
        try:
            run(project, path)
        except pi.PartImportError:
            pass
        else:
            raise AssertionError(f"{path.name} should have been rejected")
    assert sorted(p.name for p in project.parent.iterdir()) == ["Bad.kicad_pro"]

    run(project, ws.zip("ok.zip", {"a.kicad_sym": SYMBOL, "b.kicad_mod": FOOTPRINT}))
    library = project.parent / "lib" / "Bad.kicad_sym"
    library.write_text('(kicad_symbol_lib (version 20211014) (symbol "BROKEN" ')
    before = {p: p.read_bytes() for p in project.parent.rglob("*") if p.is_file()}
    try:
        run(project, ws.zip("ok2.zip", {"a.kicad_sym": rename(SYMBOL, "TESTPART", "NEW")}))
    except pi.PartImportError:
        pass
    else:
        raise AssertionError("damaged library must not be overwritten")
    after = {p: p.read_bytes() for p in project.parent.rglob("*") if p.is_file()}
    assert before == after


def test_loose_files_and_nested_zip(ws: Workspace) -> None:
    folder = ws.root / "downloaded_files"
    folder.mkdir()
    (folder / "a.kicad_sym").write_text(SYMBOL)
    (folder / "b.kicad_mod").write_text(FOOTPRINT)
    project = ws.project("Loose")
    assert run(project, folder / "a.kicad_sym", folder / "b.kicad_mod").footprints == ["SOT23_TEST"]
    inner = ws.zip("inner.zip", {"KiCad/a.kicad_sym": SYMBOL})
    outer = ws.zip("outer.zip", {"x/inner.zip": inner.read_bytes()})
    assert pi.looks_like_part_zip(outer)
    assert run(ws.project("Nested"), outer).symbols == ["TESTPART"]


def main() -> int:
    if not CLI:
        print("kicad-cli not found - install KiCad 8 or newer")
        return 1
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
