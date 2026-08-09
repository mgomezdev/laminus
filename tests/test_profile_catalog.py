import json, pytest
from app.profile_catalog import resolve_inheritance, _build_name_index
from tests.conftest import write_json

def test_resolve_flat_profile(tmp_path):
    p = tmp_path / "leaf.json"
    p.write_text(json.dumps({"name": "Leaf", "layer_height": 0.2}))
    result = resolve_inheritance(str(p), _build_name_index([str(tmp_path)]))
    assert result["layer_height"] == 0.2
    assert "inherits" not in result

def test_resolve_single_parent(tmp_path):
    parent = tmp_path / "Parent.json"
    parent.write_text(json.dumps({"name": "Parent", "layer_height": 0.3, "speed": 50}))
    child = tmp_path / "Child.json"
    child.write_text(json.dumps({"name": "Child", "inherits": "Parent", "layer_height": 0.2}))
    result = resolve_inheritance(str(child), _build_name_index([str(tmp_path)]))
    assert result["layer_height"] == 0.2
    assert result["speed"] == 50
    assert "inherits" not in result

def test_resolve_cycle_raises(tmp_path):
    a = tmp_path / "A.json"
    b = tmp_path / "B.json"
    a.write_text(json.dumps({"name": "A", "inherits": "B"}))
    b.write_text(json.dumps({"name": "B", "inherits": "A"}))
    with pytest.raises(ValueError, match="[Cc]ircular"):
        resolve_inheritance(str(a), _build_name_index([str(tmp_path)]))

def test_resolve_missing_parent_raises(tmp_path):
    # An unresolvable `inherits` parent must not silently degrade to the bare leaf -
    # that leaf is typically missing most of its real settings (nozzle temp, layer
    # height, compatible_printers) and would otherwise be published and used for
    # slicing as if it were a complete, valid profile.
    child = tmp_path / "Child.json"
    child.write_text(json.dumps({"name": "Child", "inherits": "Ghost", "layer_height": 0.2}))
    with pytest.raises(ValueError, match="Ghost"):
        resolve_inheritance(str(child), _build_name_index([str(tmp_path)]))

from app.profile_catalog import make_profile_uuid, make_machine_uuid, parse_machine_name

def test_make_profile_uuid_is_stable():
    u1 = make_profile_uuid("system", "Bambu Lab/filament/Bambu PLA Basic.json")
    u2 = make_profile_uuid("system", "Bambu Lab/filament/Bambu PLA Basic.json")
    assert u1 == u2

def test_make_profile_uuid_differs_by_source():
    assert make_profile_uuid("system", "foo/bar.json") != make_profile_uuid("user", "foo/bar.json")

def test_make_machine_uuid_is_stable():
    assert make_machine_uuid("Bambu Lab", "P1S", "0.4") == make_machine_uuid("Bambu Lab", "P1S", "0.4")

def test_make_machine_uuid_differs_by_nozzle():
    assert make_machine_uuid("Bambu Lab", "P1S", "0.4") != make_machine_uuid("Bambu Lab", "P1S", "0.6")

def test_parse_machine_name_standard():
    mfr, model, nozzle = parse_machine_name("Bambu Lab P1S 0.4 nozzle")
    assert mfr == "Bambu Lab"
    assert model == "P1S"
    assert nozzle == "0.4"

def test_parse_machine_name_multi_word_model():
    mfr, model, nozzle = parse_machine_name("Creality Ender-3 V2 0.4 nozzle")
    assert mfr == "Creality"
    assert model == "Ender-3 V2"
    assert nozzle == "0.4"

def test_parse_machine_name_no_match_returns_none():
    assert parse_machine_name("Custom Handbuilt Printer") is None


from app.profile_catalog import ProfileCatalog

def test_catalog_build_counts(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    data = cat.as_dict()
    assert len(data["machine"]) == 2
    assert len(data["process"]) == 2
    assert len(data["filament"]) == 1

def test_catalog_machine_has_tuple_fields(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    p1s = next(m for m in cat.as_dict()["machine"] if "P1S" in m["name"])
    assert p1s["manufacturer"] == "Bambu Lab"
    assert p1s["model"] == "P1S"
    assert p1s["nozzle"] == "0.4"
    assert "uuid" in p1s

def test_catalog_filament_display_name(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    fil = cat.as_dict()["filament"][0]
    assert fil["display_name"] == "Bambu PLA Basic"
    assert fil["source"] == "system"

def test_catalog_process_inheritance_resolved(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    standard = next(p for p in cat.as_dict()["process"] if "Standard" in p["name"])
    assert standard["layer_height"] == 0.2
    assert standard["speed"] == 50

def test_get_by_uuid(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    fil = cat.as_dict()["filament"][0]
    assert cat.get_by_uuid(fil["uuid"]) is not None

def test_get_machine_by_tuple(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    m = cat.get_machine("Bambu Lab", "P1S", "0.4")
    assert m is not None and m["bed_size_x"] == 256

def test_filter_by_machine_tuple(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    data = cat.as_dict(manufacturer="Bambu Lab", model="P1S", nozzle="0.4")
    for p in data["process"]:
        assert not p.get("compatible_printers") or "Bambu Lab P1S 0.4 nozzle" in p["compatible_printers"]


def test_empty_list_field_does_not_abort_build(profile_tree):
    """A profile with an empty-list value (e.g. 'filament_colour': []) must be skipped,
    not crash the whole catalog build (regression for IndexError on `[][0]`)."""
    write_json(
        f"{profile_tree['system_dir']}/Bambu Lab/filament/Bad Filament.json",
        {"name": "Bad Filament", "filament_colour": []},
    )
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()  # must not raise
    assert cat.is_built
    names = [f["name"] for f in cat.as_dict()["filament"]]
    assert "Bad Filament" in names
    bad = next(f for f in cat.as_dict()["filament"] if f["name"] == "Bad Filament")
    assert bad["filament_colour"] == "#FFFFFF"  # falls back to default, not IndexError


def test_unresolvable_parent_excluded_from_catalog(profile_tree):
    """A profile whose `inherits` parent can't be found must not be published as a
    near-empty preset - it must be dropped entirely, and the drop must be observable
    via skipped_count (regression for the degraded-profile-silently-served bug)."""
    write_json(
        f"{profile_tree['system_dir']}/Bambu Lab/filament/Orphan Filament.json",
        {"name": "Orphan Filament", "inherits": "Generic PLA @base"},
    )
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()  # must not raise
    assert cat.is_built
    names = [f["name"] for f in cat.as_dict()["filament"]]
    assert "Orphan Filament" not in names
    assert cat.skipped_count == 1


def test_skipped_count_persists_through_cache_roundtrip(profile_tree, tmp_path):
    write_json(
        f"{profile_tree['system_dir']}/Bambu Lab/filament/Orphan Filament.json",
        {"name": "Orphan Filament", "inherits": "Generic PLA @base"},
    )
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    cache_path = str(tmp_path / "cache.json")
    cat.save_to_cache(cache_path, "key1")
    loaded = ProfileCatalog.load_from_cache(
        cache_path, "key1", profile_tree["system_dir"], profile_tree["user_dir"],
    )
    assert loaded is not None
    assert loaded.skipped_count == cat.skipped_count == 1
