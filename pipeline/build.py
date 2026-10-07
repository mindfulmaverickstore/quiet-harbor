#!/usr/bin/env python3
"""Builds our own copy of the places data.

Reads OpenStreetMap (through the Overpass API) and Overture Maps (through DuckDB),
fills in missing websites and phone numbers where a confident match exists, and
writes small map-tile files the app can download:

    dist/index.json            which tiles exist, and when they were built
    dist/tiles/<lat>_<lon>.json  every listed place in one 1 degree square

Usage:  python3 pipeline/build.py --pbf portugal.osm.pbf --out dist
        python3 pipeline/build.py --bbox=-9,41,-8,42 --out dist      (slow, uses the public Overpass server)

The OpenStreetMap extract (a .pbf file) comes from download.geofabrik.de. Reading a
file is far more reliable than asking the busy public Overpass server.
"""
import argparse, json, math, os, re, sys, time, unicodedata, urllib.parse, urllib.request
import duckdb

def load_rules():
    # The matching words live in pipeline/rules.json (private repo) or, on the public builder, in the MATCH_RULES secret.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
    return json.load(open(path)) if os.path.exists(path) else json.loads(os.environ["MATCH_RULES"])


RULES = load_rules()
K = RULES["k"]  # the word used in the source data

try:
    import osmium  # reads OpenStreetMap .pbf files
except ImportError:
    osmium = None

OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "map-data-pipeline/0.1"
OVERTURE_RELEASE = None  # filled from the latest release at run time

# Big chains with at least one matching item.
CHAINS = ["Burger King", "KFC", "Subway", "Taco Bell", "Chipotle", "Qdoba", "Del Taco", "Starbucks",
          "Tim Hortons", "A&W", "Pizza Hut", "Domino's", "Papa John's", "Panago", "Blaze Pizza", "Freshii",
          "The Chopped Leaf", "Panera Bread", "Sweetgreen", "White Castle", "Greggs", "Pret A Manger",
          "Wagamama", "Nando's", "itsu", "Leon", "PizzaExpress", "Zizzi", "Wahaca", "Costa", "Caffè Nero"]

# The OpenStreetMap tags the app uses; everything else is dropped to keep files small.
KEEP = ["name", "amenity", "shop", "cuisine", "opening_hours", "website", "contact:website", "phone",
        "contact:phone", "contact:instagram", "contact:facebook", "instagram", "facebook", "website:menu",
        "addr:housenumber", "addr:street", "addr:city", "addr:postcode", "addr:housename", "addr:country",
        "brand", "brand:wikidata", "check_date", f"check_date:diet:{K}", f"diet:{K}", "diet:gluten_free",
        "takeaway", "delivery", "outdoor_seating", "wheelchair"]


def overpass(query, tries=4):
    data = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": UA})
            return json.load(urllib.request.urlopen(req, timeout=300))["elements"]
        except Exception as e:
            wait = 15 * (attempt + 1)
            print(f"  Overpass busy ({e}); waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    return None


def tile_osm(w, s, e, n):
    bbox = f"{s},{w},{n},{e}"
    listed = overpass(f'[out:json][timeout:240];(nwr["diet:{K}"~"^(only|yes)$"]["name"]({bbox}););out center tags;')
    if listed is None:
        raise RuntimeError("could not read places")
    chains = overpass("[out:json][timeout:240];(" + "".join(
        f'nwr["brand"="{c}"]["name"]({bbox});' for c in CHAINS) + ");out center tags;")
    return listed, (chains or [])


def pbf_box(path):
    """The area an extract covers: [west, south, east, north], from the file's header."""
    with osmium.io.Reader(path, osmium.osm.osm_entity_bits.NOTHING) as r:
        box = r.header().box()
        bl, tr = box.bottom_left, box.top_right
        return [bl.lon, bl.lat, tr.lon, tr.lat]


def read_poly(url):
    """Reads a Geofabrik .poly outline: a list of rings, each a list of [lon, lat].
    Thinned to about 300 points a ring: it's only used to decide 'do we cover this spot?'."""
    text = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60).read().decode()
    rings, cur, hole = [], None, False
    lines = [l.strip() for l in text.splitlines() if l.strip()][1:]   # first line is the file's name
    for line in lines:
        if cur is None:
            if line == "END":
                continue                    # the final END that closes the file
            hole = line.startswith("!")     # a section name; "!" marks a hole (ignored: being generous is fine)
            cur = []
        elif line == "END":
            if not hole:
                rings.append(cur)
            cur = None
        else:
            lon, lat = line.split()[:2]
            cur.append([round(float(lon), 3), round(float(lat), 3)])
    rings = [r for r in rings if r]
    out = []
    for r in rings:
        step = max(1, len(r) // 300)
        out.append(r[::step])
    return out


def read_pbf(path):
    """Returns {(lat, lon): [elements]} for listed places and chains in the file."""
    wanted_chains = set(CHAINS)
    found = []

    class Handler(osmium.SimpleHandler):
        def _take(self, kind, obj, lat, lon):
            t = obj.tags
            name = t.get("name")
            if not name:
                return
            listed = t.get(f"diet:{K}") in ("only", "yes")
            chain = t.get("brand") in wanted_chains
            if listed or chain:
                found.append({"type": kind, "id": obj.id, "center": {"lat": lat, "lon": lon},
                              "tags": {tag.k: tag.v for tag in t}})

        def node(self, n):
            if n.location.valid():
                self._take("node", n, n.location.lat, n.location.lon)

        def way(self, w):
            lats, lons = [], []
            for nd in w.nodes:
                if nd.location.valid():
                    lats.append(nd.lat); lons.append(nd.lon)
            if lats:
                self._take("way", w, sum(lats) / len(lats), sum(lons) / len(lons))

    Handler().apply_file(path, locations=True)
    tiles = {}
    for el in found:
        key = (math.floor(el["center"]["lat"]), math.floor(el["center"]["lon"]))
        tiles.setdefault(key, []).append(el)
    return tiles


def norm(t):
    t = unicodedata.normalize("NFD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", t)).strip()


STOP = {"restaurant", "restaurante", "cafe", "bar", "the", "a", "o", "da", "de", "do", "and", "e", "pizzeria", "bakery", "padaria"}


def toks(t):
    return {x for x in norm(t).split() if x not in STOP}


def sim(a, b):
    A, B = toks(a), toks(b)
    j = len(A & B) / len(A | B) if A and B else 0
    na, nb = norm(a), norm(b)
    if na and nb and (na == nb or na in nb or nb in na):
        j = max(j, 0.9)
    return j


def metres(a, b, c, d):
    p = math.pi / 180
    return 6371000 * math.hypot((c - a) * p * math.cos((b + d) / 2 * p), (d - b) * p)


# Links that are not a restaurant's own website: map pages, link shorteners from
# Google, delivery apps. They work if tapped, but they aren't what the Website
# button promises, so we drop them.
JUNK_URL = re.compile(
    r"(google\.[a-z.]+/maps|maps\.google|maps\.app\.goo\.gl|goo\.gl/|g\.co/|g\.page|"
    r"doordash\.com|ubereats\.com|grubhub\.com|just-eat|deliveroo|skipthedishes|menulog|"
    r"foodpanda|wolt\.com|opentable\.|tripadvisor\.)",
    re.I,
)


def clean_url(url):
    """Returns (kind, url): kind is 'website', 'instagram', 'facebook', or None for junk."""
    if not url:
        return (None, None)
    u = url.strip()
    if not re.match(r"https?://", u, re.I):
        u = "http://" + u
    low = u.lower()
    if "instagram.com" in low:
        return ("instagram", u)
    if "facebook.com" in low or "fb.com" in low:
        return ("facebook", u)
    if JUNK_URL.search(low):
        return (None, None)
    return ("website", u)


def overture_rows(con, w, s, e, n):
    """Overture food places in a box, with everything we use from them."""
    q = f"""
    select names.primary as name, websites[1] as website, phones[1] as phone, socials as socials,
           list_distinct([x.dataset for x in sources]) as src, bbox.xmin as lon, bbox.ymin as lat,
           taxonomy.primary as cat, id as gers_id,
           addresses[1].freeform as street, addresses[1].locality as city, addresses[1].postcode as postcode
    from read_parquet('s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}/theme=places/type=place/*', hive_partitioning=1)
    where bbox.xmin between {w} and {e} and bbox.ymin between {s} and {n}
      and (taxonomy.hierarchy[1] = 'food_and_drink' or taxonomy.hierarchy[2] = 'food_and_beverage_store')
    """
    cols = ["name", "website", "phone", "socials", "src", "lon", "lat", "cat", "gers_id", "street", "city", "postcode"]
    return [dict(zip(cols, r)) for r in con.execute(q).fetchall()]


def source_name(src):
    # Overture merges several sources; credit the named one when there is one.
    others = [x for x in (src or []) if x != "Overture"]
    return (others[0] if others else "Overture")


CHAIN_NAMES = {c.lower() for c in CHAINS}


def make_place(el, ov, is_chain_query=False):
    t = el["tags"]
    lat = el.get("lat") or el["center"]["lat"]
    lon = el.get("lon") or el["center"]["lon"]
    tags = {k: t[k] for k in KEEP if k in t}
    fill = {}
    # Tidy what OpenStreetMap already has: move social links out of "website", drop junk links.
    for key in ("website", "contact:website"):
        if key in tags:
            kind, u = clean_url(tags[key])
            del tags[key]
            if kind == "website":
                tags["website"] = u
            elif kind in ("instagram", "facebook") and f"contact:{kind}" not in tags:
                tags[f"contact:{kind}"] = u
    has_web = "website" in tags
    has_ph = "phone" in tags or "contact:phone" in tags
    has_ig = "contact:instagram" in tags or "instagram" in tags
    has_fb = "contact:facebook" in tags or "facebook" in tags
    # Fill the gaps from the closest similarly named Overture place.
    best = None
    for o in ov:
        if abs(o["lat"] - lat) > 0.0015 or abs(o["lon"] - lon) > 0.0025:
            continue
        d = metres(lon, lat, o["lon"], o["lat"])
        sc = sim(t.get("name", ""), o["name"] or "")
        if d <= 150 and sc >= 0.5 and (best is None or (sc, -d) > (best[0], -best[1])):
            best = (sc, d, o)
    if best:
        o = best[2]
        who = source_name(o["src"])
        links = [clean_url(o["website"])] + [clean_url(x) for x in (o["socials"] or [])]
        for kind, u in links:
            if kind == "website" and not has_web:
                tags["website"] = u; fill["website"] = who; has_web = True
            elif kind == "instagram" and not has_ig:
                tags["contact:instagram"] = u; fill["instagram"] = who; has_ig = True
            elif kind == "facebook" and not has_fb:
                tags["contact:facebook"] = u; fill["facebook"] = who; has_fb = True
        if not has_ph and o["phone"]:
            tags["phone"] = o["phone"]; fill["phone"] = who
    place = {"id": f'{el["type"]}/{el["id"]}', "lat": round(lat, 6), "lon": round(lon, 6), "tags": tags}
    if fill:
        place["fill"] = fill   # which values came from outside OpenStreetMap, and from where
    return place


# Words in place names that point to listed or alt food, in several languages. Thai "jay"
# (เจ) is only counted as a whole word or after "food" (อาหาร), because it also starts common
# words (เจ๊ "boss lady", เจริญ "prosperity").
NAME_A = re.compile(RULES["name_a"])
NAME_B = re.compile(RULES["name_b"])


def unconfirmed_places(ov, places):
    """Overture places of one category that we don't already show.

    These are labelled as unconfirmed in the app: nobody on OpenStreetMap or a
    a visitor has confirmed them, and Overture carries no open/closed
    information, so some may have closed."""
    have = [(p["lat"], p["lon"], p["tags"].get("name", "")) for p in places]
    out = []
    for o in ov:
        if not o["name"] or o["name"].strip().lower() in CHAIN_NAMES:
            continue
        # Why we think it might be listed: Overture's own listed category, a alt listing (in much of
        # Asia this is where fully listed places end up), or a name that says listed.
        name_l = o["name"].lower()
        if o["cat"] == RULES["cat_a"]:
            basis = K
        elif NAME_A.search(name_l) and not re.search(rf"non[- ]{K}", name_l):
            basis = "name"
        elif o["cat"] == RULES["cat_b"] or NAME_B.search(name_l):
            basis = RULES["cat_b"].split("_")[0]
        else:
            continue
        dup = any(abs(la - o["lat"]) <= 0.002 and abs(lo - o["lon"]) <= 0.003
                  and metres(lo, la, o["lon"], o["lat"]) <= 150 and sim(nm, o["name"]) >= 0.5 for la, lo, nm in have)
        if dup:
            continue
        tags = {"name": o["name"], "amenity": "restaurant", "ov:basis": basis}
        fill = {K: source_name(o["src"])}
        for kind, u in [clean_url(o["website"])] + [clean_url(x) for x in (o["socials"] or [])]:
            if kind == "website" and "website" not in tags:
                tags["website"] = u; fill["website"] = fill[K]
            elif kind in ("instagram", "facebook") and f"contact:{kind}" not in tags:
                tags[f"contact:{kind}"] = u; fill[kind] = fill[K]
        if o["phone"]:
            tags["phone"] = o["phone"]; fill["phone"] = fill[K]
        if o["street"]: tags["addr:street"] = o["street"]
        if o["city"]: tags["addr:city"] = o["city"]
        if o["postcode"]: tags["addr:postcode"] = o["postcode"]
        # "ov/" plus Overture's id fits the 40 character limit on report ids.
        out.append({"id": f'ov/{o["gers_id"]}', "lat": round(o["lat"], 6), "lon": round(o["lon"], 6),
                    "tags": tags, "fill": fill, "unconfirmed": True})
    return out


def closed_ids():
    # Optional list of places to leave out. Only used when the two settings are provided.
    url, key = os.environ.get("LIST_URL"), os.environ.get("LIST_KEY")
    if not url or not key:
        return set()
    req = urllib.request.Request(url + "/rest/v1/closed_places?select=place_id", headers={"apikey": key, "User-Agent": UA})
    try:
        return {r["place_id"] for r in json.load(urllib.request.urlopen(req, timeout=30))}
    except Exception as ex:
        print(f"WARNING: couldn't read the exclusion list ({ex})")
        return set()


def apply_closed(out):
    # Quick mode: drop the closed places from the finished tiles. No re-download, takes seconds.
    closed = closed_ids()
    print(f"{len(closed)} places marked closed")
    index_path = os.path.join(out, "index.json")
    index = json.load(open(index_path))
    changed = []
    for name in sorted(os.listdir(os.path.join(out, "tiles"))):
        path = os.path.join(out, "tiles", name)
        places = json.load(open(path))["places"]
        keep = [p for p in places if p["id"] not in closed]
        if len(keep) != len(places):
            # Holding bin: keep a copy of every removed place, so a mistake can be undone at once.
            os.makedirs(HOLDING, exist_ok=True)
            bin_path = os.path.join(HOLDING, name)
            held = {p["id"]: p for p in places if p["id"] in closed}
            if os.path.exists(bin_path):
                held = {**{p["id"]: p for p in json.load(open(bin_path))["places"]}, **held}
            with open(bin_path, "w") as f:
                json.dump({"places": list(held.values())}, f, ensure_ascii=False)
            with open(path, "w") as f:
                json.dump({"places": keep}, f, separators=(",", ":"), ensure_ascii=False)
            index["tiles"][name[:-5]] = len(keep)
            changed.append(name)
            print(f"{name}: removed {len(places) - len(keep)}")
    if changed:
        index["built"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        json.dump(index, open(index_path, "w"), indent=1)
    with open(os.path.join(out, "changed.txt"), "w") as f:
        f.write("\n".join(changed))


def main():
    global OVERTURE_RELEASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox")
    ap.add_argument("--pbf")
    ap.add_argument("--from-tiles", action="store_true",
                    help="Re-process tiles already in --out (no OpenStreetMap download): re-fill from Overture, "
                         "clean links, add the unconfirmed layer.")
    ap.add_argument("--poly", help="URL of the country outline, e.g. https://download.geofabrik.de/europe/portugal.poly")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--apply-closed", action="store_true", help="only remove places marked closed from the finished tiles")
    args = ap.parse_args()
    if args.apply_closed:
        return apply_closed(args.out)
    cat = json.load(urllib.request.urlopen("https://stac.overturemaps.org/catalog.json", timeout=30))
    OVERTURE_RELEASE = cat["latest"]
    print("Overture release", OVERTURE_RELEASE)
    con = duckdb.connect()
    con.execute("install httpfs; load httpfs; set s3_region='us-west-2';")
    os.makedirs(os.path.join(args.out, "tiles"), exist_ok=True)
    # Re-running for another country adds to what's already in the output folder.
    index_path = os.path.join(args.out, "index.json")
    CLOSED = closed_ids()
    index = {"built": "", "overture": OVERTURE_RELEASE, "covered": [], "tiles": {}}
    if os.path.exists(index_path):
        index.update(json.load(open(index_path)))
    index["built"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    index["overture"] = OVERTURE_RELEASE
    if args.from_tiles:
        # Rebuild each element from the tile: drop values that earlier came from Overture, so they are re-matched.
        work = []
        for name in sorted(os.listdir(os.path.join(args.out, "tiles"))):
            lat, lon = (int(x) for x in name[:-5].split("_"))
            els = []
            for p in json.load(open(os.path.join(args.out, "tiles", name)))["places"]:
                if p.get("unconfirmed"):
                    continue
                tags = dict(p["tags"])
                for field, tag in (("website", "website"), ("phone", "phone"), ("instagram", "contact:instagram"), ("facebook", "contact:facebook")):
                    if field in p.get("fill", {}):
                        tags.pop(tag, None)
                kind, osm_id = p["id"].split("/")
                els.append({"type": kind, "id": osm_id, "center": {"lat": p["lat"], "lon": p["lon"]}, "tags": tags})
            work.append((lat, lon, els))
        index["covered"] = index.get("covered", [])
    elif args.pbf:
        print("reading", args.pbf)
        t0 = time.time()
        tiles = read_pbf(args.pbf)
        print(f"read {sum(len(v) for v in tiles.values())} places in {time.time()-t0:.0f}s")
        work = [(lat, lon, els) for (lat, lon), els in sorted(tiles.items())]
        # Coverage is the country outline (a box would also claim the neighbours).
        outline = read_poly(args.poly) if args.poly else None
        index["covered"].append({"id": os.path.basename(args.pbf), "rings": outline} if outline
                                else {"id": os.path.basename(args.pbf), "box": [round(v, 4) for v in pbf_box(args.pbf)]})
    else:
        W, S, E, N = (int(float(x)) for x in args.bbox.split(","))
        index["covered"].append({"id": "bbox", "box": [W, S, E, N]})
        work = []
        for lat in range(S, N):
            for lon in range(W, E):
                try:
                    listed, chains = tile_osm(lon, lat, lon + 1, lat + 1)
                except Exception as ex:
                    print(f"tile {lat}_{lon}: skipped ({ex})"); continue
                work.append((lat, lon, listed + chains))
    for lat, lon, els in work:
        t0 = time.time()
        ov = overture_rows(con, lon, lat, lon + 1, lat + 1)
        seen, places = set(), []
        for el in els:
            pid = f'{el["type"]}/{el["id"]}'
            if pid in seen or "name" not in el["tags"]:
                continue
            seen.add(pid)
            places.append(make_place(el, ov, False))
        places += unconfirmed_places(ov, places)
        places = [p for p in places if p["id"] not in CLOSED]
        tile_path = os.path.join(args.out, "tiles", f"{lat}_{lon}.json")
        # A tile on a border may already hold places from the neighbouring country.
        if os.path.exists(tile_path) and not args.from_tiles:
            have = {p["id"] for p in places}
            places += [p for p in json.load(open(tile_path))["places"] if p["id"] not in have and not p.get("unconfirmed")]
        with open(tile_path, "w") as f:
            json.dump({"places": places}, f, separators=(",", ":"), ensure_ascii=False)
        index["tiles"][f"{lat}_{lon}"] = len(places)
        filled = sum(1 for p in places if "fill" in p)
        print(f"tile {lat}_{lon}: {len(places)} places, {filled} filled from Overture, {time.time()-t0:.0f}s")
    json.dump(index, open(index_path, "w"), indent=1)


if __name__ == "__main__":
    main()
