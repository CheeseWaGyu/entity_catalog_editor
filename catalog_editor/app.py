import json
import os
import re
import shutil

import yaml
from aiohttp import ClientSession, web

OPTS = {}
if os.path.exists("/data/options.json"):
    with open("/data/options.json", encoding="utf-8") as f:
        OPTS = json.load(f)

PATH = OPTS.get(
    "catalog_path",
    "/addon_configs/959b8912_muse_automation_mcp/entity_catalog.yaml",
)
HA = "http://supervisor/core/api"
HEADERS = {"Authorization": "Bearer " + os.environ.get("SUPERVISOR_TOKEN", "")}
WARN_DOMAINS = ["lock", "cover", "alarm_control_panel"]
# 履歴 level が full のとき特に注意が要るドメイン（位置情報などの生ログが渡る）
HISTORY_FULL_WARN_DOMAINS = ["device_tracker", "person", "camera"]
# summary でも滞在時間・遷移時刻から行動パターンが推測できるドメイン
HISTORY_PRIVACY_WARN_DOMAINS = ["device_tracker", "person"]
HISTORY_LEVELS = ["off", "summary", "full"]
DEFAULT_HEADER = "# entity_catalog.yaml (Entity Catalog Editor で生成)\n"
MAX_DESCRIPTION_LEN = 1000  # ha-muse-automation-mcp 側の上限と一致させる
# entity_id の厳格な形式（HA の規則: 小文字ドメイン . 小文字 object_id）
ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")
MAX_SELECTED = 1000  # 1回の保存で扱う entity 数の上限（descriptions / history の件数上限も同じ）
MAX_BODY_SIZE = 1024 * 1024  # リクエストボディ上限 1MB（aiohttp client_max_size）


def is_valid_entity_id(value):
    return isinstance(value, str) and bool(ENTITY_ID_RE.match(value))


class SaveValidationError(ValueError):
    """保存リクエストの検証エラー。invalid に問題のある ID を入れる。"""

    def __init__(self, message, invalid=None):
        super().__init__(message)
        self.invalid = invalid or []


def validate_save_payload(body, known_ids):
    """保存ペイロードを検証し、正規化済みの (selected, descriptions, history_levels) を返す。

    selected / descriptions のキー / history のキーはすべて、形式が正しく、
    かつ HA に実在するか既存カタログに載っている ID でなければならない。
    問題があれば SaveValidationError（HTTP 400 相当）を送出する（黙って落とさない）。
    """
    if not isinstance(body, dict):
        raise SaveValidationError("リクエストボディは JSON オブジェクトです")
    raw_selected = body.get("selected", [])
    if not isinstance(raw_selected, list):
        raise SaveValidationError("selected はリストです")
    invalid = []
    selected = []
    for s in raw_selected:
        if not is_valid_entity_id(s):
            invalid.append(s if isinstance(s, str) else repr(s))
        elif s not in known_ids:
            invalid.append(s)
        elif s not in selected:
            selected.append(s)
    if invalid:
        raise SaveValidationError("selected に不正な entity_id があります", invalid)
    if len(selected) > MAX_SELECTED:
        raise SaveValidationError(f"selected は {MAX_SELECTED} 件までです")
    selected.sort()

    def clean_map(raw, name):
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise SaveValidationError(f"{name} はオブジェクトです")
        if len(raw) > MAX_SELECTED:
            raise SaveValidationError(f"{name} は {MAX_SELECTED} 件までです")
        bad = [k for k in raw if not is_valid_entity_id(k) or k not in known_ids]
        if bad:
            raise SaveValidationError(f"{name} に不正な entity_id があります", bad)
        return raw

    descriptions = {
        k: clean_description(v)
        for k, v in clean_map(body.get("descriptions"), "descriptions").items()
    }
    history_levels = {
        k: clean_history_level(v)
        for k, v in clean_map(body.get("history"), "history").items()
    }
    return selected, descriptions, history_levels


def clean_history_level(value):
    """history.level を正規化する（不正値は off に倒す。サーバー側の解析と一致）。"""
    return value if value in HISTORY_LEVELS else "off"


def entity_history_level(entry):
    hist = entry.get("history")
    if isinstance(hist, dict):
        return clean_history_level(hist.get("level"))
    return "off"


def clean_description(value):
    """description を値として正規化する（文字列以外は空、空白除去、上限で切り詰め）。"""
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_DESCRIPTION_LEN]


def load_raw():
    try:
        with open(PATH, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return "", {}
    return text, (yaml.safe_load(text) or {})


def old_entities(data):
    return {
        e["entity_id"]: e
        for e in data.get("entities", []) or []
        if isinstance(e, dict) and "entity_id" in e
    }


async def fetch_names():
    async with ClientSession() as s:
        async with s.get(HA + "/states", headers=HEADERS) as r:
            r.raise_for_status()
            states = await r.json()
    return {
        st["entity_id"]: st.get("attributes", {}).get("friendly_name", "")
        for st in states
    }


def one_line(value):
    return yaml.safe_dump(value, default_flow_style=True, width=10**6).split("\n")[0]


def render(selected, old, names, text, data, descriptions=None, history_levels=None):
    # 防御: render には検証済み ID しか渡さない（万一のときはここでも止める）
    for eid in selected:
        if not is_valid_entity_id(eid):
            raise SaveValidationError("render に未検証の entity_id が渡されました", [eid])
    # 既存ファイルの先頭コメントを引き継ぐ
    header = []
    for line in text.splitlines():
        if line.startswith("entities:"):
            break
        header.append(line)
    out = header or [DEFAULT_HEADER.rstrip()]
    out.append("entities:")
    for eid in selected:
        e = old.get(eid, {})
        out.append(f"  - entity_id: {eid}")
        name = re.sub(r"\s+", " ", names.get(eid) or "").strip()
        if name:
            out.append(f"    # {name}")
        # 説明はコメントではなく description の値として書く（get_entity_catalog で Muse に渡る）。
        # UI から送られた値を優先し、無ければ既存ファイルの値を引き継ぐ。
        desc = clean_description((descriptions or {}).get(eid, e.get("description")))
        if desc:
            dumped = yaml.safe_dump(
                {"description": desc}, allow_unicode=True, width=10**6
            ).rstrip()
            for ln in dumped.splitlines():
                out.append("    " + ln)
        area = e.get("area")
        out.append("    area: " + (one_line(area) if area else ""))
        tags = e.get("tags") or []
        out.append("    tags: " + yaml.safe_dump(tags, default_flow_style=True, width=10**6).strip())
        # 履歴（証拠レイヤー）: level が off 以外のときだけ history: を値として書く。
        # UI から送られた level を優先し、無ければ既存ファイルの値を引き継ぐ。
        # 既存の history.attributes（許可属性リスト）はそのまま保持する。
        old_hist = e.get("history") if isinstance(e.get("history"), dict) else {}
        level = clean_history_level((history_levels or {}).get(eid, old_hist.get("level")))
        if level != "off":
            out.append("    history:")
            out.append(f"      level: {level}")
            attrs = old_hist.get("attributes")
            if isinstance(attrs, list) and attrs:
                out.append("      attributes: " + yaml.safe_dump(attrs, default_flow_style=True, width=10**6).strip())
        for k, v in e.items():  # area/tags/description/history 以外の独自キーも保持
            if k in ("entity_id", "area", "tags", "description", "history"):
                continue
            for ln in yaml.safe_dump({k: v}, allow_unicode=True).splitlines():
                out.append("    " + ln)
    extras = {k: v for k, v in data.items() if k != "entities"}
    if extras:
        out.append(yaml.safe_dump(extras, allow_unicode=True).rstrip())
    return "\n".join(out) + "\n"


async def index(request):
    with open("/index.html", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")


async def entities(request):
    names = await fetch_names()
    _, data = load_raw()
    old = old_entities(data)
    ids = sorted(set(names) | set(old))
    return web.json_response(
        {
            "path": PATH,
            "warn_domains": WARN_DOMAINS,
            "history_full_warn_domains": HISTORY_FULL_WARN_DOMAINS,
            "history_privacy_warn_domains": HISTORY_PRIVACY_WARN_DOMAINS,
            "selected": sorted(old),
            "entities": [
                {
                    "id": i,
                    "name": names.get(i, ""),
                    "missing": i not in names,
                    "description": clean_description(old.get(i, {}).get("description")),
                    "history": entity_history_level(old.get(i, {})),
                }
                for i in ids
            ],
        }
    )


async def save(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "リクエストボディが JSON ではありません"},
                                 status=400)
    names = await fetch_names()
    text, data = load_raw()
    known_ids = set(names) | set(old_entities(data))
    try:
        selected, descriptions, history_levels = validate_save_payload(body, known_ids)
    except SaveValidationError as e:
        return web.json_response(
            {"ok": False, "error": str(e), "invalid": e.invalid}, status=400)
    content = render(selected, old_entities(data), names, text, data, descriptions, history_levels)
    yaml.safe_load(content)  # 書く前に壊れていないか検証
    if os.path.exists(PATH):
        shutil.copy2(PATH, PATH + ".bak")
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, PATH)
    return web.json_response({"ok": True, "count": len(selected)})


app = web.Application(client_max_size=MAX_BODY_SIZE)
app.add_routes([web.get("/", index), web.get("/api/entities", entities), web.post("/api/save", save)])

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8099)
