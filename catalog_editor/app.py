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
DEFAULT_HEADER = "# entity_catalog.yaml (Entity Catalog Editor で生成)\n"
MAX_DESCRIPTION_LEN = 1000  # ha-muse-automation-mcp 側の上限と一致させる


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


def render(selected, old, names, text, data, descriptions=None):
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
        for k, v in e.items():  # area/tags/description 以外の独自キーも保持
            if k in ("entity_id", "area", "tags", "description"):
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
            "selected": sorted(old),
            "entities": [
                {
                    "id": i,
                    "name": names.get(i, ""),
                    "missing": i not in names,
                    "description": clean_description(old.get(i, {}).get("description")),
                }
                for i in ids
            ],
        }
    )


async def save(request):
    body = await request.json()
    selected = sorted({s for s in body.get("selected", []) if isinstance(s, str)})
    raw_desc = body.get("descriptions") or {}
    descriptions = {
        k: clean_description(v) for k, v in raw_desc.items() if isinstance(k, str)
    }
    names = await fetch_names()
    text, data = load_raw()
    content = render(selected, old_entities(data), names, text, data, descriptions)
    yaml.safe_load(content)  # 書く前に壊れていないか検証
    if os.path.exists(PATH):
        shutil.copy2(PATH, PATH + ".bak")
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, PATH)
    return web.json_response({"ok": True, "count": len(selected)})


app = web.Application()
app.add_routes([web.get("/", index), web.get("/api/entities", entities), web.post("/api/save", save)])

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8099)
