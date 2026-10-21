"""Omitted bool form fields must take their intended default on every FastAPI version.

Newer FastAPI reports an omitted bool form field as False whatever its default, so default-true
flags are declared ``Optional[str] = Form(None)`` and parsed with ``main._form_bool``.
"""
from app import main as m
from app.main import app


def test_form_bool_resolution():
    assert m._form_bool(None, True) is True
    assert m._form_bool(None, False) is False
    assert m._form_bool("", True) is True
    assert m._form_bool(True, False) is True and m._form_bool(False, True) is False
    for v in ("true", "True", "1", "yes", "on", " TRUE "):
        assert m._form_bool(v, False) is True
    for v in ("false", "False", "0", "no", "off"):
        assert m._form_bool(v, True) is False


def test_form_bool_rejects_garbage():
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        m._form_bool("maybe", True)
    assert e.value.status_code == 422


def test_no_bool_form_field_defaults_to_true_in_schema():
    schemas = app.openapi()["components"]["schemas"]
    bad = [
        f"{name}.{prop}"
        for name, sch in schemas.items() if name.startswith("Body_")
        for prop, spec in sch.get("properties", {}).items()
        if spec.get("type") == "boolean" and spec.get("default") is True
    ]
    assert not bad, f"bool Form fields with default=True break on newer FastAPI: {bad}"
