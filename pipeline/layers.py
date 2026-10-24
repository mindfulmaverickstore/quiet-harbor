#!/usr/bin/env python3
"""Builds the places data in layers, one per source, and assembles the files the app reads from them.

Each source is kept whole in its own layer, so a rule (which places to show, how to match duplicates) can change
without downloading anything again, and nothing is thrown away:

  python3 pipeline/layers.py region --pbf portugal.osm.pbf --poly https://download.geofabrik.de/europe/portugal.poly --out part
      part/osm/<lat>_<lon>.json   the listed places and chains from OpenStreetMap in one 1-degree square
      part/ov/<lat>_<lon>.json    every Overture food place in the same squares, closed and doubtful ones included
      part/covered.json           the region's outline
  python3 pipeline/layers.py assemble --parts layers --out dist
      layers/<region>/...         the parts of every region (any number, built at the same time, overlapping at borders)
      dist/tiles/<lat>_<lon>.json and dist/index.json, in the format the app reads (see build.py)

The rules applied when assembling are at the top of this file (RULES_NOW). Changing one needs only "assemble".
"""
import argparse, concurrent.futures, glob, json, os, subprocess, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402  the matching, filling and cleaning rules shared with the one-step build

# What the assembled files leave out, decided 23 Oct 2026. The layers keep these places, so this can change any time.
RULES_NOW = {
    "drop_status": ["permanently_closed"],  # Overture places it marks as closed
    "min_confidence": 0.3,                  # Overture places it doubts exist
}

OV_COLS = ["name", "website", "phone", "socials", "src", "lon", "lat", "cat", "gers_id", "street", "city", "postcode", "status", "confidence"]


def tile_of(lat, lon):
    return f"{int(lat // 1)}_{int(lon // 1)}"


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)


# ----- One region -----

def prefilter(pbf):
    """Keeps only objects with the tags we read, using the fast osmium program: a 3 GB country of forests and roads
    becomes a few MB before our (slow) Python reads it."""
    small = pbf + ".small.pbf"
    exprs = [f"nwr/diet:{build.K}", f"nwr/diet:{build.KB}", "nwr/brand=" + ",".join(build.CHAINS)]
    subprocess.run(["osmium", "tags-filter", pbf, *exprs, "-o", small, "--overwrite"], check=True)
    return small


def overture_tile(con, lat, lon):
    """Every Overture food place in one square, with nothing left out."""
    q = f"""
    select names.primary, websites[1], phones[1], socials, list_distinct([x.dataset for x in sources]),
           bbox.xmin, bbox.ymin, taxonomy.primary, id,
           addresses[1].freeform, addresses[1].locality, addresses[1].postcode, operating_status, confidence
    from read_parquet('s3://overturemaps-us-west-2/release/{build.OVERTURE_RELEASE}/theme=places/type=place/*', hive_partitioning=1)
    where bbox.xmin between {lon} and {lon + 1} and bbox.ymin between {lat} and {lat + 1}
      and (taxonomy.hierarchy[1] = 'food_and_drink' or taxonomy.hierarchy[2] = 'food_and_beverage_store')
    """
    for attempt in range(3):
        try:
            return [list(r) for r in con.cursor().execute(q).fetchall()]
        except Exception as ex:
            print(f"  overture {lat}_{lon}: {ex}; retrying", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"overture {lat}_{lon} failed")


def region(args):
    cat = json.load(urllib.request.urlopen("https://stac.overturemaps.org/catalog.json", timeout=30))
    build.OVERTURE_RELEASE = cat["latest"]
    t0 = time.time()
    tiles = build.read_pbf(prefilter(args.pbf))
    print(f"OpenStreetMap: {sum(len(v) for v in tiles.values())} places in {len(tiles)} squares, {time.time() - t0:.0f}s", flush=True)
    for (lat, lon), els in tiles.items():
        write(os.path.join(args.out, "osm", f"{lat}_{lon}.json"), {"elements": els})
    import duckdb
    con = duckdb.connect()
    con.execute("install httpfs; load httpfs; set s3_region='us-west-2';")
    t0 = time.time()
    # Several squares at once: each is a separate request to Overture's files.
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        jobs = {pool.submit(overture_tile, con, lat, lon): (lat, lon) for lat, lon in tiles}
        for job in concurrent.futures.as_completed(jobs):
            lat, lon = jobs[job]
            write(os.path.join(args.out, "ov", f"{lat}_{lon}.json"), {"release": build.OVERTURE_RELEASE, "cols": OV_COLS, "rows": job.result()})
    print(f"Overture: {len(tiles)} squares in {time.time() - t0:.0f}s", flush=True)
    outline = build.read_poly(args.poly) if args.poly else None
    cover = {"id": os.path.basename(args.pbf), "rings": outline} if outline else {"id": os.path.basename(args.pbf), "box": [round(v, 4) for v in build.pbf_box(args.pbf)]}
    write(os.path.join(args.out, "covered.json"), cover)


# ----- Assemble -----

def keep(o):
    if o["status"] in RULES_NOW["drop_status"]:
        return False
    return o["confidence"] is None or o["confidence"] >= RULES_NOW["min_confidence"]


def assemble(args):
    by_tile = {}
    for path in glob.glob(os.path.join(args.parts, "*", "*", "*.json")):
        layer, name = path.split(os.sep)[-2], os.path.basename(path)[:-5]
        if layer in ("osm", "ov"):
            by_tile.setdefault(name, {"osm": [], "ov": []})[layer].append(path)
    closed = build.closed_ids()
    release = ""
    covered, seen_cover = [], set()
    for path in sorted(glob.glob(os.path.join(args.parts, "*", "covered.json"))):
        c = json.load(open(path))
        if c["id"] not in seen_cover:
            seen_cover.add(c["id"]); covered.append(c)
    index = {"built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "overture": "", "covered": covered, "tiles": {}, "rules": RULES_NOW}
    os.makedirs(os.path.join(args.out, "tiles"), exist_ok=True)
    total = 0
    for name, files in sorted(by_tile.items()):
        if not files["osm"]:
            continue
        els, seen = [], set()
        for p in files["osm"]:
            for el in json.load(open(p))["elements"]:
                pid = f'{el["type"]}/{el["id"]}'
                if pid not in seen and "name" in el["tags"]:
                    seen.add(pid); els.append(el)
        ov, seen_ov = [], set()
        for p in files["ov"]:
            d = json.load(open(p))
            release = d.get("release") or release
            for r in d["rows"]:
                o = dict(zip(d["cols"], r))
                if o["gers_id"] not in seen_ov:
                    seen_ov.add(o["gers_id"]); ov.append(o)
        ov = [o for o in ov if keep(o)]
        places = [build.make_place(el, ov) for el in els]
        places += build.unconfirmed_places(ov, places)
        places = [p for p in places if p["id"] not in closed]
        write(os.path.join(args.out, "tiles", f"{name}.json"), {"places": places})
        index["tiles"][name] = len(places)
        total += len(places)
    index["overture"] = release
    index["licence"] = build.LICENCE_TEXT
    json.dump(index, open(os.path.join(args.out, "index.json"), "w"), indent=1)
    print(f"assembled {len(index['tiles'])} squares, {total} places, from {len(covered)} regions", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("region")
    r.add_argument("--pbf", required=True)
    r.add_argument("--poly")
    r.add_argument("--out", required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--parts", required=True)
    a.add_argument("--out", required=True)
    args = ap.parse_args()
    region(args) if args.cmd == "region" else assemble(args)
