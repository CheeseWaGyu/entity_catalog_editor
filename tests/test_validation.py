"""Entity Catalog Editor の保存時検証と render のテスト。

aiohttp が無い環境でも pytest + PyYAML だけで回せるよう、app.py を
import する前に aiohttp モジュールを sys.modules にスタブとして差し込む
（app.py はモジュールレベルで `from aiohttp import ClientSession, web` し、
末尾で web.Application を生成するため、その生成に必要な最小限を用意する）。
"""
import asyncio
import importlib.util
import os
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

    def _json_response(data=None, status=200, **kwargs):
        # ハンドラのテストで status と本文を検証できるよう、最小限の応答オブジェクトを返す
        return types.SimpleNamespace(status=status, data=data)

    web.json_response = _json_response
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


# ------------------------------------------------------------------
# catalog_path の許可範囲（v0.2.2）: 検証そのものと、保存失敗時の原子性。
# ハンドラ（app.save）を直接呼ぶため、fetch_names はスタブに差し替え、
# PATH / ALLOWED_ROOT は tmp ディレクトリへ向ける。
# ------------------------------------------------------------------

INITIAL_CATALOG = (
    "entities:\n"
    "  - entity_id: sensor.living_temp\n"
    "    area: リビング\n"
    "    tags: []\n"
)


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def editor_env(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "959b8912_muse_automation_mcp"
    cfg_dir.mkdir()
    catalog = cfg_dir / "entity_catalog.yaml"
    catalog.write_text(INITIAL_CATALOG, encoding="utf-8")
    monkeypatch.setattr(app, "PATH", str(catalog))
    monkeypatch.setattr(app, "ALLOWED_ROOT", str(tmp_path))

    async def fake_fetch_names():
        return {"sensor.living_temp": "Living Temp"}

    monkeypatch.setattr(app, "fetch_names", fake_fetch_names)
    return catalog


def test_catalog_path_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ALLOWED_ROOT", str(tmp_path))
    good = tmp_path / "959b8912_muse_automation_mcp" / "entity_catalog.yaml"
    assert app.catalog_path_error(str(good)) is None
    bare = tmp_path / "muse_automation_mcp" / "entity_catalog.yaml"
    assert app.catalog_path_error(str(bare)) is None
    # ファイル名が違う
    assert app.catalog_path_error(
        str(tmp_path / "959b8912_muse_automation_mcp" / "other.yaml")) is not None
    # 直上のディレクトリが MCP の設定ディレクトリでない
    assert app.catalog_path_error(
        str(tmp_path / "some_other_addon" / "entity_catalog.yaml")) is not None
    # 許可ルートの外
    monkeypatch.setattr(app, "ALLOWED_ROOT", str(tmp_path / "root"))
    assert app.catalog_path_error(str(good)) is not None


def test_save_success_writes_catalog(editor_env):
    catalog = editor_env
    resp = asyncio.run(app.save(_FakeRequest(
        {"selected": ["sensor.living_temp"],
         "descriptions": {"sensor.living_temp": "リビングの室温"}})))
    assert resp.status == 200 and resp.data["ok"] is True
    parsed = yaml.safe_load(catalog.read_text(encoding="utf-8"))
    assert parsed["entities"][0]["description"] == "リビングの室温"
    assert Path(str(catalog) + ".bak").exists()  # 差し替え前にバックアップ


def test_save_invalid_payload_writes_nothing(editor_env):
    catalog = editor_env
    before = catalog.read_bytes()
    resp = asyncio.run(app.save(_FakeRequest({"selected": ["sensor.unknown_thing"]})))
    assert resp.status == 400 and resp.data["ok"] is False
    assert catalog.read_bytes() == before  # 既存カタログはバイト単位で不変
    assert not Path(str(catalog) + ".tmp").exists()


def test_save_replace_failure_keeps_original(editor_env, monkeypatch):
    catalog = editor_env
    before = catalog.read_bytes()

    def failing_replace(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        asyncio.run(app.save(_FakeRequest({"selected": ["sensor.living_temp"]})))
    assert catalog.read_bytes() == before  # 差し替え失敗でも原文は無傷
    assert not Path(str(catalog) + ".tmp").exists()  # 一時ファイルは掃除される


def test_save_disallowed_path_refused(editor_env, tmp_path, monkeypatch):
    # 許可範囲外（直上が MCP の設定ディレクトリでない）へは書かない
    elsewhere_dir = tmp_path / "some_other_addon"
    elsewhere_dir.mkdir()
    target = elsewhere_dir / "entity_catalog.yaml"
    target.write_text(INITIAL_CATALOG, encoding="utf-8")
    monkeypatch.setattr(app, "PATH", str(target))
    before = target.read_bytes()
    resp = asyncio.run(app.save(_FakeRequest({"selected": ["sensor.living_temp"]})))
    assert resp.status == 500 and resp.data["ok"] is False
    assert "catalog_path" in resp.data["error"]
    assert target.read_bytes() == before


def test_write_catalog_disallowed_path_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ALLOWED_ROOT", str(tmp_path))
    monkeypatch.setattr(app, "PATH", str(tmp_path / "evil" / "entity_catalog.yaml"))
    with pytest.raises(app.CatalogPathError):
        app.write_catalog("entities: []\n")
    assert not (tmp_path / "evil").exists()  # ディレクトリすら作らない
