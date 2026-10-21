"""Omitted bool form fields must take their intended default on every FastAPI version.

Newer FastAPI reports an omitted bool form field as False whatever its default, so default-true
flags are declared ``Optional[str] = Form(None)`` and parsed with ``main._form_default_true``.
"""
from app import main as m
from app.main import app


def test_form_default_true_resolution():
    assert m._form_default_true(None) is True
    assert m._form_default_true("") is False
    assert m._form_default_true(True) is True and m._form_default_true(False) is False
    for v in ("true", "True", "1", "yes", "on"):
        assert m._form_default_true(v) is True
    for v in ("false", "False", "0", "no", "off"):
        assert m._form_default_true(v) is False


def test_no_bool_form_field_defaults_to_true_in_schema():
    schemas = app.openapi()["components"]["schemas"]
    bad = [
        f"{name}.{prop}"
        for name, sch in schemas.items() if name.startswith("Body_")
        for prop, spec in sch.get("properties", {}).items()
        if spec.get("type") == "boolean" and spec.get("default") is True
    ]
    assert not bad, f"bool Form fields with default=True break on newer FastAPI: {bad}"
