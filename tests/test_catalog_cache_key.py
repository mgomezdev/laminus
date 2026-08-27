"""Cache-key coverage: any preset change must void the cached catalog.

The key gates whether the catalog is rebuilt (and inheritance re-verified). If a change
to the vendor tree does not move the key, laminus serves resolved presets from a stale
cache indefinitely - which is exactly the failure the vendor bundle signature exists to
prevent, since OrcaSlicer updates vendor bundles independently of the app version.
"""
import json
import os
import pytest

from tests.conftest import write_json


@pytest.fixture
def key_env(tmp_path, monkeypatch):
    system = tmp_path / "system"
    user = tmp_path / "user"
    (system / "Elegoo" / "machine").mkdir(parents=True)
    (user / "default" / "machine").mkdir(parents=True)
    (system / "Elegoo.json").write_text(
        json.dumps({"name": "Elegoo", "version": "02.04.00.06"}), encoding="utf-8"
    )
    write_json(
        str(user / "default" / "machine" / "Mine.json"),
        {"name": "Mine", "inherits": "Elegoo Centauri Carbon 0.4 nozzle"},
    )

    from app import main
    monkeypatch.setattr(main, "SYSTEM_PROFILES_DIR", str(system))
    monkeypatch.setattr(main, "USER_CONFIG_DIR", str(user))
    monkeypatch.setenv("ORCA_VERSION", "2.4.2")
    return {"main": main, "system": system, "user": user}


def test_key_is_stable_when_nothing_changes(key_env):
    main = key_env["main"]
    assert main._catalog_cache_key() == main._catalog_cache_key()


def test_orca_version_bump_changes_key(key_env, monkeypatch):
    main = key_env["main"]
    before = main._catalog_cache_key()
    monkeypatch.setenv("ORCA_VERSION", "2.5.0")
    assert main._catalog_cache_key() != before


def test_vendor_bundle_version_bump_changes_key(key_env):
    """OrcaSlicer updates vendor bundles without changing ORCA_VERSION - notably when
    SYSTEM_PROFILES_DIR is bind-mounted from a host install that self-updates."""
    main, system = key_env["main"], key_env["system"]
    before = main._catalog_cache_key()
    (system / "Elegoo.json").write_text(
        json.dumps({"name": "Elegoo", "version": "02.05.00.00"}), encoding="utf-8"
    )
    assert main._catalog_cache_key() != before, "vendor bundle update did not void the cache"


def test_new_vendor_bundle_changes_key(key_env):
    main, system = key_env["main"], key_env["system"]
    before = main._catalog_cache_key()
    (system / "BBL.json").write_text(
        json.dumps({"name": "BBL", "version": "01.00.00.00"}), encoding="utf-8"
    )
    assert main._catalog_cache_key() != before


def test_user_profile_edit_changes_key(key_env):
    main, user = key_env["main"], key_env["user"]
    before = main._catalog_cache_key()
    write_json(
        str(user / "default" / "machine" / "Mine.json"),
        {"name": "Mine", "inherits": "Elegoo Centauri Carbon 0.4 nozzle", "max_layer_height": ["0.4"]},
    )
    assert main._catalog_cache_key() != before


def test_new_user_profile_changes_key(key_env):
    main, user = key_env["main"], key_env["user"]
    before = main._catalog_cache_key()
    write_json(str(user / "default" / "machine" / "Another.json"), {"name": "Another"})
    assert main._catalog_cache_key() != before


def test_missing_system_dir_does_not_raise(key_env, monkeypatch):
    main = key_env["main"]
    monkeypatch.setattr(main, "SYSTEM_PROFILES_DIR", "/nonexistent/profiles")
    assert main._catalog_cache_key()  # returns a key rather than blowing up
