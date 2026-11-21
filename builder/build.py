#!/usr/bin/env python3
"""
Builds the data the Nuvio Live TV addon serves.

For every country:
  * public channels and streams from iptv-org (https://github.com/iptv-org/api)
  * extra public playlists (a validated list, Free-TV, a health-checked list), merged by channel
  * free streaming-service channels (Samsung TV Plus, Pluto TV, Roku) with their own guides
  * every stream is test-played once per build; working streams come first
  * free daily XMLTV guides from epg.pw and epgshare01, matched to channels by name
    (the two sources label channels differently, so ids alone never line up)

Output (written to ./out, published to the `data` branch):
  countries.json                 every country with channel / guide counts
  categories.json                category ids and names
  c/<cc>.json                    channels of one country (compact)
  now/<cc>.json                  programmes from 2 h ago to 14 h ahead, per channel
  g/<cc>/<channel-id>.json       full schedule of one channel (1 day back, 3 ahead)
  x/<cc>.xml.gz                  XMLTV guide using our channel ids (for TiviMate & co.)
  sources.json                   the stream sources and their counts
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
EPG_PW = {"AU", "BR", "CA", "CN", "DE", "FR", "GB", "HK", "ID", "IN", "JP", "MY", "NZ", "PH", "RU", "SG",
          "TW", "US", "VN", "ZA"}
SHARE = "https://epgshare01.online/epgshare01/"
SHARE_SKIP = {"ALL_SOURCES1", "DUMMY_CHANNELS"}
# iptv-org country code -> code the guide sites use
ALIAS = {"UK": "GB"}

# ---------------------------------------------------------------- stream sources
SOURCES = {
    "samsung": "Samsung TV Plus",
    "pluto": "Pluto TV",
    "roku": "Roku Channel",
    "freetv": "Free-TV",
    "validated": "Validated list",
    "online": "Health-checked list",
    "iptv": "iptv-org",
    "argentina": "Argentina (iptv-org M3U)",
}
PRIO = {"samsung": 6, "pluto": 6, "roku": 6, "freetv": 5, "validated": 4, "online": 4, "iptv": 1, "argentina": 2}
M3U_SOURCES = [
    ("validated", "https://raw.githubusercontent.com/AdeelWajid/iptv_validated_list/main/playlist.m3u"),
    ("freetv", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"),
    ("online", "https://dearbulut.github.io/iptv/playlists/online.m3u"),
    ("argentina", "https://iptv-org.github.io/iptv/countries/ar.m3u"),
]
MJH = "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/"
FAST = [
    # key, folder, stream url pattern
    ("samsung", "SamsungTVPlus", "https://jmp2.uk/stvp-{id}"),
    ("pluto", "PlutoTV", "https://jmp2.uk/plu-{id}.m3u8"),
    ("roku", "Roku", "https://jmp2.uk/rok-{id}.m3u8"),
]
FAST_GROUPS = {
    "hindi news": ("news", "hin"), "english news": ("news", "eng"), "regional news": ("news", None),
    "news": ("news", None), "news + opinion": ("news", None), "local news": ("news", None),
    "movies": ("movies", None), "entertainment": ("entertainment", None), "music": ("music", None),
    "lifestyle": ("lifestyle", None), "infotainment": ("documentary", None), "documentaries": ("documentary", None),
    "sports": ("sports", None), "kids": ("kids", None), "kids & family": ("kids", None),
    "devotional": ("religious", None), "comedy": ("comedy", None), "reality": ("entertainment", None),
    "crime": ("entertainment", None), "drama": ("entertainment", None), "food": ("cooking", None),
    "home": ("lifestyle", None), "travel": ("travel", None), "anime": ("animation", None),
    "classic tv": ("classic", None), "sci-fi": ("entertainment", None), "game shows": ("entertainment", None),
}
LANG_WORDS = {"bhojpuri": "bho", "punjabi": "pan", "tamil": "tam", "telugu": "tel", "bangla": "ben", "bengali": "ben",
              "marathi": "mar", "gujarati": "guj", "kannada": "kan", "malayalam": "mal", "odia": "ori", "urdu": "urd"}
QUALITY = {"2160p": 6, "1440p": 5, "1080p": 4, "720p": 3, "576p": 2, "480p": 2, "360p": 1}


def parse_m3u(text):
    """[{attrs..., name, url, ua, ref}] from an M3U playlist."""
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            cur = {"attrs": attrs, "name": line.rsplit(",", 1)[-1].strip() if "," in line else attrs.get("tvg-name", "")}
        elif line.startswith("#EXTVLCOPT:http-user-agent=") and cur is not None:
            cur["ua"] = line.split("=", 1)[1]
        elif line.startswith("#EXTVLCOPT:http-referrer=") and cur is not None:
            cur["ref"] = line.split("=", 1)[1]
        elif line and not line.startswith("#") and cur is not None:
            cur["url"] = line
            out.append(cur)
            cur = None
    return out


def name_quality(name):
    m = re.search(r"\((\d{3,4}p)\)", name or "")
    return m.group(1) if m else None


def clean_name(name):
    return re.sub(r"\s*(\([^)]*\)|\[[^\]]*\])", "", name or "").strip()


def probe(st):
    """Test-plays a stream briefly: (works, quality)."""
    headers = {"User-Agent": st.get("a") or "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
    if st.get("r"):
        headers["Referer"] = st["r"]
    try:
        req = urllib.request.Request(st["u"], headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            data = r.read(65536)
    except Exception:
        return False, None
    head = data[:4000].decode("utf-8", "ignore")
    if head.lstrip().startswith("#EXTM3U"):
        res = [int(h) for _w, h in re.findall(r"RESOLUTION=(\d+)x(\d+)", head)]
        q = None
        if res:
            h = max(res)
            q = "2160p" if h >= 2000 else "1080p" if h >= 1000 else "720p" if h >= 700 else "576p" if h >= 560 else "480p" if h >= 440 else "360p"
        return ("#EXTINF" in head or "#EXT-X-STREAM-INF" in head or "#EXT-X-TARGETDURATION" in head), q
    if data[:1] == b"\x47" or "video" in ctype or "mpegurl" in ctype or "octet-stream" in ctype:
        return len(data) > 1000, None
    return False, None
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


def esc(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def xt(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


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
            ns = [d.text for d in el.findall("display-name") if d.text]
            if cid:  # ids like "Star.Plus.HD.in" are names too
                ns.append(re.sub(r"\.[a-z]{2}$", "", cid).replace(".", " "))
            names[cid] = ns
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
        st["o"] = "iptv"
        chan_streams.setdefault(cid, []).append(st)

    country_names = {c["code"]: c for c in countries}
    by_cname = {c["name"].lower(): c["code"] for c in countries}
    by_cname.update({"uk": "UK", "usa": "US", "united states": "US", "united kingdom": "UK"})
    cat_ids = {c["id"] for c in categories}

    # name index per country, to merge the same channel from different sources
    idx = {}
    for c in by_id.values():
        for k in name_keys([c["name"]] + (c.get("alt_names") or [])):
            idx.setdefault(c["country"], {}).setdefault(k, c["id"])

    def add_stream(cid, st):
        lst = chan_streams.setdefault(cid, [])
        if any(x["u"] == st["u"] for x in lst):
            for x in lst:  # same link listed by several sources: keep the best origin
                if x["u"] == st["u"] and PRIO[st["o"]] > PRIO[x.get("o", "iptv")]:
                    x["o"] = st["o"]
            return
        lst.append(st)

    def new_channel(cid, name, cc, cats=None, logo_url=None, la=None):
        if cid not in by_id:
            by_id[cid] = {"id": cid, "name": name, "country": cc, "categories": cats or [], "alt_names": []}
            if logo_url:
                logo[cid] = (0, logo_url)
            if la:
                langs.setdefault(cid, set()).add(la)
            for k in name_keys([name]):
                idx.setdefault(cc, {}).setdefault(k, cid)
        return cid

    def slug(name):
        return re.sub(r"[^A-Za-z0-9]+", "", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()) or "ch"

    source_counts = {k: 0 for k in SOURCES}
    source_counts["iptv"] = sum(len(v) for v in chan_streams.values())

    # 1) extra public playlists
    for key, url in M3U_SOURCES:
        raw = get(url, timeout=120)
        if not raw:
            print(f"  ! source {key} unavailable")
            continue
        entries = parse_m3u(raw.decode("utf-8", "ignore"))
        added = 0
        for e in entries:
            a = e["attrs"]
            tid = (a.get("tvg-id") or "").split("@")[0].strip()
            cid = tid if tid in by_id else None
            name = clean_name(e["name"]) or clean_name(a.get("tvg-name"))
            if not name or not e.get("url", "").startswith("http"):
                continue
            if cid:
                cc = by_id[cid]["country"]
            else:
                cc = (a.get("tvg-country") or "").split(";")[0].strip().upper()[:2]
                if not cc and re.search(r"\.[a-z]{2}$", tid):
                    cc = tid[-2:].upper()
                if not cc:
                    cc = by_cname.get((a.get("group-title") or "").strip().lower())
                cc = {"GB": "UK"}.get(cc, cc)
                if not cc or cc not in country_names:
                    continue
                for k in name_keys([name]):
                    if k in idx.get(cc, {}):
                        cid = idx[cc][k]
                        break
                if not cid:
                    group = [g.strip().lower() for g in (a.get("group-title") or "").split(";")]
                    cid = new_channel(f"{slug(name)}.{cc.lower()}", name, cc, [g for g in group if g in cat_ids], a.get("tvg-logo"))
            st = {"u": e["url"], "o": key}
            q = name_quality(e["name"])
            if q:
                st["q"] = q
            if e.get("ua"):
                st["a"] = e["ua"]
            if e.get("ref"):
                st["r"] = e["ref"]
            add_stream(cid, st)
            added += 1
        source_counts[key] = added
        print(f"  source {key}: {added} streams")

    # 2) free streaming services, with their own exact guides
    fast_guide = {}   # channel id -> (folder, region, service channel id)
    for key, folder, pattern in FAST:
        raw = get(MJH + folder + "/.channels.json.gz", timeout=120)
        if not raw:
            print(f"  ! source {key} unavailable")
            continue
        d = json.loads(raw)
        ua = (d.get("headers") or {}).get("user-agent")
        regions = d.get("regions") or {"us": {"channels": d.get("channels", {})}}
        if "channels" in d and isinstance(d.get("channels"), dict) and folder == "Roku":
            regions = {"all": {"channels": d["channels"]}}
        added = 0
        for rcc, reg in regions.items():
            cc = "US" if rcc == "all" else {"GB": "UK"}.get(rcc.upper(), rcc.upper())
            if cc not in country_names:
                continue
            for fid, ch in (reg.get("channels") or {}).items():
                name = (ch.get("name") or "").strip()
                if not name:
                    continue
                groups = [ch.get("group")] if ch.get("group") else list(ch.get("groups") or [])
                cat, la = None, None
                for g in groups:
                    m = FAST_GROUPS.get((g or "").strip().lower())
                    if m:
                        cat, la = m
                        break
                low = name.lower()
                for w, code in LANG_WORDS.items():
                    if w in low:
                        la = code
                cid = None
                for k in name_keys([name]):
                    if k in idx.get(cc, {}):
                        cid = idx[cc][k]
                        break
                if not cid:
                    cid = new_channel(f"{key}-{fid}", name, cc, [cat] if cat else [], ch.get("logo"), la)
                elif la:
                    langs.setdefault(cid, set()).add(la)
                st = {"u": pattern.format(id=fid), "o": key, "q": "HD"}
                if ua:
                    st["a"] = ua
                add_stream(cid, st)
                fast_guide.setdefault(cid, (folder, "all" if rcc == "all" else rcc, fid))
                added += 1
        source_counts[key] = added
        print(f"  source {key}: {added} channels")

    # 3) test-play every link once (streaming-service links are trusted)
    todo = {}
    for lst in chan_streams.values():
        for st in lst:
            if st.get("o") in ("samsung", "pluto", "roku"):
                st["k"] = 1
            else:
                todo.setdefault(st["u"], st)
    print(f"Checking {len(todo)} links…")
    t1 = time.time()
    results = {}
    with cf.ThreadPoolExecutor(160) as ex:
        for u, (ok, q) in zip(todo, ex.map(probe, todo.values())):
            results[u] = (ok, q)
    working = sum(1 for ok, _ in results.values() if ok)
    print(f"  {working} of {len(todo)} links work ({int(time.time() - t1)} s)")
    for lst in chan_streams.values():
        for st in lst:
            if st["u"] in results:
                ok, q = results[st["u"]]
                st["k"] = 1 if ok else 0
                if q:
                    st["q"] = q
        # working first, then the sharpest picture, then the most dependable source
        lst.sort(key=lambda st: (-st.get("k", 0), -QUALITY.get(st.get("q", ""), 3.5 if st.get("q") == "HD" else 0), -PRIO.get(st.get("o"), 0)))
    json.dump({k: {"name": v, "streams": source_counts.get(k, 0)} for k, v in SOURCES.items()},
              open(f"{OUT}/sources.json", "w"), ensure_ascii=False)

    per_country = {}
    for cid, sts in chan_streams.items():
        c = by_id[cid]
        per_country.setdefault(c["country"], []).append(c)

    json.dump({c["id"]: c["name"] for c in categories}, open(f"{OUT}/categories.json", "w"))

    codes = sorted(per_country)
    print(f"Countries with streams: {len(codes)}")

    # guide sources: epg.pw per country, epgshare01 per country plus brand-wide files
    share_files = []
    idx = get(SHARE, timeout=60)
    if idx:
        share_files = sorted(set(re.findall(r'epg_ripper_([A-Za-z0-9_.-]+?)\.xml\.gz', idx.decode("utf-8", "ignore"))))
    print(f"  epgshare01 files: {len(share_files)}")
    share_by_cc, share_global = {}, []
    for f in share_files:
        if f in SHARE_SKIP:
            continue
        m = re.fullmatch(r"([A-Z]{2})\d*", f)
        if m:
            share_by_cc.setdefault(m.group(1), []).append(f)
        else:
            share_global.append(f)

    def load(urls):
        """Parse several XMLTV files into one {key: (gid, n)} index plus programmes."""
        names, progs = {}, {}
        for i, url in enumerate(urls):
            xml = get(url, timeout=300)
            if not xml:
                continue
            try:
                n, p = parse_guide(xml)
            except Exception as e:
                print(f"  ! bad guide {url}: {e}", file=sys.stderr)
                continue
            for gid, ns in n.items():
                names[f"{i}|{gid}"] = ns
            for gid, ps in p.items():
                progs[f"{i}|{gid}"] = ps
        index = {}
        for gid, ns in names.items():
            n = len(progs.get(gid, []))
            if n == 0:
                continue
            for k in name_keys(ns):
                if k not in index or n > index[k][1]:
                    index[k] = (gid, n)
        return index, progs, len(names)

    print(f"Loading {len(share_global)} brand-wide guides…")
    g_index, g_all, _ = load([SHARE + f"epg_ripper_{f}.xml.gz" for f in share_global])
    print(f"  brand-wide guide channels: {len(g_index)} keys")

    fast_xml = {}
    need = sorted({(f, r) for f, r, _ in fast_guide.values()})
    print(f"Loading {len(need)} streaming-service guides…")

    def load_fast(fr):
        folder, region = fr
        raw = get(MJH + f"{folder}/{region}.xml.gz", timeout=300)
        if not raw:
            return fr, {}
        try:
            return fr, parse_guide(raw)[1]
        except Exception as e:
            print(f"  ! bad guide {folder}/{region}: {e}", file=sys.stderr)
            return fr, {}
    with cf.ThreadPoolExecutor(6) as ex:
        for fr, progs in ex.map(load_fast, need):
            fast_xml[fr] = progs

    def build_country(cc):
        chans = per_country[cc]
        gc = ALIAS.get(cc, cc)
        urls = ([EPG.format(cc=gc)] if gc in EPG_PW else []) + \
               [SHARE + f"epg_ripper_{f}.xml.gz" for f in share_by_cc.get(gc, []) + share_by_cc.get(cc, [])]
        urls = list(dict.fromkeys(urls))
        index, g_progs, g_count = load(urls)
        lc = cc.lower()
        out, now, matched = [], {}, 0
        xml_ch, xml_pr = [], []
        os.makedirs(f"{OUT}/g/{lc}", exist_ok=True)
        for c in sorted(chans, key=lambda c: c["name"].lower()):
            item = {
                "id": c["id"],
                "n": c["name"],
                "s": chan_streams[c["id"]][:6],
            }
            item["o"] = sorted({st.get("o", "iptv") for st in chan_streams[c["id"]]}, key=lambda o: -PRIO.get(o, 0))
            if any(st.get("k") for st in chan_streams[c["id"]]):
                item["v"] = 1
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
            gid, progs = None, None
            fg = fast_guide.get(c["id"])
            if fg and fast_xml.get(fg[:2], {}).get(fg[2]):
                gid, progs = fg[2], fast_xml[fg[:2]][fg[2]]
            keys = name_keys([c["name"]] + (c.get("alt_names") or []))
            for k in ([] if gid else keys):
                if k in index:
                    gid = index[k][0]
                    progs = g_progs[gid]
                    break
            if not gid:
                for k in keys:
                    if k in g_index and len(k) >= 4:
                        gid = g_index[k][0]
                        progs = g_all[gid]
                        break
            if gid:
                item["g"] = 1
                matched += 1
                with open(f"{OUT}/g/{lc}/{c['id']}.json", "w") as fh:
                    json.dump(progs, fh, separators=(",", ":"), ensure_ascii=False)
                window = [{"s": p["s"], "e": p["e"], "t": p["t"], **({"c": p["c"]} if "c" in p else {}),
                           **({"i": p["i"]} if "i" in p else {})}
                          for p in progs if p["e"] >= NOW_FROM.timestamp() and p["s"] <= NOW_TO.timestamp()]
                if window:
                    now[c["id"]] = window
                xml_ch.append(f'<channel id="{esc(c["id"])}"><display-name>{esc(c["name"])}</display-name>'
                              + (f'<icon src="{esc(item["l"])}"/>' if item.get("l") else "") + "</channel>")
                for p in progs:
                    xml_pr.append(
                        f'<programme start="{xt(p["s"])}" stop="{xt(p["e"])}" channel="{esc(c["id"])}">'
                        f'<title>{esc(p["t"])}</title>'
                        + (f'<sub-title>{esc(p["st"])}</sub-title>' if p.get("st") else "")
                        + (f'<desc>{esc(p["d"])}</desc>' if p.get("d") else "")
                        + "".join(f"<category>{esc(x)}</category>" for x in p.get("c", []))
                        + (f'<icon src="{esc(p["i"])}"/>' if p.get("i") else "")
                        + "</programme>")
            out.append(item)
        os.makedirs(f"{OUT}/x", exist_ok=True)
        with gzip.open(f"{OUT}/x/{lc}.xml.gz", "wt", encoding="utf-8") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="nuvio-livetv">')
            fh.write("".join(xml_ch))
            fh.write("".join(xml_pr))
            fh.write("</tv>")
        with open(f"{OUT}/c/{lc}.json", "w") as fh:
            json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
        with open(f"{OUT}/now/{lc}.json", "w") as fh:
            json.dump({"at": int(NOW.timestamp()), "p": now}, fh, separators=(",", ":"), ensure_ascii=False)
        info = country_names.get(cc, {})
        return lc, {"name": info.get("name", cc), "flag": info.get("flag", ""), "channels": len(out),
                    "working": sum(1 for i in out if i.get("v")),
                    "guide": matched, "guideChannels": g_count, "sources": len(urls)}

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
