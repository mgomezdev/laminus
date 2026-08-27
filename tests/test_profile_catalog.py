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

def test_resolve_self_named_inherits_uses_system_profile(tmp_path):
    """A self-named `inherits` (parent name == own name) means 'the system profile of
    this name' - when a distinct system profile with that name exists, it must be
    used as the parent instead of being mistaken for circular self-inheritance."""
    system = tmp_path / "system"
    user = tmp_path / "user"
    system.mkdir()
    user.mkdir()
    (system / "Optimal.json").write_text(json.dumps({"name": "Optimal", "layer_height": 0.16, "speed": 60}))
    (user / "Optimal.json").write_text(json.dumps({"name": "Optimal", "inherits": "Optimal", "layer_height": 0.2}))
    result = resolve_inheritance(str(user / "Optimal.json"), _build_name_index([str(system), str(user)]))
    assert result["layer_height"] == 0.2
    assert result["speed"] == 60

def test_resolve_self_named_inherits_without_system_profile_raises_not_found(tmp_path):
    """Without a distinct system profile to shadow it, a self-named `inherits` is
    unresolvable - it must raise 'not found', not 'circular inheritance'."""
    child = tmp_path / "Optimal.json"
    child.write_text(json.dumps({"name": "Optimal", "inherits": "Optimal", "layer_height": 0.2}))
    with pytest.raises(ValueError, match="not found"):
        resolve_inheritance(str(child), _build_name_index([str(tmp_path)]))

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


def test_parent_with_slash_resolves_to_escaped_filename(profile_tree):
    """OrcaSlicer stores a profile named "A/B" in a file that escapes the slash,
    and is inconsistent about how: both "-" and " " ship in the same tree. A child
    inherits by name, so the index must try those spellings or the parent looks
    missing and the whole child is dropped from the catalog."""
    system_dir = profile_tree["system_dir"]
    write_json(
        f"{system_dir}/Bambu Lab/filament/Support For PLA-PETG @base.json",
        {"name": "Support For PLA/PETG @base", "filament_type": "PLA", "nozzle_temperature": 220},
    )
    write_json(
        f"{system_dir}/Bambu Lab/filament/Support For PA PET @base.json",
        {"name": "Support For PA/PET @base", "filament_type": "PA", "nozzle_temperature": 280},
    )
    write_json(
        f"{system_dir}/Bambu Lab/filament/Dash Child.json",
        {"name": "Dash Child", "inherits": "Support For PLA/PETG @base"},
    )
    write_json(
        f"{system_dir}/Bambu Lab/filament/Space Child.json",
        {"name": "Space Child", "inherits": "Support For PA/PET @base"},
    )

    cat = ProfileCatalog(system_dir=system_dir, user_dir=profile_tree["user_dir"])
    cat.build()

    by_name = {f["name"]: f for f in cat.as_dict()["filament"]}
    assert "Dash Child" in by_name, "slash-to-dash escaped parent was not resolved"
    assert "Space Child" in by_name, "slash-to-space escaped parent was not resolved"
    # Inherited values must actually be merged in, not just the child published bare.
    assert by_name["Dash Child"]["nozzle_temperature"] == 220
    assert by_name["Space Child"]["nozzle_temperature"] == 280


# ---------------------------------------------------------------------------
# Lazy resolution: build() validates inheritance and publishes the dictionary,
# but the fully-merged preset is materialised on demand.
# ---------------------------------------------------------------------------

def test_catalog_entries_do_not_carry_resolved_blob(profile_tree):
    """The merged dict must not be retained per entry - it was ~40% of an 87 MB
    cache file and is only ever needed for the presets a slice job actually uses."""
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    for entries in cat._catalog.values():
        for e in entries:
            assert "_resolved" not in e


def test_resolved_materialises_full_preset(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    standard = next(p for p in cat.as_dict()["process"] if "Standard" in p["name"])
    full = cat.resolved(standard["uuid"])
    assert full["layer_height"] == 0.2   # own value
    assert full["speed"] == 50           # inherited from FFF Settings
    assert "inherits" not in full


def test_resolved_unknown_uuid_raises(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    with pytest.raises(KeyError):
        cat.resolved("not-a-real-uuid")


def test_updated_vendor_base_rolls_into_user_profile(profile_tree):
    """The point of the whole design: a user profile that declares `inherits` picks up
    an updated vendor base on the next catalog build, with no reflattening step."""
    write_json(
        f"{profile_tree['user_dir']}/default/process/My Fast @Custom.json",
        {"name": "My Fast @Custom", "inherits": "FFF Settings", "layer_height": 0.28},
    )
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    mine = next(p for p in cat.as_dict()["process"] if p["name"] == "My Fast @Custom")
    assert cat.resolved(mine["uuid"])["speed"] == 50

    # Vendor ships a new base value; user profile is untouched on disk.
    write_json(
        f"{profile_tree['system_dir']}/Bambu Lab/process/FFF Settings.json",
        {"name": "FFF Settings", "layer_height": 0.3, "speed": 80},
    )
    cat2 = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat2.build()
    mine2 = next(p for p in cat2.as_dict()["process"] if p["name"] == "My Fast @Custom")
    full = cat2.resolved(mine2["uuid"])
    assert full["speed"] == 80, "updated vendor base did not roll through"
    assert full["layer_height"] == 0.28, "user override must still win"


def test_flattened_profile_does_not_receive_vendor_updates(profile_tree):
    """Counterpart to the test above - documents why a flattened preset is a fork."""
    write_json(
        f"{profile_tree['user_dir']}/default/process/Frozen @Custom.json",
        {"name": "Frozen @Custom", "layer_height": 0.28, "speed": 50},
    )
    write_json(
        f"{profile_tree['system_dir']}/Bambu Lab/process/FFF Settings.json",
        {"name": "FFF Settings", "layer_height": 0.3, "speed": 80},
    )
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    frozen = next(p for p in cat.as_dict()["process"] if p["name"] == "Frozen @Custom")
    assert cat.resolved(frozen["uuid"])["speed"] == 50


def test_resolved_works_after_cache_roundtrip(profile_tree, tmp_path):
    """A cache hit skips build() entirely, so lazy resolution must still find the file
    on disk - the persisted name index has to survive the roundtrip."""
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    cache_path = str(tmp_path / "cache.json")
    cat.save_to_cache(cache_path, "key1")
    loaded = ProfileCatalog.load_from_cache(
        cache_path, "key1", profile_tree["system_dir"], profile_tree["user_dir"],
    )
    assert loaded is not None
    standard = next(p for p in loaded.as_dict()["process"] if "Standard" in p["name"])
    assert loaded.resolved(standard["uuid"])["speed"] == 50
    assert loaded.get_by_uuid(standard["uuid"]) is not None  # by_uuid rebuilt, not persisted


def test_cache_file_excludes_resolved_blobs(profile_tree, tmp_path):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    cache_path = str(tmp_path / "cache.json")
    cat.save_to_cache(cache_path, "key1")
    raw = json.loads(open(cache_path, encoding="utf-8").read())
    assert "by_uuid" not in raw
    for entries in raw["catalog"].values():
        for e in entries:
            assert "_resolved" not in e


def test_broken_reports_reason_and_survives_cache(profile_tree, tmp_path):
    """An OrcaSlicer update that renames a vendor base shows up here."""
    write_json(
        f"{profile_tree['user_dir']}/default/filament/Mine @Custom.json",
        {"name": "Mine @Custom", "inherits": "Vendor Base That Went Away"},
    )
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    assert cat.skipped_count == 1
    (entry,) = cat.broken
    assert entry["name"] == "Mine @Custom"
    assert entry["source"] == "user"
    assert entry["type"] == "filament"
    assert "Vendor Base That Went Away" in entry["reason"]

    cache_path = str(tmp_path / "cache.json")
    cat.save_to_cache(cache_path, "key1")
    loaded = ProfileCatalog.load_from_cache(
        cache_path, "key1", profile_tree["system_dir"], profile_tree["user_dir"],
    )
    assert loaded.broken == cat.broken
