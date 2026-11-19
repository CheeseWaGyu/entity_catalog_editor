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


def render(selected, old, names, text, data):
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
        area = e.get("area")
        out.append("    area: " + (one_line(area) if area else ""))
        tags = e.get("tags") or []
        out.append("    tags: " + yaml.safe_dump(tags, default_flow_style=True, width=10**6).strip())
        for k, v in e.items():  # area/tags 以外の独自キーも保持
            if k in ("entity_id", "area", "tags"):
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
                {"id": i, "name": names.get(i, ""), "missing": i not in names}
                for i in ids
            ],
        }
    )


async def save(request):
    body = await request.json()
    selected = sorted({s for s in body.get("selected", []) if isinstance(s, str)})
    names = await fetch_names()
    text, data = load_raw()
    content = render(selected, old_entities(data), names, text, data)
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
