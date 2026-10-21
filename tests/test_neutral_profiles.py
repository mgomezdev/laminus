"""Contract tests for the neutral (model / nozzle-variant) profile endpoints.

Fixture catalog: a multi-nozzle P1S (0.4 / 0.6 / 0.4 high flow), an A1, a custom user
printer, and process/filament presets with mixed compatibility. As in
test_slice_start_validation.py, the catalog is injected straight onto app.main.
"""
import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.profile_catalog import ProfileCatalog
from tests.conftest import write_json

P1S_04 = "Bambu Lab P1S 0.4 nozzle"
P1S_06 = "Bambu Lab P1S 0.6 nozzle"
P1S_HF = "Bambu Lab P1S 0.4 High Flow nozzle"
A1_04 = "Bambu Lab A1 0.4 nozzle"
REF_P1S_04 = "machine:bambu-lab-p1s-0.4-nozzle"


@pytest.fixture
def tree(tmp_path):
    base = str(tmp_path)
    m = f"{base}/system/Bambu Lab/machine"
    for name, noz in ((P1S_04, 0.4), (P1S_06, 0.6), (P1S_HF, 0.4), (A1_04, 0.4)):
        write_json(f"{m}/{name}.json", {"name": name, "nozzle_diameter": [str(noz)], "bed_size_x": 256, "bed_size_y": 256})
    write_json(f"{base}/system/Prusa/machine/Prusa MK4S 0.4 nozzle.json",
               {"name": "Prusa MK4S 0.4 nozzle", "nozzle_diameter": ["0.4"], "bed_size_x": 250, "bed_size_y": 210})
    write_json(f"{base}/user/default/machine/My Custom Printer.json", {"name": "My Custom Printer"})
    p = f"{base}/system/Bambu Lab/process"
    write_json(f"{p}/Base.json", {"name": "Base", "layer_height": 0.3})
    write_json(f"{p}/0.20mm P1S.json", {"name": "0.20mm P1S", "inherits": "Base", "layer_height": 0.2,
                                        "compatible_printers": [P1S_04, P1S_HF]})
    write_json(f"{p}/0.30mm P1S 0.6.json", {"name": "0.30mm P1S 0.6", "inherits": "Base",
                                            "compatible_printers": [P1S_06]})
    write_json(f"{p}/Universal.json", {"name": "Universal", "layer_height": 0.2})
    f = f"{base}/system/Bambu Lab/filament"
    write_json(f"{f}/PLA P1S+A1.json", {"name": "PLA P1S+A1", "filament_type": "PLA",
                                         "compatible_printers": [P1S_04, A1_04]})
    write_json(f"{f}/PLA Any.json", {"name": "PLA Any", "filament_type": "PLA"})
    return {"system_dir": f"{base}/system", "user_dir": f"{base}/user"}


def _built(tree):
    cat = ProfileCatalog(**tree)
    cat.build()
    return cat


@pytest.fixture
def client(tree, monkeypatch):
    monkeypatch.setattr(main_mod, "catalog", _built(tree))
    monkeypatch.setattr(main_mod, "_catalog_building", False)
    monkeypatch.setattr(main_mod, "_stale_catalog", None)
    return TestClient(main_mod.app)


def _by_name(items):
    return {i["name"]: i for i in items}


# ---- suggest ----

def test_suggest_exact_model_returns_all_nozzle_variants_default_first(client):
    r = client.post("/api/profiles/machines/suggest", json={"manufacturer": "Bambu Lab", "model": "P1S"})
    assert r.status_code == 200
    body = r.json()
    assert r.headers["X-Catalog-Version"] == body["catalog_version"]
    assert "stale" not in body
    c = body["candidates"]
    assert [x["name"] for x in c[:3]] == [P1S_04, P1S_HF, P1S_06]
    assert all(x["match_reason"] == "exact_model" and x["confidence"] == 1.0 for x in c[:3])
    by = _by_name(c)
    assert (by[P1S_04]["nozzle_diameter_mm"], by[P1S_04]["flow"]) == (0.4, "standard")
    assert (by[P1S_06]["nozzle_diameter_mm"], by[P1S_06]["flow"]) == (0.6, "standard")
    assert (by[P1S_HF]["nozzle_diameter_mm"], by[P1S_HF]["flow"]) == (0.4, "high_flow")
    assert by[P1S_04]["ref"] == REF_P1S_04
    # other Bambu models trail as low-confidence vendor_only
    assert c[3]["match_reason"] == "vendor_only" and c[3]["confidence"] < 0.5
    assert [x["confidence"] for x in c] == sorted((x["confidence"] for x in c), reverse=True)


def test_suggest_fuzzy_and_model_key_and_embedded_manufacturer(client):
    r = client.post("/api/profiles/machines/suggest",
                    json={"manufacturer": "Bambulab", "model": "Bambu Lab P1S Combo", "model_key": "p1s"})
    top = r.json()["candidates"][0]
    assert top["name"] == P1S_04
    assert top["match_reason"] in ("exact_model", "fuzzy_name")


def test_suggest_no_match_is_empty_not_404(client):
    r = client.post("/api/profiles/machines/suggest", json={"manufacturer": "Nobody", "model": "Zzz9000"})
    assert r.status_code == 200
    assert r.json()["candidates"] == []


def test_suggest_requires_manufacturer_and_model(client):
    assert client.post("/api/profiles/machines/suggest", json={"model": "P1S"}).status_code == 422


# ---- variants ----

def test_variants_multi_nozzle_model(client):
    r = client.post("/api/profiles/models/variants", json={"manufacturer": "Bambu Lab", "model": "P1S"})
    v = r.json()["variants"]
    assert [(x["nozzle_diameter_mm"], x["flow"]) for x in v] == [(0.4, "standard"), (0.4, "high_flow"), (0.6, "standard")]
    assert [x["is_default_guess"] for x in v] == [True, False, False]
    assert all("P1S" in x["name"] for x in v)


def test_variants_default_falls_back_when_no_04(client, tree):
    write_json(tree["system_dir"] + "/Prusa/machine/Prusa XL 0.6 nozzle.json",
               {"name": "Prusa XL 0.6 nozzle", "nozzle_diameter": ["0.6"]})
    write_json(tree["system_dir"] + "/Prusa/machine/Prusa XL 0.8 nozzle.json",
               {"name": "Prusa XL 0.8 nozzle", "nozzle_diameter": ["0.8"]})
    main_mod.catalog = _built(tree)
    v = client.post("/api/profiles/models/variants", json={"manufacturer": "Prusa", "model": "XL"}).json()["variants"]
    assert [x["is_default_guess"] for x in v] == [True, False]
    assert v[0]["nozzle_diameter_mm"] == 0.6


def test_variants_unknown_model_empty(client):
    r = client.post("/api/profiles/models/variants", json={"manufacturer": "Bambu Lab", "model": "Nope"})
    assert r.status_code == 200 and r.json()["variants"] == []


# ---- presets ----

def test_presets_filtered_by_compatibility_and_inheritance(client):
    r = client.get(f"/api/profiles/machines/{REF_P1S_04}/presets")
    assert r.status_code == 200
    body = r.json()
    assert body["machine_ref"] == REF_P1S_04
    by = _by_name(body["presets"])
    # 0.6-only process excluded; inherited ("Base") + universal presets handled
    assert "0.20mm P1S" in by and "0.30mm P1S 0.6" not in by
    assert "Universal" in by and "PLA Any" in by and "PLA P1S+A1" in by
    assert by["0.20mm P1S"]["kind"] == "process" and by["PLA Any"]["kind"] == "filament"
    assert by["0.20mm P1S"]["ref"] == "process:0.20mm-p1s"
    assert by["0.20mm P1S"]["compatible_models"] == ["Bambu Lab P1S"]
    assert by["PLA P1S+A1"]["compatible_models"] == ["Bambu Lab A1", "Bambu Lab P1S"]
    assert "compatible_models" not in by["Universal"]


def test_presets_kind_filter(client):
    ref = "machine:bambu-lab-p1s-0.6-nozzle"
    p = client.get(f"/api/profiles/machines/{ref}/presets?kind=process").json()["presets"]
    assert {x["kind"] for x in p} == {"process"} and "0.30mm P1S 0.6" in _by_name(p)
    f = client.get(f"/api/profiles/machines/{ref}/presets?kind=filament").json()["presets"]
    assert {x["kind"] for x in f} == {"filament"} and "PLA P1S+A1" not in _by_name(f)
    assert client.get(f"/api/profiles/machines/{ref}/presets?kind=bogus").status_code == 422


def test_presets_unknown_machine_ref_404(client):
    r = client.get("/api/profiles/machines/machine:nope/presets")
    assert r.status_code == 404 and r.json()["error"] == "unknown_machine_ref"


# ---- supports ----

def test_supports(client):
    base = f"/api/profiles/machines/{REF_P1S_04}/supports"
    assert client.get(base).json()["supported"] is True
    ok = client.get(base + "?bed_x=256&bed_y=256").json()
    assert (ok["supported"], ok["reason"]) == (True, None)
    bad = client.get(base + "?bed_x=180&bed_y=180").json()
    assert (bad["supported"], bad["reason"]) == (False, "bed_mismatch")
    unk = client.get("/api/profiles/machines/machine:nope/supports")
    assert unk.status_code == 200
    assert unk.json()["supported"] is False and unk.json()["reason"] == "unknown_machine_ref"


def test_supports_machine_without_bed_size_not_ruled_out(client):
    r = client.get("/api/profiles/machines/machine:my-custom-printer/supports?bed_x=999&bed_y=1").json()
    assert r["supported"] is True


# ---- refs / version / stale ----

def test_refs_and_version_stable_across_rebuild_and_change_with_content(client, tree):
    url = f"/api/profiles/machines/{REF_P1S_04}/supports"
    v1 = client.get(url).json()["catalog_version"]
    main_mod.catalog = _built(tree)
    r = client.get(url)
    assert r.json()["supported"] and r.json()["catalog_version"] == v1
    write_json(tree["system_dir"] + "/Bambu Lab/machine/Bambu Lab P1S 0.8 nozzle.json",
               {"name": "Bambu Lab P1S 0.8 nozzle", "nozzle_diameter": ["0.8"]})
    main_mod.catalog = _built(tree)
    r2 = client.get(url)
    assert r2.json()["catalog_version"] != v1
    assert r2.headers["X-Catalog-Version"] == r2.json()["catalog_version"]


def test_stale_flag_while_rebuilding(client, monkeypatch):
    monkeypatch.setattr(main_mod, "_catalog_building", True)
    assert client.get("/api/profiles/machines/machine:x/supports").json()["stale"] is True


def test_serves_stale_cache_when_no_live_catalog(client, tree, tmp_path, monkeypatch):
    cache = str(tmp_path / "cache.json")
    _built(tree).save_to_cache(cache, "old-key")
    monkeypatch.setattr(main_mod, "catalog", None)
    monkeypatch.setattr(main_mod, "_CATALOG_CACHE_FILE", cache)
    r = client.post("/api/profiles/models/variants", json={"manufacturer": "Bambu Lab", "model": "P1S"})
    assert r.status_code == 200 and r.json()["stale"] is True and len(r.json()["variants"]) == 3


def test_503_when_no_catalog_at_all(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "catalog", None)
    monkeypatch.setattr(main_mod, "_CATALOG_CACHE_FILE", str(tmp_path / "missing.json"))
    for r in (
        client.post("/api/profiles/machines/suggest", json={"manufacturer": "a", "model": "b"}),
        client.get("/api/profiles/machines/machine:x/presets"),
        client.get("/api/profiles/machines/machine:x/supports"),
    ):
        assert r.status_code == 503 and r.json() == {"error": "catalog_unavailable"}


def test_existing_endpoints_unchanged(client):
    assert set(client.get("/api/profiles").json()) == {"machine", "process", "filament"}
    assert client.get("/api/profiles/broken").status_code == 200


def test_abstract_bases_hidden_and_printable_area_bed(client, tree):
    write_json(tree["system_dir"] + "/Bambu Lab/process/fdm_process_common.json",
               {"name": "fdm_process_common", "instantiation": "false"})
    write_json(tree["system_dir"] + "/Bambu Lab/machine/fdm_machine_common.json",
               {"name": "fdm_machine_common", "instantiation": "false"})
    write_json(tree["system_dir"] + "/Bambu Lab/machine/Bambu Lab X1 0.4 nozzle.json",
               {"name": "Bambu Lab X1 0.4 nozzle", "nozzle_diameter": ["0.4"],
                "printable_area": ["0x0", "256x0", "256x256", "0x256"]})
    main_mod.catalog = _built(tree)
    names = {p["name"] for p in client.get(f"/api/profiles/machines/{REF_P1S_04}/presets").json()["presets"]}
    assert "fdm_process_common" not in names
    cands = client.post("/api/profiles/machines/suggest", json={"manufacturer": "Bambu Lab", "model": "X1"}).json()["candidates"]
    assert "fdm_machine_common" not in {c["name"] for c in cands}
    base = "/api/profiles/machines/machine:bambu-lab-x1-0.4-nozzle/supports"
    assert client.get(base + "?bed_x=256&bed_y=256").json()["supported"] is True
    assert client.get(base + "?bed_x=220&bed_y=220").json()["reason"] == "bed_mismatch"


def test_universal_presets_scoped_to_vendor_bundle(client, tree):
    write_json(tree["system_dir"] + "/Prusa/process/Prusa Universal.json", {"name": "Prusa Universal"})
    write_json(tree["system_dir"] + "/OrcaFilamentLibrary/filament/Generic PLA.json", {"name": "Generic PLA"})
    write_json(tree["user_dir"] + "/default/process/My Process.json", {"name": "My Process"})
    main_mod.catalog = _built(tree)
    names = {p["name"] for p in client.get(f"/api/profiles/machines/{REF_P1S_04}/presets").json()["presets"]}
    assert "Prusa Universal" not in names
    assert {"Generic PLA", "My Process", "Universal"} <= names
