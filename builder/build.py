#!/usr/bin/env python3
"""
Builds the data the Nuvio Live TV addon serves.

For every country:
  * public channels and streams from iptv-org (https://github.com/iptv-org/api)
  * the free daily XMLTV guide from epg.pw, matched to channels by name
    (the two sources label channels differently, so ids alone never line up)

Output (written to ./out, published to the `data` branch):
  countries.json                 every country with channel / guide counts
  categories.json                category ids and names
  c/<cc>.json                    channels of one country (compact)
  now/<cc>.json                  programmes from 2 h ago to 14 h ahead, per channel
  g/<cc>/<channel-id>.json       full schedule of one channel (1 day back, 3 ahead)
  report.json                    per-country match statistics
"""
import concurrent.futures as cf
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

API = "https://iptv-org.github.io/api/"
EPG = "https://epg.pw/xmltv/epg_{cc}.xml.gz"
OUT = os.environ.get("OUT", "out")
UA = "nuvio-livetv-builder/1.0 (+https://github.com/jyotiprakash101/nuvio-livetv)"
NOW = datetime.now(timezone.utc)
BACK = NOW - timedelta(days=1)
AHEAD = NOW + timedelta(days=3, hours=12)
NOW_FROM = NOW - timedelta(hours=2)
NOW_TO = NOW + timedelta(hours=14)


def get(url, timeout=120, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:  # network hiccup: retry
            last = e
        time.sleep(2 + i * 3)
    print(f"  ! giving up on {url}: {last}", file=sys.stderr)
    return None


def api(name):
    data = get(API + name + ".json")
    return json.loads(data) if data else []


# ---------------------------------------------------------------- name matching

NOISE = re.compile(
    r"\b(hd|sd|uhd|fhd|4k|8k|720p|1080p|480p|360p|tv\s*hd|channel|live|official|india|in|"
    r"usa|us|uk|ca|canada|apac|me|mena|international|intl|world|asia|europe|plus\s*hd)\b"
)


def norm(s, strong=False):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = s.replace("&", " and ").replace("+", " plus ")
    if strong:
        s = NOISE.sub(" ", s)
        s = re.sub(r"\btv\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def name_keys(names):
    keys = []
    for n in names:
        for strong in (False, True):
            k = norm(n, strong)
            if len(k) >= 2 and k not in keys:
                keys.append(k)
    return keys


# ---------------------------------------------------------------- guide parsing

def parse_time(t):
    # "20261120183000 +0530"
    t = t.strip()
    dt = datetime.strptime(t[:14], "%Y%m%d%H%M%S")
    off = t[14:].strip() or "+0000"
    sign = -1 if off[0] == "-" else 1
    off = off.lstrip("+-")
    delta = timedelta(hours=int(off[:2] or 0), minutes=int(off[2:4] or 0))
    return (dt - sign * delta).replace(tzinfo=timezone.utc)


def parse_guide(xml_bytes):
    """Returns ({epg_id: [display names]}, {epg_id: [programmes]})."""
    names, progs = {}, {}
    for _, el in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if el.tag == "channel":
            cid = el.get("id")
            names[cid] = [d.text for d in el.findall("display-name") if d.text]
            el.clear()
        elif el.tag == "programme":
            try:
                start, stop = parse_time(el.get("start")), parse_time(el.get("stop"))
            except Exception:
                el.clear()
                continue
            if stop < BACK or start > AHEAD:
                el.clear()
                continue
            title = (el.findtext("title") or "").strip()
            if not title:
                el.clear()
                continue
            p = {"s": int(start.timestamp()), "e": int(stop.timestamp()), "t": title[:120]}
            sub = (el.findtext("sub-title") or "").strip()
            if sub:
                p["st"] = sub[:120]
            desc = (el.findtext("desc") or "").strip()
            if desc:
                p["d"] = desc[:400]
            cats = [c.text.strip() for c in el.findall("category") if c.text and c.text.strip()]
            if cats:
                p["c"] = cats[:3]
            icon = el.find("icon")
            if icon is not None and icon.get("src"):
                p["i"] = icon.get("src")
            ep = el.findtext("episode-num")
            if ep:
                p["ep"] = ep.strip()[:20]
            progs.setdefault(el.get("channel"), []).append(p)
            el.clear()
    for v in progs.values():
        v.sort(key=lambda p: p["s"])
    return names, progs


# ---------------------------------------------------------------- main

def main():
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    print("Loading iptv-org api…")
    with cf.ThreadPoolExecutor(8) as ex:
        futs = {n: ex.submit(api, n) for n in
                ["channels", "streams", "feeds", "logos", "categories", "countries", "blocklist"]}
        channels, streams, feeds, logos, categories, countries, blocklist = (
            futs[n].result() for n in
            ["channels", "streams", "feeds", "logos", "categories", "countries", "blocklist"])
    print(f"  {len(channels)} channels, {len(streams)} streams")

    blocked = {b.get("channel") for b in blocklist}
    by_id = {c["id"]: c for c in channels if c["id"] not in blocked and not c.get("closed")}

    langs = {}
    for f in feeds:
        if f.get("languages"):
            langs.setdefault(f["channel"], set()).update(f["languages"])

    logo = {}
    for l in logos:
        if l.get("feed"):
            continue
        cur = logo.get(l["channel"])
        score = (l.get("width") or 0) * (0.5 if "picons" in (l.get("tags") or []) else 1)
        if cur is None or score > cur[0]:
            logo[l["channel"]] = (score, l["url"])

    QUALITY = {"2160p": 5, "1080p": 4, "720p": 3, "576p": 2, "480p": 2, "360p": 1}
    chan_streams = {}
    for s in streams:
        cid = s.get("channel")
        if not cid or cid not in by_id:
            continue
        st = {"u": s["url"]}
        if s.get("quality"):
            st["q"] = s["quality"]
        if s.get("referrer"):
            st["r"] = s["referrer"]
        if s.get("user_agent"):
            st["a"] = s["user_agent"]
        if s.get("feed"):
            st["f"] = s["feed"]
        chan_streams.setdefault(cid, []).append(st)
    for v in chan_streams.values():
        v.sort(key=lambda st: -QUALITY.get(st.get("q", ""), 0))

    per_country = {}
    for cid, sts in chan_streams.items():
        c = by_id[cid]
        per_country.setdefault(c["country"], []).append(c)

    json.dump({c["id"]: c["name"] for c in categories}, open(f"{OUT}/categories.json", "w"))

    country_names = {c["code"]: c for c in countries}
    codes = sorted(per_country)
    print(f"Countries with streams: {len(codes)}")

    def build_country(cc):
        chans = per_country[cc]
        xml = get(EPG.format(cc=cc), timeout=300)
        g_names, g_progs = parse_guide(xml) if xml else ({}, {})
        # index guide channels by name keys; prefer the one with most programmes
        index = {}
        for gid, ns in g_names.items():
            n = len(g_progs.get(gid, []))
            if n == 0:
                continue
            for k in name_keys(ns):
                if k not in index or n > index[k][1]:
                    index[k] = (gid, n)
        out, now, matched = [], {}, 0
        os.makedirs(f"{OUT}/g/{cc}", exist_ok=True)
        for c in sorted(chans, key=lambda c: c["name"].lower()):
            item = {
                "id": c["id"],
                "n": c["name"],
                "s": chan_streams[c["id"]][:6],
            }
            if c["id"] in logo:
                item["l"] = logo[c["id"]][1]
            if c.get("categories"):
                item["c"] = c["categories"]
            if langs.get(c["id"]):
                item["la"] = sorted(langs[c["id"]])
            if c.get("is_nsfw"):
                item["x"] = 1
            if c.get("website"):
                item["w"] = c["website"]
            gid = None
            for k in name_keys([c["name"]] + (c.get("alt_names") or [])):
                if k in index:
                    gid = index[k][0]
                    break
            if gid:
                progs = g_progs[gid]
                item["g"] = 1
                matched += 1
                with open(f"{OUT}/g/{cc}/{c['id']}.json", "w") as fh:
                    json.dump(progs, fh, separators=(",", ":"), ensure_ascii=False)
                window = [{"s": p["s"], "e": p["e"], "t": p["t"], **({"c": p["c"]} if "c" in p else {}),
                           **({"i": p["i"]} if "i" in p else {})}
                          for p in progs if p["e"] >= NOW_FROM.timestamp() and p["s"] <= NOW_TO.timestamp()]
                if window:
                    now[c["id"]] = window
            out.append(item)
        with open(f"{OUT}/c/{cc}.json", "w") as fh:
            json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
        with open(f"{OUT}/now/{cc}.json", "w") as fh:
            json.dump({"at": int(NOW.timestamp()), "p": now}, fh, separators=(",", ":"), ensure_ascii=False)
        info = country_names.get(cc, {})
        return cc, {"name": info.get("name", cc), "flag": info.get("flag", ""), "channels": len(out),
                    "guide": matched, "guideChannels": len(g_names)}

    os.makedirs(f"{OUT}/c", exist_ok=True)
    os.makedirs(f"{OUT}/now", exist_ok=True)
    report = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for cc, info in ex.map(build_country, codes):
            report[cc] = info
            print(f"  {cc}: {info['channels']} channels, guide for {info['guide']}")
    json.dump(report, open(f"{OUT}/countries.json", "w"), ensure_ascii=False, separators=(",", ":"))
    total = sum(r["channels"] for r in report.values())
    guided = sum(r["guide"] for r in report.values())
    json.dump({"built": int(NOW.timestamp()), "channels": total, "withGuide": guided,
               "seconds": int(time.time() - t0)}, open(f"{OUT}/report.json", "w"))
    print(f"Done: {total} channels, {guided} with guide, {int(time.time() - t0)} s")


if __name__ == "__main__":
    main()
