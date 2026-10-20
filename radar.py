#!/usr/bin/env python3
"""Türkiye trend radarı.

Her çalıştırmada (günde bir kez, bu bilgisayarın TR IP'sinden):
  1. TikTok Explore akışından Türkiye'de dolaşan videoları toplar (girişsiz)
     ve config.json'daki nişlere ayırır.
  2. İzlenen hashtag'lerin toplam video/izlenme sayılarını kaydeder; geçmiş
     anlık görüntülerden hız (günlük artış), ivme ve yaşam evresi hesaplar.
  3. Nişte tekrar eden yeni hashtag'leri otomatik takibe alır.
  4. TikTok Creative Center (TR) resmi hashtag'lerini ve Google Trends TR
     gündemini çeker.

Çıktılar:
  data/history/YYYY-MM-DD.json  günlük anlık görüntü (momentum hesabı için)
  data/pool.json                son `poolDays` günün Explore videoları
  data/watchlist.json           otomatik keşfedilen hashtag'ler
  data/latest.json              tüm sonuçlar
  data/sheet/*.csv              Google Sheets'in IMPORTDATA ile okuduğu tablolar
  rapor.md                      okunabilir günlük özet
"""

import asyncio
import csv
import json
import random
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HISTORY = DATA / "history"
SHEET = DATA / "sheet"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
EXPLORE_URL = "https://www.tiktok.com/explore?lang=tr-TR"
CC_HASHTAG_API = "https://ads.tiktok.com/CreativeOne/KnowledgeAPI/GetHashtagList"
GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"
GT_NS = {"ht": "https://trends.google.com/trending/rss"}

TR_ASCII = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def log(*args):
    print("[radar]", *args, flush=True)


def norm(text):
    """Karşılaştırma anahtarı: küçük harf + Türkçe karakterler ASCII'ye.
    "Kızkıza", "kizkiza" ve "KIZKIZA" aynı anahtara düşer."""
    t = (text or "").replace("İ", "i").replace("I", "ı").lower()
    return t.translate(TR_ASCII).strip()


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# --------------------------------------------------------------------------
# TikTok (Playwright, girişsiz)
# --------------------------------------------------------------------------

async def new_page(browser):
    ctx = await browser.new_context(user_agent=UA, locale="tr-TR",
                                    viewport={"width": 1440, "height": 900})
    return ctx, await ctx.new_page()


async def click_chip(page, label):
    return await page.evaluate("""(label) => {
        const el = [...document.querySelectorAll('button, [role="tab"]')]
            .find(e => (e.textContent || '').trim() === label);
        if (!el) return false;
        el.scrollIntoView({block: 'center', inline: 'center'});
        el.click();
        return true;
    }""", label)


async def collect_explore(browser, categories, scrolls):
    """Explore'da "Tümü" akışını ve her kategori butonunu gezer.
    Her kategori yeni bir categoryType ile istek atar; tıklamadan sonra ilk
    beliren yeni categoryType o kategoriye aittir.
    Dönüş: {video_id: (item, kategori_adı)}"""
    feeds = defaultdict(dict)  # categoryType -> {id: item}
    ctx, page = await new_page(browser)

    async def on_response(resp):
        if "/explore/item_list" not in resp.url:
            return
        m = re.search(r"categoryType=(\d+)", resp.url)
        try:
            for it in (await resp.json()).get("itemList") or []:
                if it.get("id"):
                    feeds[m.group(1) if m else "120"][it["id"]] = it
        except Exception:
            pass

    async def scroll():
        for _ in range(scrolls):
            await page.keyboard.press("End")
            await page.wait_for_timeout(2200 + random.randint(0, 900))

    page.on("response", on_response)
    await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(6000)
    label_of = {"120": "Tümü"}
    await scroll()
    for label in categories:
        known = set(feeds)
        if not await click_chip(page, label):
            log(f"  UYARI: '{label}' kategori butonu bulunamadı")
            continue
        for _ in range(20):
            await page.wait_for_timeout(500)
            new = [c for c in feeds if c not in known and c not in label_of]
            if new:
                label_of[new[0]] = label
                break
        else:
            log(f"  UYARI: '{label}' tıklandı ama akış gelmedi")
            continue
        await page.evaluate("window.scrollTo(0, 0)")
        await scroll()
    await ctx.close()

    out = {}
    for cat, items in feeds.items():
        for vid, it in items.items():
            out.setdefault(vid, (it, label_of.get(cat, "Tümü")))
    log("explore: " + ", ".join(f"{label_of.get(c, c)}={len(v)}" for c, v in feeds.items())
        + f" → {len(out)} tekil video")
    return out


async def fetch_tag_stats(browser, tags):
    """Her hashtag sayfasından toplam video/izlenme sayısını okur.
    TikTok ara sıra captcha gösterir; kaçanlar bir kez yeni tarayıcıyla
    denenir, yine olmazsa o gün atlanır (momentum boşlukları tolere eder)."""
    out = {}
    remaining = list(tags)
    for attempt in range(2):
        if not remaining:
            break
        ctx, page = await new_page(browser)
        seen = {}

        async def on_response(resp):
            if "/api/challenge/detail" not in resp.url:
                return
            try:
                ci = (await resp.json()).get("challengeInfo") or {}
            except Exception:
                return
            title = (ci.get("challenge") or {}).get("title") or ""
            stats = ci.get("statsV2") or ci.get("stats") or {}
            if title:
                seen[norm(title)] = {"name": title, "n": int(stats.get("videoCount") or 0),
                                     "v": int(stats.get("viewCount") or 0)}

        page.on("response", on_response)
        failed = []
        for tag in remaining:
            try:
                await page.goto(f"https://www.tiktok.com/tag/{urllib.parse.quote(tag)}?lang=tr-TR",
                                wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3500 + random.randint(0, 1500))
            except Exception as e:
                log(f"  #{tag}: sayfa hatası {str(e)[:60]}")
            if norm(tag) in seen:
                out[norm(tag)] = seen[norm(tag)]
            else:
                failed.append(tag)
        await ctx.close()
        remaining = failed
        if failed and attempt == 0:
            log(f"  {len(failed)} etiket tekrar denenecek")
    for tag in remaining:
        log(f"  UYARI: #{tag} için veri alınamadı (captcha veya etiket yok)")
    log(f"hashtag istatistikleri: {len(out)}/{len(tags)}")
    return out


# --------------------------------------------------------------------------
# Creative Center ve Google Trends (düz HTTP)
# --------------------------------------------------------------------------

def http(url, data=None, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def fetch_cc_hashtags(region):
    try:
        body = json.dumps({"timeRange": 7, "countryCode": region, "page": 1, "limit": 50}).encode()
        raw = http(CC_HASHTAG_API, body, {"content-type": "application/json",
                                          "referer": "https://ads.tiktok.com/", "agw-js-conv": "str"})
        items = json.loads(raw).get("items") or []
        out = [{"tag": it.get("hashtagName"), "rank": int(it.get("rankIndex") or 0),
                "posts": int(it.get("publishCnt") or 0), "views": int(it.get("vv") or 0)}
               for it in items]
        log(f"creative center: {len(out)} hashtag")
        return out
    except Exception as e:
        log("UYARI: creative center alınamadı:", e)
        return []


def fetch_google_trends(geo):
    try:
        root = ET.fromstring(http(GOOGLE_TRENDS_RSS.format(geo=geo)))
        out = []
        for it in root.findall("./channel/item"):
            news = [n.findtext("ht:news_item_title", namespaces=GT_NS) or ""
                    for n in it.findall("ht:news_item", GT_NS)]
            out.append({"query": it.findtext("title") or "",
                        "traffic": it.findtext("ht:approx_traffic", namespaces=GT_NS) or "",
                        "news": [n for n in news if n][:2]})
        log(f"google trends: {len(out)} arama")
        return out
    except Exception as e:
        log("UYARI: google trends alınamadı:", e)
        return []


# --------------------------------------------------------------------------
# Nişlere ayırma ve havuz
# --------------------------------------------------------------------------

def compact(item):
    music = item.get("music") or {}
    author = (item.get("author") or {}).get("uniqueId") or ""
    return {
        "desc": (item.get("desc") or "")[:200],
        "tags": [c.get("title") or "" for c in item.get("challenges") or [] if c.get("title")],
        "music": {"id": str(music.get("id") or ""), "title": music.get("title") or "",
                  "author": music.get("authorName") or "", "original": bool(music.get("original"))},
        "plays": int((item.get("stats") or {}).get("playCount") or 0),
        "url": f"https://www.tiktok.com/@{author}/video/{item.get('id')}",
    }


def niche_matchers(niches):
    out = {}
    for name, spec in niches.items():
        cats = set(spec.get("categories") or [])
        tags = {norm(t) for t in spec.get("tags") or []}
        kws = [re.compile(r"\b" + re.escape(norm(k)) + r"\b") for k in spec.get("keywords") or []]
        out[name] = (cats, tags, kws)
    return out


def niches_of(video, matchers):
    vcats = set(video.get("cats") or [])
    vtags = {norm(t) for t in video["tags"]}
    desc = norm(video["desc"])
    return [name for name, (cats, tags, kws) in matchers.items()
            if vcats & cats or vtags & tags or any(k.search(desc) for k in kws)]


def update_pool(pool, explore, today, pool_days):
    for vid, (item, cat) in explore.items():
        rec = pool.get(vid) or {"first": today, "cats": []}
        rec.update(compact(item))
        rec["seen"] = today
        if cat not in rec.setdefault("cats", []):
            rec["cats"].append(cat)
        pool[vid] = rec
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=pool_days)).strftime("%Y-%m-%d")
    for vid in [v for v, r in pool.items() if r["seen"] < cutoff]:
        del pool[vid]
    return pool


def is_turkish(video):
    text = video["desc"] + " " + " ".join(video["tags"])
    return bool(re.search(r"[çğışöüÇĞİŞÖÜ]", text))


# --------------------------------------------------------------------------
# Momentum
# --------------------------------------------------------------------------

def tag_series(history, key, today_stat, today):
    series = []
    for snap in history:
        st = (snap.get("tags") or {}).get(key)
        if st and snap["date"] != today:
            series.append({"date": snap["date"], "n": st["n"], "v": st["v"]})
    if today_stat:
        series.append({"date": today, "n": today_stat["n"], "v": today_stat["v"]})
    return series


def _per_day(a, b, field):
    days = max((datetime.strptime(b["date"], "%Y-%m-%d")
                - datetime.strptime(a["date"], "%Y-%m-%d")).days, 1)
    return (b[field] - a[field]) / days


def tag_momentum(series):
    """Hız = günlük yeni video, ivme = son hız - önceki hız.
    Evreler: erken yükseliş (küçük + hızlanıyor) > hızlanıyor > zirvede > yavaşlıyor."""
    if not series:
        return {"stage": "veri yok"}
    last = series[-1]
    if len(series) < 2:
        return {"stage": "veri bekleniyor", "videos": last["n"], "views": last["v"]}
    vel = _per_day(series[-2], last, "n")
    vel_views = _per_day(series[-2], last, "v")
    growth = vel / max(series[-2]["n"], 1)
    out = {"videos": last["n"], "views": last["v"], "velocity": round(vel),
           "velocityViews": round(vel_views), "growthPct": round(growth * 100, 3),
           "acceleration": None, "days": len(series)}
    if len(series) < 3:
        out["stage"] = "büyüyor" if vel > 0 else "durgun"
        return out
    prev_vel = _per_day(series[-3], series[-2], "n")
    acc = vel - prev_vel
    out["acceleration"] = round(acc)
    if vel <= 0:
        out["stage"] = "düşüşte"
    elif acc > 0 and last["n"] < 20000:
        out["stage"] = "🔥 erken yükseliş"
    elif acc > 0:
        out["stage"] = "📈 hızlanıyor"
    elif acc > -0.15 * max(prev_vel, 1):
        out["stage"] = "zirvede"
    else:
        out["stage"] = "yavaşlıyor"
    return out


STAGE_ORDER = {"🔥 erken yükseliş": 0, "📈 hızlanıyor": 1, "büyüyor": 2, "zirvede": 3,
               "veri bekleniyor": 4, "yavaşlıyor": 5, "durgun": 6, "düşüşte": 7, "veri yok": 8}


def rank_sounds(videos, today):
    """Havuzdaki videolardan ses sıralaması. Tek videoda geçen orijinal sesler
    gürültüdür; aynı ses birden fazla videoda geçiyorsa trend adayıdır."""
    sounds = {}
    for v in videos:
        m = v["music"]
        if not m["id"]:
            continue
        # Aynı şarkı farklı yüklemelerle farklı kimlik alabiliyor; orijinal
        # olmayan sesleri başlık + sanatçıya göre birleştir.
        key = m["id"] if m["original"] else norm(m["title"]) + "|" + norm(m["author"])
        s = sounds.setdefault(key, {**m, "videos": 0, "today": 0, "plays": 0,
                                    "first": v["first"], "examples": []})
        s["videos"] += 1
        s["plays"] += v["plays"]
        s["today"] += v["seen"] == today and v["first"] == today
        s["first"] = min(s["first"], v["first"])
        if len(s["examples"]) < 2:
            s["examples"].append(v["url"])
    ranked = []
    for s in sounds.values():
        if s["original"] and s["videos"] < 2:
            continue
        if s["first"] == today:
            s["label"] = "yeni"
        elif s["today"] > 0 and s["today"] * 2 >= s["videos"]:
            s["label"] = "yükselişte"
        elif s["today"] > 0:
            s["label"] = "aktif"
        else:
            s["label"] = "sakin"
        s["link"] = f"https://www.tiktok.com/music/x-{s['id']}"
        ranked.append(s)
    ranked.sort(key=lambda s: (s["videos"], s["today"], s["plays"]), reverse=True)
    return ranked[:25]


def discover_tags(videos, matchers, ignore, watchlist, fixed, today, limit, ttl):
    """Nişte bugün 2+ videoda geçen, sabit listede olmayan etiketleri takibe al."""
    counts = defaultdict(lambda: {"n": 0, "name": "", "niche": ""})
    for v in videos:
        if v["seen"] != today:
            continue
        nn = niches_of(v, matchers)
        if not nn:
            continue
        for t in v["tags"]:
            k = norm(t)
            if k and k not in ignore and k not in fixed:
                counts[k]["n"] += 1
                counts[k]["name"] = counts[k]["name"] or t
                counts[k]["niche"] = counts[k]["niche"] or nn[0]
    for k, c in counts.items():
        if c["n"] >= 2:
            entry = watchlist.setdefault(k, {"name": c["name"], "niche": c["niche"], "added": today})
            entry["lastSeen"] = today
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=ttl)).strftime("%Y-%m-%d")
    for k in [k for k, e in watchlist.items() if e.get("lastSeen", e["added"]) < cutoff]:
        del watchlist[k]
    if len(watchlist) > limit:
        keep = sorted(watchlist.items(), key=lambda kv: kv[1].get("lastSeen", ""), reverse=True)[:limit]
        watchlist.clear()
        watchlist.update(keep)
    return watchlist


# --------------------------------------------------------------------------
# Çıktılar
# --------------------------------------------------------------------------

def write_csv(name, header, rows):
    SHEET.mkdir(parents=True, exist_ok=True)
    with open(SHEET / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def fmt(n):
    if n is None:
        return "–"
    n = int(n)
    if abs(n) >= 1_000_000_000:
        return f"{n/1e9:.1f} Mr"
    if abs(n) >= 1_000_000:
        return f"{n/1e6:.1f} Mn"
    if abs(n) >= 1_000:
        return f"{n/1e3:.1f} B"
    return str(n)


def write_outputs(result, cfg):
    today = result["date"]
    tags = result["tags"]
    write_csv("hashtagler.csv",
              ["tarih", "etiket", "niş", "kaynak", "evre", "toplam_video", "toplam_izlenme",
               "gunluk_yeni_video", "gunluk_yeni_izlenme", "buyume_yuzde", "ivme", "gun_sayisi", "link"],
              [[today, "#" + t["name"], t["niche"], t["source"], t["stage"], t.get("videos"),
                t.get("views"), t.get("velocity"), t.get("velocityViews"), t.get("growthPct"),
                t.get("acceleration"), t.get("days"), t["link"]] for t in tags])
    rows = []
    for niche, sounds in result["sounds"].items():
        for s in sounds:
            # Örnek video linkleri başka kullanıcılara ait; yayınlanan CSV'ye
            # girmez, yalnızca yereldeki latest.json'da kalır.
            rows.append([today, niche, s["title"], s["author"], s["label"], s["videos"], s["today"],
                         s["plays"], "evet" if s["original"] else "hayır", s["link"]])
    write_csv("sesler.csv", ["tarih", "niş", "ses", "sanatçı", "durum", "video_7gun", "video_bugun",
                             "toplam_izlenme", "orijinal_ses", "link"], rows)
    write_csv("gundem.csv", ["tarih", "arama", "trafik", "haber_basliklari"],
              [[today, g["query"], g["traffic"], " | ".join(g["news"])] for g in result["googleTrends"]])
    write_csv("creative_center.csv", ["tarih", "sira", "etiket", "paylasim", "izlenme"],
              [[today, h["rank"], "#" + (h["tag"] or ""), h["posts"], h["views"]] for h in result["ccHashtags"]])

    lines = [f"# Trend Radarı — {today}", "",
             f"Explore'dan bugün {result['exploreToday']} video, havuzda {result['poolSize']} video "
             f"({result['turkishShare']}% Türkçe). Geçmiş: {result['snapshots']} gün.", ""]
    if result["snapshots"] < 3:
        lines += ["> ⏳ Hız/ivme için en az 3 günlük veri gerekiyor. İlk günlerde evreler "
                  "\"veri bekleniyor\" görünür, normaldir.", ""]
    lines += ["## Hashtag'ler (evreye göre)", "",
              "| Etiket | Niş | Evre | Toplam video | Günlük yeni video | Büyüme | İvme |",
              "|---|---|---|---|---|---|---|"]
    for t in tags[:20]:
        g = f"%{t['growthPct']}" if t.get("growthPct") is not None else "–"
        lines.append(f"| [#{t['name']}]({t['link']}) | {t['niche']} | {t['stage']} | {fmt(t.get('videos'))} "
                     f"| {fmt(t.get('velocity'))} | {g} | {fmt(t.get('acceleration'))} |")
    for niche, sounds in result["sounds"].items():
        lines += ["", f"## Sesler — {niche}", ""]
        if not sounds:
            lines.append("_Henüz tekrar eden ses yok (havuz büyüdükçe dolacak)._")
            continue
        lines += ["| Ses | Durum | Video (7 gün) | Bugün |", "|---|---|---|---|"]
        for s in sounds[:10]:
            title = f"{s['title']} — {s['author']}".replace("|", "/")
            lines.append(f"| [{title}]({s['link']}) | {s['label']} "
                         f"| {s['videos']} | {s['today']} |")
    lines += ["", "## Gündem (Google Trends TR)", ""]
    for g in result["googleTrends"]:
        news = f" — {g['news'][0]}" if g["news"] else ""
        lines.append(f"- **{g['query']}** ({g['traffic']}){news}")
    if result["ccHashtags"]:
        lines += ["", "## Creative Center TR (resmi, 7 gün)", ""]
        lines += [f"- #{h['tag']} — {fmt(h['posts'])} paylaşım, {fmt(h['views'])} izlenme"
                  for h in result["ccHashtags"]]
    lines += ["", "## Elle bakılacaklar (otomatik çekilmez)", ""]
    lines += [f"- [{k}]({v})" for k, v in (cfg.get("manualSources") or {}).items()]
    lines += ["- Instagram: Reels kamerası → Ses → **Trend** sekmesi (yukarı ok = yükselişte)", ""]
    (ROOT / "rapor.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# Ana akış
# --------------------------------------------------------------------------

async def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = read_json(ROOT / "config.json", None)
    tz = ZoneInfo(cfg.get("timezone", "Europe/Istanbul"))
    today = datetime.now(timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
    HISTORY.mkdir(parents=True, exist_ok=True)

    niches = cfg["niches"]
    matchers = niche_matchers(niches)
    ignore = {norm(t) for t in cfg.get("ignoreTags") or []}
    fixed = {}
    for name, spec in niches.items():
        for t in spec.get("watch") or []:
            fixed.setdefault(norm(t), {"name": t, "niche": name, "source": "sabit"})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        categories = []
        for spec in niches.values():
            categories += [c for c in spec.get("categories") or [] if c not in categories]
        categories += [c for c in cfg.get("extraCategories") or [] if c not in categories]
        explore = await collect_explore(browser, categories, int(cfg.get("exploreScrolls", 6)))

        pool = update_pool(read_json(DATA / "pool.json", {}), explore, today,
                           int(cfg.get("poolDays", 7)))
        videos = list(pool.values())
        watchlist = discover_tags(videos, matchers, ignore, read_json(DATA / "watchlist.json", {}),
                                  set(fixed), today, int(cfg.get("maxDiscoveredTags", 25)),
                                  int(cfg.get("discoveredTtlDays", 10)))
        tracked = dict(fixed)
        for k, e in watchlist.items():
            tracked.setdefault(k, {"name": e["name"], "niche": e["niche"], "source": "keşif"})
        stats = await fetch_tag_stats(browser, [e["name"] for e in tracked.values()])
        await browser.close()

    if not explore and not stats:
        log("HATA: TikTok'tan hiç veri gelmedi (IP engeli veya sayfa değişikliği)")
        sys.exit(1)

    cc = fetch_cc_hashtags(cfg.get("region", "TR"))
    gt = fetch_google_trends(cfg.get("region", "TR"))

    history = [read_json(f, {}) for f in sorted(HISTORY.glob("*.json"))[-int(cfg.get("historyDays", 30)):]]
    history = [h for h in history if h.get("date")]

    tag_rows = []
    for k, meta in tracked.items():
        m = tag_momentum(tag_series(history, k, stats.get(k), today))
        name = (stats.get(k) or {}).get("name") or meta["name"]
        tag_rows.append({"key": k, "name": name, "niche": meta["niche"], "source": meta["source"],
                         "link": f"https://www.tiktok.com/tag/{urllib.parse.quote(name)}", **m})
    tag_rows.sort(key=lambda t: (STAGE_ORDER.get(t["stage"], 9), -(t.get("growthPct") or 0)))

    by_niche = defaultdict(list)
    for v in videos:
        for n in niches_of(v, matchers):
            by_niche[n].append(v)
    turkish = [v for v in videos if is_turkish(v)]
    sounds = {n: rank_sounds(by_niche.get(n, []), today) for n in niches}
    extra = set(cfg.get("extraCategories") or [])
    for cat in extra:
        sounds[cat] = rank_sounds([v for v in videos if cat in (v.get("cats") or [])], today)
    sounds["Genel Türkiye"] = rank_sounds(turkish, today)

    snap_dates = {h["date"] for h in history} | {today}
    result = {
        "generatedAt": datetime.now(timezone.utc).isoformat(), "date": today,
        "exploreToday": len(explore), "poolSize": len(videos),
        "turkishShare": round(100 * len(turkish) / max(len(videos), 1)),
        "snapshots": len(snap_dates), "tags": tag_rows, "sounds": sounds,
        "nicheVideoCounts": {n: len(by_niche.get(n, [])) for n in niches},
        "ccHashtags": cc, "googleTrends": gt,
    }

    write_json(DATA / "pool.json", pool)
    write_json(DATA / "watchlist.json", watchlist)
    write_json(HISTORY / f"{today}.json", {"date": today, "tags": stats})
    write_json(DATA / "latest.json", result)
    write_outputs(result, cfg)
    log(f"tamam: {len(tag_rows)} etiket, nişlere düşen video {result['nicheVideoCounts']}, rapor.md yazıldı")


if __name__ == "__main__":
    asyncio.run(main())
