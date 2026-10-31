#!/usr/bin/env python3
"""Collect rental listings from several UK property sites into one feed.

Reads config.json, fetches search results from Rightmove, OnTheMarket and
OpenRent, reads each listing's full description to detect parking, double
glazing and wood/laminate floors, merges listings that appear on more than
one site, scores them and writes web/listings.json for the web page.

Standard library only. Run: python3 scrape.py
"""
import html as htmllib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
OUT = ROOT / "web" / "listings.json"
CACHE_DIR = ROOT / ".cache"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-GB"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def page_text(h):
    h = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", h, flags=re.S)
    h = re.sub(r"<br\s*/?>|</(p|li|div|h\d)>", "\n", h)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t))


def next_data(h):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    return json.loads(m.group(1)) if m else None


def km(a, b):
    (lat1, lon1), (lat2, lon2) = a, b
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


# ---------- feature detection ----------

# Parking levels, best first. "mentioned" means the listing says parking
# without saying what kind, so it is worth asking about.
PARKING_LEVELS = ("offstreet", "mentioned", "permit", "unknown", "none")
PARKING_OFFSTREET = re.compile(
    r"off[- ]street|allocated (car )?parking|parking space|secure parking|"
    r"underground (car )?parking|private parking|gated parking|on[- ]site parking|"
    r"motorbike|motorcycle|\bgarage\b|driveway|car port|carport")
PARKING_PERMIT = re.compile(r"permit parking|residents'? parking|on[- ]street parking|parking permit")
PARKING_ANY = re.compile(r"\bparking\b")
PARKING_NO = re.compile(
    r"car[- ]free|permit[- ]free|no parking|not eligible for (a )?(parking )?permit|"
    r"parking:? (not available|none)")
GLAZING = re.compile(r"double[- ]glaz|triple[- ]glaz|secondary glaz")
FLOORS = re.compile(
    r"wood(en)? floor|hard ?wood|laminate|oak floor|engineered (wood|oak|floor)|"
    r"parquet|timber floor|wood[- ]effect floor")
CARPET = re.compile(r"carpet")


def context(t, m, pad=45):
    """The words around a regex match, so the page can show why a flag was set."""
    a, b = max(m.start() - pad, 0), min(m.end() + pad, len(t))
    return ("…" if a else "") + re.sub(r"\s+", " ", t[a:b]).strip() + ("…" if b < len(t) else "")


def detect(text):
    t = text.lower()
    parking, pm = "unknown", None
    for level, rx in (("none", PARKING_NO), ("offstreet", PARKING_OFFSTREET),
                      ("permit", PARKING_PERMIT), ("mentioned", PARKING_ANY)):
        pm = rx.search(t)
        if pm:
            parking = level
            break
    gl, fl = GLAZING.search(t), FLOORS.search(t)
    evidence = {}
    for k, m in (("parking", pm), ("glazing", gl), ("floors", fl)):
        if m:
            evidence[k] = context(text, m)
    return {
        "parking": parking,
        "glazing": bool(gl),
        "floors": bool(fl),
        "carpet": bool(CARPET.search(t)),
        "evidence": evidence,
    }


POSTCODE_DISTRICT = re.compile(r"\b(EC\d+[A-Z]?|WC\d+[A-Z]?|NW\d+|N\d+|E\d+)\b")


def snippet(text, n=420):
    t = re.sub(r"\s+", " ", text).strip()
    return t[:n] + ("…" if len(t) > n else "")


# ---------- detail cache ----------

def load_cache():
    CACHE_DIR.mkdir(exist_ok=True)
    f = CACHE_DIR / "details.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_cache(c):
    (CACHE_DIR / "details.json").write_text(json.dumps(c))


def detail(cache, key, url, extract):
    if key in cache:
        return cache[key]
    time.sleep(CONFIG["detail_delay_seconds"])
    try:
        cache[key] = extract(fetch(url))
    except Exception as e:  # keep going; the listing still shows without details
        print(f"  detail failed {key}: {e}", file=sys.stderr)
        cache[key] = {"text": "", "error": True}
    return cache[key]


# ---------- Rightmove ----------

def rightmove(cache):
    base = "https://www.rightmove.co.uk/property-to-rent/find.html?" + urllib.parse.urlencode({
        "locationIdentifier": CONFIG["rightmove_location"],
        "minBedrooms": CONFIG["min_beds"], "maxBedrooms": CONFIG["max_beds"],
        "minPrice": CONFIG["min_price"], "maxPrice": CONFIG["max_price"],
        "propertyTypes": "flat", "dontShow": "houseShare,retirement,student",
        "radius": "0.0", "sortType": "6"})
    out, index = [], 0
    while True:
        d = next_data(fetch(f"{base}&index={index}"))
        sr = d["props"]["pageProps"]["searchResults"]
        for p in sr["properties"]:
            if p.get("students") or p.get("commercial"):
                continue
            url = "https://www.rightmove.co.uk/properties/%s" % p["id"]

            def extract(h):
                t = page_text(h)
                start = t.find("Key features")
                start = start if start >= 0 else t.find("Description")
                end = t.find("PARKING", start)
                body = t[start:end if end > 0 else start + 6000]
                pk = re.search(r"PARKING .*?glossary page\.\s*(.+?)\n", t)
                return {"text": body, "parking_field": pk.group(1).strip() if pk else ""}

            det = detail(cache, f"rm-{p['id']}", url, extract)
            imgs = p.get("images") or []
            out.append({
                "source": "Rightmove", "id": str(p["id"]), "url": url,
                "title": f"{p['bedrooms']} bed {p.get('propertySubType') or 'flat'}".strip(),
                "address": p.get("displayAddress", ""),
                "price": p["price"]["amount"] if p["price"]["frequency"] == "monthly"
                else round(p["price"]["amount"] * 52 / 12),
                "beds": p.get("bedrooms") or 0,
                "lat": p["location"]["latitude"], "lng": p["location"]["longitude"],
                "image": imgs[0]["srcUrl"] if imgs else "",
                "agent": (p.get("customer") or {}).get("branchDisplayName", ""),
                "added": (p.get("listingUpdate") or {}).get("listingUpdateDate", ""),
                "available": p.get("letAvailableDate") or "",
                "summary": p.get("summary", ""),
                "text": det.get("text", ""), "parking_field": det.get("parking_field", ""),
            })
        nxt = sr["pagination"].get("next")
        if not nxt or int(nxt) <= index:
            break
        index = int(nxt)
        time.sleep(1)
    return out


# ---------- OnTheMarket ----------

def onthemarket(cache):
    out, page = [], 1
    while page <= 10:
        q = {"min-bedrooms": CONFIG["min_beds"], "max-bedrooms": CONFIG["max_beds"],
             "min-price": CONFIG["min_price"], "max-price": CONFIG["max_price"]}
        if page > 1:
            q["page"] = page
        url = (f"https://www.onthemarket.com/to-rent/property/{CONFIG['onthemarket_area']}/?"
               + urllib.parse.urlencode(q))
        d = next_data(fetch(url))
        lst = d["props"]["pageProps"]["initialReduxState"]["results"]["list"]
        if not lst:
            break
        new = 0
        for p in lst:
            ptype = (p.get("humanised-property-type") or "").lower()
            if any(w in ptype for w in ("house", "room", "bungalow")):
                continue
            if any(o["id"] == str(p["id"]) for o in out):
                continue
            new += 1
            durl = "https://www.onthemarket.com/details/%s/" % p["id"]

            def extract(h):
                t = page_text(h)
                start = t.find("Key features")
                start = start if start >= 0 else t.find("Description")
                return {"text": t[max(start, 0):max(start, 0) + 6000]}

            det = detail(cache, f"otm-{p['id']}", durl, extract)
            price = int(re.sub(r"[^\d]", "", (p.get("short-price") or "0")) or 0)
            img = (p.get("cover-image") or {}) if isinstance(p.get("cover-image"), dict) else {}
            images = p.get("images") or []
            out.append({
                "source": "OnTheMarket", "id": str(p["id"]), "url": durl,
                "title": p.get("property-title", ""), "address": p.get("address", ""),
                "price": price, "beds": p.get("bedrooms") or 0,
                "lat": p["location"]["lat"], "lng": p["location"]["lon"],
                "image": img.get("default") or (images[0].get("default") if images and isinstance(images[0], dict) else ""),
                "agent": (p.get("agent") or {}).get("name", ""),
                "added": "", "available": "",
                "summary": " · ".join(p.get("features") or []),
                "text": " ".join(p.get("features") or []) + "\n" + det.get("text", ""),
                "parking_field": "",
            })
        if new == 0:
            break
        page += 1
        time.sleep(1)
    return out


# ---------- OpenRent ----------

def js_array(h, name):
    m = re.search(r"var %s = \[(.*?)\];" % name, h, re.S)
    if not m:
        return []
    body = re.sub(r",\s*$", "", m.group(1).strip()).replace("'", '"')
    body = body.replace("True", "true").replace("False", "false")
    return [float(v) if isinstance(v, str) and re.fullmatch(r"-?[\d.]+", v) else v
            for v in json.loads("[" + body + "]")]


def openrent(cache):
    url = (f"https://www.openrent.co.uk/properties-to-rent/{CONFIG['openrent_area']}?"
           + urllib.parse.urlencode({"prices_min": CONFIG["min_price"], "prices_max": CONFIG["max_price"],
                                     "bedrooms_min": CONFIG["min_beds"], "bedrooms_max": CONFIG["max_beds"]}))
    h = fetch(url)
    ids = js_array(h, "PROPERTYIDS")
    cols = {n: js_array(h, n) for n in ("prices", "bedrooms", "parkings", "isshared", "isstudio",
                                         "PROPERTYLISTLATITUDES", "PROPERTYLISTLONGITUDES",
                                         "PROPERTYLISTCOMMUTEORDISTANCE", "dateFirstListedMs",
                                         "students", "islivelistBool")}
    out = []
    for i, pid in enumerate(ids):
        col = lambda n: cols[n][i] if i < len(cols[n]) else None
        price, beds = col("prices"), col("bedrooms")
        price, beds = (int(price) if price is not None else None), (int(beds) if beds is not None else None)
        if price is None or not (CONFIG["min_price"] <= price <= CONFIG["max_price"]):
            continue
        if beds is None or not (CONFIG["min_beds"] <= beds <= CONFIG["max_beds"]):
            continue
        if col("isshared") or col("isstudio") or col("islivelistBool") == 0:
            continue
        dist = col("PROPERTYLISTCOMMUTEORDISTANCE")
        if dist is not None and dist > CONFIG["openrent_max_km"]:
            continue
        durl = "https://www.openrent.co.uk/%s" % pid

        def extract(h):
            t = page_text(h)
            m = re.search(r"<title>(.*?)</title>", h, re.S)
            title = htmllib.unescape(m.group(1)).split("|")[0] if m else ""
            title = re.sub(r"\s+-\s+To Rent.*$", "", re.sub(r"^\s*London\s+-\s+", "", title)).strip()
            im = re.search(r'(//imagescdn\.openrent\.co\.uk/listings/\d+/[^"\s]+?\.(?:jpe?g|png))', h, re.I)
            start = max(t.find("Description"), 0)
            end = re.search(r"\bFeatures\s+Garden\s+Parking|Nearby Schools", t[start:])
            return {"text": t[start:start + end.start() if end else None][:6000], "title": title,
                    "image": "https:" + im.group(1) if im else ""}

        det = detail(cache, f"or-{pid}", durl, extract)
        listed = col("dateFirstListedMs")
        title = det.get("title", "")
        out.append({
            "source": "OpenRent", "id": str(pid), "url": durl,
            "title": title.split(",")[0] if title else f"{beds} bed flat",
            "address": ", ".join(title.split(",")[1:]).strip() if "," in title else "",
            "price": price, "beds": beds,
            "lat": col("PROPERTYLISTLATITUDES"), "lng": col("PROPERTYLISTLONGITUDES"),
            "image": det.get("image", ""), "agent": "Landlord (OpenRent)",
            "added": datetime.fromtimestamp(listed / 1000, timezone.utc).isoformat() if listed else "",
            "available": "", "summary": "",
            "text": det.get("text", ""),
            "parking_field": "Parking listed" if col("parkings") else "",
        })
    return out


# ---------- merge + score ----------

def enrich(l):
    f = detect(l["summary"] + "\n" + l["text"])
    pf = (l.get("parking_field") or "").lower()
    if l["source"] == "OpenRent" and pf and f["parking"] in ("unknown", "mentioned"):
        f["parking"] = "mentioned"
        f["evidence"]["parking"] = "Parking ticked in the OpenRent listing"
    elif pf and "ask agent" not in pf:
        if re.search(r"\bno\b|none|not ", pf):
            f["parking"] = "none"
        elif re.search(r"permit|on street|residents", pf):
            f["parking"] = "permit"
        elif re.search(r"allocated|off street|garage|driveway|private|gated|underground", pf):
            f["parking"] = "offstreet"
        elif f["parking"] in ("unknown", "none"):
            f["parking"] = "mentioned"
        f["evidence"]["parking"] = "Listing's parking field: " + l["parking_field"]
    l.update(f)
    l["blurb"] = snippet(l["summary"] or l["text"])
    pc = POSTCODE_DISTRICT.search(l["address"].upper()) or POSTCODE_DISTRICT.search(l["text"][:3000])
    l["district"] = pc.group(1) if pc else ""
    del l["text"]
    return l


def score(l):
    s = {"offstreet": 40, "mentioned": 25, "permit": 10, "unknown": 0, "none": -30}[l["parking"]]
    s += 15 if l["glazing"] else 0
    s += 15 if l["floors"] else 0
    s += 10 if l["beds"] >= 2 else 0
    span = CONFIG["max_price"] - CONFIG["min_price"]
    s += round(10 * (CONFIG["max_price"] - l["price"]) / span) if span else 0
    return s


def merge(listings):
    groups = []
    for l in sorted(listings, key=lambda x: x["source"] != "Rightmove"):
        for g in groups:
            a = g[0]
            if (a["beds"] == l["beds"] and abs(a["price"] - l["price"]) <= 50
                    and a["lat"] and l["lat"] and km((a["lat"], a["lng"]), (l["lat"], l["lng"])) < 0.12
                    and l["source"] not in {o["source"] for o in g}):
                g.append(l)
                break
        else:
            groups.append([l])
    merged = []
    for g in groups:
        m = dict(g[0])
        m["key"] = f"{m['source'][:2].lower()}-{m['id']}"
        m["also"] = [{"source": o["source"], "url": o["url"], "price": o["price"]} for o in g[1:]]
        for o in g[1:]:
            if m["parking"] == "unknown" or (m["parking"] == "mentioned" and o["parking"] != "unknown"):
                m["parking"] = o["parking"]
                if "parking" in o["evidence"]:
                    m["evidence"]["parking"] = o["evidence"]["parking"]
            for k in ("glazing", "floors"):
                m[k] = m[k] or o[k]
            for k, v in o["evidence"].items():
                m["evidence"].setdefault(k, v)
            m["image"] = m["image"] or o["image"]
        m["score"] = score(m)
        merged.append(m)
    return merged


def shrink(f, width=360):
    """Re-encode a photo as a small JPEG when Pillow is installed; otherwise keep it as is."""
    try:
        from PIL import Image
    except ImportError:
        return
    with Image.open(f) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)))
        im.save(f, "JPEG", quality=68, optimize=True)


def thumbnails(listings):
    """Save one small photo per listing next to the page (the page cannot load remote images)."""
    img_dir = ROOT / "web" / "img"
    img_dir.mkdir(exist_ok=True)
    keep = set()
    for l in listings:
        name = re.sub(r"[^a-z0-9-]", "", l["key"]) + ".jpg"
        f = img_dir / name
        if l["image"] and not f.exists():
            try:
                req = urllib.request.Request(l["image"], headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    f.write_bytes(r.read())
                shrink(f)
                time.sleep(0.2)
            except Exception as e:
                print(f"  image failed {l['key']}: {e}", file=sys.stderr)
        if f.exists():
            l["thumb"] = "img/" + name
            keep.add(name)
    for f in img_dir.glob("*.jpg"):
        if f.name not in keep:
            f.unlink()


def main():
    cache = load_cache()
    prev = {}
    if OUT.exists():
        prev = {l["key"]: l.get("first_seen") for l in json.loads(OUT.read_text()).get("listings", [])}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found, status = [], {}
    for name, fn in (("Rightmove", rightmove), ("OnTheMarket", onthemarket), ("OpenRent", openrent)):
        try:
            got = fn(cache)
            found += got
            status[name] = {"ok": True, "count": len(got)}
            print(f"{name}: {len(got)}")
        except Exception as e:
            status[name] = {"ok": False, "error": str(e)[:200]}
            print(f"{name} failed: {e}", file=sys.stderr)
        save_cache(cache)
    listings = merge([enrich(l) for l in found])
    for l in listings:
        l["first_seen"] = prev.get(l["key"]) or now
    listings.sort(key=lambda l: -l["score"])
    thumbnails(listings)
    OUT.write_text(json.dumps({"updated": now, "criteria": CONFIG, "sources": status,
                               "listings": listings}, indent=1))
    embed(OUT.read_text())
    print(f"wrote {len(listings)} listings to {OUT}")


def embed(data):
    """Copy the feed into web/index.html so the page needs no second request to show it."""
    page = ROOT / "web" / "index.html"
    html = page.read_text()
    compact = json.dumps(json.loads(data), separators=(",", ":")).replace("</", "<\\/")
    block = ('<!-- FEED-DATA:START -->\n<script type="application/json" id="feed-data">'
             + compact + '</script>\n<!-- FEED-DATA:END -->')
    html = re.sub(r"<!-- FEED-DATA:START -->.*?<!-- FEED-DATA:END -->", lambda m: block, html, flags=re.S)
    page.write_text(html)


if __name__ == "__main__":
    main()
