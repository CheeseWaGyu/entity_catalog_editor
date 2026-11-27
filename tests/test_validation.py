"""Entity Catalog Editor の保存時検証と render のテスト。

aiohttp が無い環境でも pytest + PyYAML だけで回せるよう、app.py を
import する前に aiohttp モジュールを sys.modules にスタブとして差し込む
（app.py はモジュールレベルで `from aiohttp import ClientSession, web` し、
末尾で web.Application を生成するため、その生成に必要な最小限を用意する）。
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml


def _install_aiohttp_stub():
    if "aiohttp" in sys.modules:
        return
    aiohttp = types.ModuleType("aiohttp")

    class _ClientSession:  # fetch_names 用（テストでは呼ばない）
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    web = types.ModuleType("aiohttp.web")

    class _Application:
        def __init__(self, *args, **kwargs):
            self.client_max_size = kwargs.get("client_max_size")

        def add_routes(self, routes):
            self.routes = list(routes)

    web.Application = _Application
    web.Response = object
    web.json_response = lambda *a, **k: None
    web.get = lambda path, handler: ("GET", path, handler)
    web.post = lambda path, handler: ("POST", path, handler)
    web.run_app = lambda *a, **k: None
    aiohttp.ClientSession = _ClientSession
    aiohttp.web = web
    sys.modules["aiohttp"] = aiohttp
    sys.modules["aiohttp.web"] = web


_install_aiohttp_stub()

_APP_PATH = Path(__file__).resolve().parent.parent / "catalog_editor" / "app.py"
_spec = importlib.util.spec_from_file_location("catalog_editor_app", _APP_PATH)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

KNOWN = {"sensor.living_temp", "binary_sensor.living_presence", "light.living_ceiling"}


def test_bad_format_id_rejected():
    for bad in ("Sensor.Living_Temp", "sensor.", ".living_temp", "no-dot",
                "sensor.living temp", "sensor.living-temp", "sensor.living_temp\n  evil: 1",
                123, None):
        with pytest.raises(app.SaveValidationError):
            app.validate_save_payload({"selected": [bad]}, KNOWN)


def test_unknown_id_rejected():
    with pytest.raises(app.SaveValidationError) as ei:
        app.validate_save_payload({"selected": ["sensor.unknown_thing"]}, KNOWN)
    assert "sensor.unknown_thing" in ei.value.invalid


def test_known_id_accepted_and_normalized():
    selected, descriptions, history = app.validate_save_payload(
        {
            "selected": ["sensor.living_temp", "sensor.living_temp",
                         "binary_sensor.living_presence"],
            "descriptions": {"sensor.living_temp": "  リビングの室温  "},
            "history": {"sensor.living_temp": "full",
                        "binary_sensor.living_presence": "banana"},
        },
        KNOWN,
    )
    assert selected == ["binary_sensor.living_presence", "sensor.living_temp"]  # 重複除去＋ソート
    assert descriptions == {"sensor.living_temp": "リビングの室温"}
    assert history["sensor.living_temp"] == "full"
    assert history["binary_sensor.living_presence"] == "off"  # 不正な level は off に倒す


def test_descriptions_and_history_keys_validated():
    with pytest.raises(app.SaveValidationError):
        app.validate_save_payload(
            {"selected": [], "descriptions": {"sensor.unknown_thing": "x"}}, KNOWN)
    with pytest.raises(app.SaveValidationError):
        app.validate_save_payload(
            {"selected": [], "history": {"Bad Format": "full"}}, KNOWN)
    with pytest.raises(app.SaveValidationError):
        app.validate_save_payload(
            {"selected": [], "history": {"sensor.unknown_thing": "full"}}, KNOWN)


def test_count_limits():
    many = {f"sensor.e{i}" for i in range(app.MAX_SELECTED + 1)}
    with pytest.raises(app.SaveValidationError):
        app.validate_save_payload({"selected": sorted(many)}, many)
    with pytest.raises(app.SaveValidationError):
        app.validate_save_payload(
            {"selected": [], "descriptions": {k: "x" for k in many}}, many)
    with pytest.raises(app.SaveValidationError):
        app.validate_save_payload(
            {"selected": [], "history": {k: "full" for k in many}}, many)
    # 上限ちょうどは通る
    just = {f"sensor.e{i}" for i in range(app.MAX_SELECTED)}
    selected, _, _ = app.validate_save_payload({"selected": sorted(just)}, just)
    assert len(selected) == app.MAX_SELECTED


def test_render_round_trip_preserves_history_attributes_and_unknown_keys():
    data = {
        "entities": [
            {
                "entity_id": "sensor.living_temp",
                "description": "リビングの室温",
                "area": "リビング",
                "tags": ["climate"],
                "history": {"level": "full", "attributes": ["unit_of_measurement"]},
                "custom_key": "keep-me",
            }
        ],
        "extra_top": {"nested": 1},
    }
    old = app.old_entities(data)
    text = "# my header\nentities:\n  - entity_id: sensor.living_temp\n"
    out = app.render(["sensor.living_temp"], old,
                     {"sensor.living_temp": "Living Temp"}, text, data)
    parsed = yaml.safe_load(out)
    (entry,) = parsed["entities"]
    assert entry["history"] == {"level": "full",
                                 "attributes": ["unit_of_measurement"]}
    assert entry["custom_key"] == "keep-me"
    assert entry["description"] == "リビングの室温"
    assert entry["area"] == "リビング"
    assert entry["tags"] == ["climate"]
    assert parsed["extra_top"] == {"nested": 1}
    assert out.startswith("# my header\n")


def test_render_history_level_override_and_off_omission():
    data = {"entities": [{"entity_id": "sensor.living_temp",
                          "history": {"level": "full", "attributes": ["a"]}}]}
    old = app.old_entities(data)
    out = app.render(["sensor.living_temp"], old, {}, "", data,
                     history_levels={"sensor.living_temp": "summary"})
    (entry,) = yaml.safe_load(out)["entities"]
    assert entry["history"] == {"level": "summary", "attributes": ["a"]}
    out = app.render(["sensor.living_temp"], old, {}, "", data,
                     history_levels={"sensor.living_temp": "off"})
    (entry,) = yaml.safe_load(out)["entities"]
    assert "history" not in entry  # off では history 自体を書かない


def test_render_rejects_unvalidated_id():
    with pytest.raises(app.SaveValidationError):
        app.render(["sensor.living_temp; rm -rf"], {}, {}, "", {"entities": []})
