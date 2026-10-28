# -*- coding: utf-8 -*-
"""
ネタ②「競合路線、運賃が安いのはどっち?」動画。

同じ発着駅を結ぶ2〜4本の競合ルートについて、
  1) 地図フェーズ : 車両が同じペース(km基準)で進み、運賃を入れた途中駅を
                    通過すると駅の近くに「¥xxx」がポップアップ。下の棒グラフも
                    その駅までの運賃に伸びていく。
  2) 距離グラフ   : 横軸=距離(km)・縦軸=運賃 の階段グラフを描き、
                    「約10km / 20km / 30km …」の同じ距離で運賃を並べて比べる。
                    (config の "show_distance_chart": false で省略可)
  3) 最終運賃     : 最終目的地までの運賃を ¥0 からカウントアップで表示し、
                    差額と 1kmあたりの運賃を出す。

config 例は configs/fare_shinjuku_fujisawa.json を参照。
  "video_type": "fare"
  "fare_label": "IC運賃"
  "compare_km": [10, 20, 30]       # 省略時は自動
  "routes": [ {"key","name","short_name","color",
               "stations": [{"line": "...", "name": "新宿", "fare": 0},
                            {"line": "...", "name": "登戸", "fare": 270, "popup": true}, ...]} ]
"fare" を書いた駅だけがポップアップ・グラフの対象になる(始発駅は0扱い)。
駅に "km"(始発駅からの距離。営業キロなど)を書くと、距離グラフ・同距離比較・
1kmあたり運賃にその値を使う(省略時は線路データ上の距離)。
終着駅の "fare" は必須。
"""
import math

from PIL import ImageDraw

from .common import (
    CANVAS_W, CANVAS_H, font, build_base_map, build_geometry, PolyPath, draw_trail,
    draw_train, draw_chip, pop_scale, ease_out_cubic, ease_in_out, FrameWriter,
    write_text_script, load_icon,
)
from .geo import project
from .route_builder import load_line_geoms

MAP_BOX = (60, 470, 1020, 1090)
PALETTE = [(0, 110, 235), (232, 70, 40), (31, 158, 107), (139, 92, 246)]


def yen(v):
    return f"¥{int(round(v)):,}"


class FareRoute:
    def __init__(self, cfg, route, idx):
        self.cfg = cfg
        self.key = cfg.get("key", f"route{idx}")
        self.name = cfg.get("name", self.key)
        self.short = cfg.get("short_name", self.name)
        self.color = tuple(cfg.get("color") or PALETTE[idx % len(PALETTE)])
        self.badge = cfg.get("badge")
        self.icon = load_icon(cfg.get("icon_path"), int(cfg.get("icon_width", 86)))
        self.route = route
        self.path = PolyPath(route)
        sts = route["stations"]
        k0 = sts[0]["cum_km"]
        self.points = []   # (km_from_start, fare, station)
        for i, s in enumerate(sts):
            fare = s.get("fare")
            if i == 0 and fare is None:
                fare = 0
            if fare is None or fare == "":
                continue
            self.points.append((s["cum_km"] - k0, float(fare), s))
        if sts[-1].get("fare") in (None, ""):
            raise ValueError(f"[{self.name}] 終着駅 {sts[-1]['name']} の fare(運賃)は必須です")
        self.k0 = k0
        self.total_km = sts[-1]["cum_km"] - k0
        self.final_fare = self.points[-1][1]

        # 表示用の距離(グラフ・同距離比較・1kmあたり運賃に使う)。
        # 駅に "km"(始発駅からの距離。営業キロなど)が書いてあればそれを使い、
        # 無い駅は前後の手入力値の間を線路上の距離の比で補う(手入力が無ければ線路上の距離)。
        geo = [s["cum_km"] - k0 for s in sts]
        man = [None if s.get("km") in (None, "") else float(s["km"]) for s in sts]
        man[0] = 0.0 if man[0] is None else man[0]
        known = [i for i, v in enumerate(man) if v is not None]
        disp = []
        for i, g in enumerate(geo):
            if man[i] is not None:
                disp.append(man[i])
                continue
            a = max([k for k in known if k < i], default=None)
            b = min([k for k in known if k > i], default=None)
            if a is not None and b is not None and geo[b] > geo[a]:
                disp.append(man[a] + (man[b] - man[a]) * (g - geo[a]) / (geo[b] - geo[a]))
            elif a is not None:
                ratio = (man[a] / geo[a]) if geo[a] > 0 and man[a] > 0 else 1.0
                disp.append(man[a] + (g - geo[a]) * ratio)
            else:
                disp.append(g)
        by_station = {id(s): d for s, d in zip(sts, disp)}
        self.dpoints = [(by_station[id(st)], fare, st) for _k, fare, st in self.points]   # 表示km版
        self.disp_total_km = disp[-1]
        self.manual_km = len(known) > 1

    def fare_at_progress(self, km):
        """km 地点までに通過した最後の運賃駅の (運賃, 駅, 通過km)"""
        cur = self.points[0]
        for p in self.points:
            if p[0] <= km + 1e-6:
                cur = p
        return cur

    def nearest_point(self, km):
        """同距離比較用: 表示km(手入力があればそれ)で km に一番近い運賃駅"""
        cands = [p for p in self.dpoints if p[0] > 0.5]
        return min(cands, key=lambda p: abs(p[0] - km)) if cands else self.dpoints[-1]


def _nice_step(span, target=5):
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _auto_compare_km(routes):
    shortest = min(r.disp_total_km for r in routes)
    step = 10 if shortest >= 25 else 5
    kms = [k for k in range(step, int(shortest) + 1, step)]
    return kms[:4] or [round(shortest / 2)]


def _draw_live_bars(canvas, routes, tv, map_t0, speed, max_fare):
    d = ImageDraw.Draw(canvas, "RGBA")
    n = len(routes)
    row_h = 118 if n <= 2 else 96
    top = 1130
    d.rounded_rectangle([30, top - 20, CANVAS_W - 30, top + row_h * n + 10], radius=26, fill=(8, 10, 22, 210),
                        outline=(255, 255, 255, 40), width=2)
    km_now = max(0.0, (tv - map_t0) * speed)
    for i, r in enumerate(routes):
        y = top + i * row_h
        kpass, fare, st = r.fare_at_progress(km_now)
        # 直前の運賃からのカウントアップ(通過してから0.4秒)
        prev = [p for p in r.points if p[0] < kpass - 1e-6]
        prev_fare = prev[-1][1] if prev else 0
        t_pass = map_t0 + kpass / speed
        f = ease_out_cubic((tv - t_pass) / 0.4) if kpass > 0 else 1
        shown = prev_fare + (fare - prev_fare) * f
        d.text((60, y + 26), r.short, font=font(30, "black"), fill=r.color + (255,), anchor="lm")
        sect = f"{r.route['stations'][0]['name']}→{st['name']}" if kpass > 0 else "乗車前"
        d.text((60, y + 66), sect, font=font(24), fill=(200, 210, 230, 255), anchor="lm")
        bx0, bx1 = 330, CANVAS_W - 210
        d.rounded_rectangle([bx0, y + 30, bx1, y + 62], radius=16, fill=(255, 255, 255, 36))
        w = (bx1 - bx0) * (shown / max_fare if max_fare else 0)
        if w > 4:
            d.rounded_rectangle([bx0, y + 30, bx0 + w, y + 62], radius=16, fill=r.color + (255,))
        d.text((CANVAS_W - 60, y + 46), yen(shown), font=font(44, "black"), fill=(255, 255, 255, 255), anchor="rm")


def _chart_frame(d, box, max_km, max_fare):
    x0, y0, x1, y1 = box
    xs = _nice_step(max_km, 5)
    ys = _nice_step(max_fare, 5)
    f_ax = font(24)
    k = 0.0
    while k <= max_km + 1e-6:
        x = x0 + (x1 - x0) * k / max_km
        d.line([(x, y0), (x, y1)], fill=(255, 255, 255, 28), width=1)
        d.text((x, y1 + 26), f"{k:g}km", font=f_ax, fill=(190, 200, 220, 255), anchor="mm")
        k += xs
    v = 0.0
    while v <= max_fare + 1e-6:
        y = y1 - (y1 - y0) * v / max_fare
        d.line([(x0, y), (x1, y)], fill=(255, 255, 255, 28), width=1)
        d.text((x0 - 14, y), yen(v), font=f_ax, fill=(190, 200, 220, 255), anchor="rm")
        v += ys
    d.line([(x0, y1), (x1, y1)], fill=(255, 255, 255, 140), width=2)
    d.line([(x0, y0), (x0, y1)], fill=(255, 255, 255, 140), width=2)


def _draw_distance_chart(canvas, cfg, routes, t, compare_km, chart_sec):
    """t: このフェーズに入ってからの秒数"""
    d = ImageDraw.Draw(canvas, "RGBA")
    a_in = ease_out_cubic(t / 0.5)
    d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(3, 5, 14, int(215 * a_in)))
    d.text((CANVAS_W / 2, 330), "同じ距離なら どっちが安い?", font=font(52, "black"),
           fill=(255, 255, 255, int(255 * a_in)), anchor="mm")
    d.text((CANVAS_W / 2, 395), f"横軸=乗った距離 / 縦軸={cfg.get('fare_label', '運賃')}", font=font(28),
           fill=(255, 215, 0, int(255 * a_in)), anchor="mm")
    max_km = max(r.disp_total_km for r in routes) * 1.04
    max_fare = max(r.final_fare for r in routes) * 1.12
    box = (160, 470, CANVAS_W - 60, 1180)
    x0, y0, x1, y1 = box
    _chart_frame(d, box, max_km, max_fare)

    def X(k):
        return x0 + (x1 - x0) * k / max_km

    def Y(v):
        return y1 - (y1 - y0) * v / max_fare

    draw_f = ease_in_out((t - 0.4) / 2.2)
    km_lim = max_km * draw_f
    for r in routes:
        pts = []
        prev = r.dpoints[0]
        pts.append((X(0), Y(prev[1])))
        for p in r.dpoints[1:]:
            if p[0] > km_lim:
                pts.append((X(km_lim), Y(prev[1])))
                break
            pts.append((X(p[0]), Y(prev[1])))
            pts.append((X(p[0]), Y(p[1])))
            prev = p
        if len(pts) >= 2:
            d.line(pts, fill=r.color + (255,), width=6, joint="curve")
        for p in r.dpoints[1:]:
            if p[0] <= km_lim:
                d.ellipse([X(p[0]) - 6, Y(p[1]) - 6, X(p[0]) + 6, Y(p[1]) + 6], fill=r.color + (255,))

    # 凡例
    lx = x0 + 10
    for r in routes:
        d.rounded_rectangle([lx, y0 + 6, lx + 34, y0 + 18], radius=6, fill=r.color + (255,))
        d.text((lx + 44, y0 + 12), r.short, font=font(26), fill=(255, 255, 255, 255), anchor="lm")
        lx += 74 + d.textlength(r.short, font=font(26))

    # 同距離の比較マーカー
    t_mark0 = 2.8
    per = max(0.6, (chart_sec - t_mark0 - 0.6) / max(1, len(compare_km)))
    table_y = 1250
    for j, km in enumerate(compare_km):
        el = t - (t_mark0 + j * per)
        if el < 0:
            continue
        al = int(255 * ease_out_cubic(el / 0.35))
        x = X(km)
        for yy in range(int(y0), int(y1), 18):
            d.line([(x, yy), (x, min(y1, yy + 9))], fill=(255, 215, 0, al), width=3)
        d.text((x, y0 - 22), f"約{km:g}km", font=font(28, "black"), fill=(255, 215, 0, al), anchor="mm")
        row = []
        for r in routes:
            pk, pf, st = r.nearest_point(km)
            row.append((r, pf, st, pk))
            d.ellipse([X(pk) - 13, Y(pf) - 13, X(pk) + 13, Y(pf) + 13], outline=(255, 255, 255, al), width=4)
        # 下の表に1行追加
        ty = table_y + j * 92
        d.rounded_rectangle([40, ty, CANVAS_W - 40, ty + 78], radius=18, fill=(20, 24, 40, int(0.92 * al)))
        d.text((70, ty + 39), f"約{km:g}km", font=font(30, "black"), fill=(255, 215, 0, al), anchor="lm")
        cw = (CANVAS_W - 260) / len(row)
        cheapest = min(v[1] for v in row)
        for c, (r, pf, st, pk) in enumerate(row):
            cx = 230 + cw * c
            d.text((cx, ty + 24), f"{st['name']}({pk:.0f}km)", font=font(22), fill=(200, 210, 230, al), anchor="lm")
            col = r.color if pf > cheapest or len(set(v[1] for v in row)) == 1 else (255, 215, 0)
            d.text((cx, ty + 56), yen(pf) + (" 安!" if pf == cheapest and len(set(v[1] for v in row)) > 1 else ""),
                   font=font(30, "black"), fill=tuple(col) + (al,), anchor="lm")


def _draw_final(canvas, cfg, routes, t):
    d = ImageDraw.Draw(canvas, "RGBA")
    a = ease_out_cubic(t / 0.5)
    d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(3, 5, 14, int(225 * a)))
    end = cfg.get("end_label", routes[0].route["stations"][-1]["name"])
    start = cfg.get("start_name", routes[0].route["stations"][0]["name"])
    d.text((CANVAS_W / 2, 360), f"{start} → {end}", font=font(60, "black"), fill=(255, 255, 255, int(255 * a)),
           anchor="mm")
    d.text((CANVAS_W / 2, 430), f"最終目的地までの{cfg.get('fare_label', '運賃')}は…", font=font(34),
           fill=(255, 215, 0, int(255 * a)), anchor="mm")
    n = len(routes)
    row_h = 200 if n <= 2 else 150
    y = 520
    count_f = ease_in_out((t - 0.6) / 1.8)
    cheapest = min(r.final_fare for r in routes)
    for i, r in enumerate(routes):
        ry = y + i * row_h
        d.rounded_rectangle([70, ry, CANVAS_W - 70, ry + row_h - 30], radius=26, fill=(20, 24, 40, 240),
                            outline=r.color + (255,), width=5)
        d.text((110, ry + 50), r.name, font=font(38, "black"), fill=r.color + (255,), anchor="lm")
        d.text((110, ry + 105), f"{r.disp_total_km:.1f}km / 1kmあたり {r.final_fare / max(r.disp_total_km, 0.1):.1f}円",
               font=font(26), fill=(200, 210, 230, 255), anchor="lm")
        v = r.final_fare * count_f
        win = count_f >= 1 and r.final_fare == cheapest and len(set(x.final_fare for x in routes)) > 1
        d.text((CANVAS_W - 110, ry + (row_h - 30) / 2), yen(v), font=font(84, "black"),
               fill=(255, 215, 0, 255) if win else (255, 255, 255, 255), anchor="rm")
    if count_f >= 1:
        el = t - 2.4
        al = int(255 * ease_out_cubic(el / 0.4))
        sc = pop_scale(el)
        fares = sorted(r.final_fare for r in routes)
        yb = y + n * row_h + 70
        if fares[0] == fares[-1]:
            msg, sub = "運賃は同じ!", ""
        else:
            win = min(routes, key=lambda r: r.final_fare)
            diff = fares[1] - fares[0]
            msg = f"{win.short}が {yen(diff)} 安い!"
            sub = f"(往復なら {yen(diff * 2)} の差)"
        d.text((CANVAS_W / 2, yb), msg, font=font(int(64 * max(sc, 0.01)), "black"), fill=(255, 215, 0, al),
               anchor="mm")
        if sub:
            d.text((CANVAS_W / 2, yb + 80), sub, font=font(32), fill=(255, 255, 255, al), anchor="mm")
        note = cfg.get("result_note")
        if note:
            d.text((CANVAS_W / 2, yb + 140), note, font=font(26), fill=(200, 210, 230, al), anchor="mm")


def render_fare(config, geojson_path, station_db, out_dir="output", fast_preview=False, out_name=None):
    slug = out_name or config.get("slug", "fare")
    line_geoms = load_line_geoms(geojson_path)
    routes = []
    for i, rc in enumerate(config["routes"]):
        color = rc.get("color") or PALETTE[i % len(PALETTE)]
        route, warnings = build_geometry(rc["stations"], line_geoms, station_db, key=rc.get("key", f"r{i}"),
                                         name=rc.get("name", ""), color=color)
        for w in warnings:
            print("[warn]", w)
        routes.append(FareRoute(rc, route, i))

    base, proj = build_base_map(
        [r.route for r in routes], MAP_BOX, config.get("title_line1", ""), config.get("title_line2", ""),
        legend=[(r.name, r.color) for r in routes], geojson_path=geojson_path,
        start_label=config.get("start_name"), end_label=config.get("end_label"),
        dot_station_names=[p[2]["name"] for r in routes for p in r.points])
    base.convert("RGB").save(f"{out_dir}/{slug}_base_map.png")

    fps = 10 if fast_preview else int(config.get("fps", 30))
    intro = float(config.get("intro_sec", 1.5))
    map_sec = float(config.get("map_sec", 13))
    show_chart = bool(config.get("show_distance_chart", True))
    compare_km = config.get("compare_km") or _auto_compare_km(routes)
    compare_km = compare_km[:4]
    chart_sec = float(config.get("chart_sec", 3.0 + 1.3 * len(compare_km))) if show_chart else 0
    final_sec = float(config.get("final_sec", 5.5))

    max_km = max(r.total_km for r in routes)
    speed = max_km / map_sec        # km / 動画秒
    map_t0 = intro
    map_end = intro + map_sec + 0.8
    chart_t0 = map_end
    final_t0 = chart_t0 + chart_sec
    total = final_t0 + final_sec
    max_fare = max(r.final_fare for r in routes)

    # 駅ポップアップ: 同じ駅に複数ルートの運賃が出る場合は上下にずらす
    pops = []
    for ri, r in enumerate(routes):
        for k, fare, st in r.points[1:]:
            pops.append((map_t0 + k / speed, r, st, fare, ri))

    script = [(0.0, f"{config.get('title_line1', '')}、{config.get('fare_label', '運賃')}が安いのはどっち?")]
    for t0, r, st, fare, _ in sorted(pops, key=lambda x: x[0]):
        if st.get("popup"):
            script.append((t0, f"{r.short}、{st['name']}までは{int(fare)}円。"))
    if show_chart:
        script.append((chart_t0, "同じ距離で比べてみると…"))
    w = min(routes, key=lambda r: r.final_fare)
    script.append((final_t0, f"最終目的地までの運賃は、{w.short}が一番安い{int(w.final_fare)}円。"))
    write_text_script(out_dir, slug, sorted(script, key=lambda x: x[0]))

    writer = FrameWriter(f"{out_dir}/{slug}.mp4", fps)
    n_frames = int(total * fps)
    print(f"rendering {n_frames} frames ({total:.1f}s @ {fps}fps) ...")
    for fi in range(n_frames):
        tv = fi / fps
        map_visible_until = (chart_t0 if show_chart else final_t0) + 0.6
        if tv < map_visible_until:
            canvas = base.copy()
            km_now = max(0.0, (tv - map_t0) * speed)
            for r in routes:
                k = min(km_now, r.total_km)
                if k > 0.02:
                    draw_trail(canvas, r.path.screen_polyline(proj, 0, r.k0, r.k0 + k), r.color)
            for ri, r in reversed(list(enumerate(routes))):
                k = min(km_now, r.total_km)
                x, y = r.path.screen_at_km(proj, r.k0 + k)
                draw_train(canvas, x, y, r.color, badge=r.badge, icon_img=r.icon)
            for t0, r, st, fare, ri in pops:
                el = tv - t0
                last = st is r.route["stations"][-1]
                life = 99 if last else 2.2
                if 0 <= el <= life:
                    alpha = 255 if el < life - 0.35 else int(255 * (life - el) / 0.35)
                    x, y = project(proj, st["lon"], st["lat"])
                    dy = -20 - ri * 64 if ri % 2 == 0 else 80 + (ri // 2) * 64
                    draw_chip(canvas, x, y + dy, f"{st['name']} {yen(fare)}", r.color, size=28,
                              scale=pop_scale(el), alpha=alpha, tail=(ri % 2 == 0))
            _draw_live_bars(canvas, routes, tv, map_t0, speed, max_fare)
            last_map = canvas
        else:
            canvas = last_map.copy()
        if show_chart and chart_t0 <= tv < final_t0 + 0.6:
            _draw_distance_chart(canvas, config, routes, tv - chart_t0, compare_km, chart_sec)
        if tv >= final_t0:
            _draw_final(canvas, config, routes, tv - final_t0)
        writer.write(canvas)
    return writer.close()
