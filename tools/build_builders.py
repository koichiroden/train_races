# -*- coding: utf-8 -*-
"""
tools/src/*.html (ビルダーのテンプレート) に、共通CSS/JS と駅データ・系統プリセット
(tools/config_builder.html から抽出)を埋め込んで、1ファイルで動く
tools/<name>_builder.html を生成する。

    python3 tools/build_builders.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"


def extract_from_config_builder():
    h = (ROOT / "config_builder.html").read_text(encoding="utf-8")
    m = re.search(r'<script id="station-data" type="application/json">(.*?)</script>', h, re.S)
    station_json = m.group(1).strip()
    i = h.index("const MANUAL_PRESETS = {")
    depth, j = 0, h.index("{", i)
    for k in range(j, len(h)):
        if h[k] == "{":
            depth += 1
        elif h[k] == "}":
            depth -= 1
            if depth == 0:
                break
    presets = h[i:k + 1] + ";"
    return station_json, presets


def overtake_sample_state():
    """configs/overtake_keikyu_shinagawa_yokohama.json をビルダーの初期状態に変換する。"""
    import json
    c = json.loads((ROOT.parent / "configs" / "overtake_keikyu_shinagawa_yokohama.json").read_text(encoding="utf-8"))

    def fmt_p(v):
        if v is None:
            return ""
        if isinstance(v, dict):
            return f"{v['arr']}/{v['dep']}"
        if isinstance(v, list):
            return "/".join(v)
        return str(v)

    rows = {}
    n = len(c["stations"])
    for i, s in enumerate(c["stations"]):
        v = c["local"]["times"].get(s["name"])
        arr = dep = ""
        if isinstance(v, dict):
            arr, dep = v["arr"], v["dep"]
        elif v:
            if i == n - 1:
                arr = v
            else:
                dep = v
        rows[s["name"]] = {"popup": bool(s.get("popup")), "arr": arr, "dep": dep,
                           "p": [fmt_p(p["times"].get(s["name"])) for p in c["passers"]]}
    hexc = lambda rgb: "#" + "".join(f"{v:02x}" for v in rgb)
    st = {
        "slug": c["slug"], "title_line1": c["title_line1"], "title_line2": c["title_line2"],
        "video_duration_sec": c.get("video_duration_sec", 40),
        "default_dwell_sec": c["local"].get("default_dwell_sec", 30),
        "segments": [{"line": c["stations"][0]["line"], "preset": 0, "from": c["stations"][0]["name"],
                      "to": c["stations"][-1]["name"]}],
        "local": {"name": c["local"]["name"], "badge": c["local"]["badge"], "color": hexc(c["local"]["color"])},
        "passers": [{"name": p["name"], "badge": p["badge"], "color": hexc(p["color"])} for p in c["passers"]],
        "rows": rows,
    }
    return json.dumps(st, ensure_ascii=False)


def main():
    station_json, presets = extract_from_config_builder()
    css = (SRC / "shared.css").read_text(encoding="utf-8")
    js = (SRC / "shared.js").read_text(encoding="utf-8").replace("/*MANUAL_PRESETS*/", presets)
    for name in ("rush", "fare", "overtake"):
        p = SRC / f"{name}.html"
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        t = t.replace("/*SHARED_CSS*/", css).replace("/*SHARED_JS*/", js).replace("/*STATION_DATA*/", station_json)
        if "/*SAMPLE_STATE*/" in t:
            t = t.replace("/*SAMPLE_STATE*/", overtake_sample_state())
        out = ROOT / f"{name}_builder.html"
        out.write_text(t, encoding="utf-8")
        print("wrote", out, f"{len(t) // 1024}KB")


if __name__ == "__main__":
    main()
