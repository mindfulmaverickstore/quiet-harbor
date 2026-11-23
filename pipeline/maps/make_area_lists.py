#!/usr/bin/env python3
"""Makes the small per-region list the app reads to offer download areas: no heavy outlines, just what the picker needs.

  python3 pipeline/maps/make_area_lists.py portugal nord-ovest      # writes areas/lists/<region>.json

From areas/<region>.geojson (outlines and measured sizes) and areas/files/<region>/sizes.json (the exact size of each
area's map file) it writes areas/lists/<region>.json:
  {"built": "2026-10-24", "region": "portugal", "areas": [
     {"id": "r123", "n": "Viana do Castelo", "e": "English name or absent", "b": 1234567 (bytes), "p": "parent id or absent",
      "f": "friendly name, only when it differs (see friendly_names.py; n stays the official name)",
      "l": 8 (admin level), "x": [west, south, east, north], "r": [[lon, lat, lon, lat, ...], ...] (rough outline, about 200 m)}]}
Areas named only in another alphabet get an English or Latin-letter name from OpenStreetMap when it has one (Overpass).
Friendly names (friendly_names.py) need osmium-tool and a connection to Geofabrik; CHECK_ONLY=1 prints the renamed areas and uploads nothing.
Plain Python plus shapely; needs the aws program and S3_ENDPOINT, S3_BUCKET and AWS_* variables.
"""
import argparse, datetime, gzip, json, os, subprocess, sys, tempfile, urllib.parse, urllib.request

import shapely
from shapely.geometry import shape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import friendly_names

PUBLIC = 'https://maps.beetstreets.com'
# The area files and their sizes.json live on Backblaze.
AREA_FILES_PUBLIC = 'https://f004.backblazeb2.com/file/harbor-maps'
ENDPOINT, BUCKET = os.environ.get('S3_ENDPOINT', ''), os.environ.get('S3_BUCKET', '')
UA = {'User-Agent': 'map-build/1.0'}


def fetch_json(url):
    raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw)


def rings(geom):
    """Outer outlines only, as flat lists of longitude, latitude (already simplified, see shared_simplify)."""
    polys = list(geom.geoms) if geom.geom_type in ('MultiPolygon', 'GeometryCollection') else [geom]
    out = []
    for q in polys:
        if q.is_empty or q.geom_type != 'Polygon':
            continue
        coords = list(q.exterior.coords)
        if len(coords) < 4:
            continue
        # A very large area would be heavy: keep at most 1500 points.
        step = max(1, len(coords) // 1500)
        flat = []
        for x, y in coords[::step]:
            flat += [round(x, 4), round(y, 4)]
        out.append(flat)
    return out


def shared_simplify(geoms, tolerance=0.0006):
    """Simplifies neighbours together, so a border shared by two towns stays one line where the outlines allow. (The
    outlines from build_areas.py are already simplified one by one, so thin slivers remain; the app treats a tap within
    about 150 m of a town as in it.) Falls back to one by one, finer."""
    try:
        return list(shapely.coverage_simplify(geoms, tolerance))
    except Exception:
        return [g.simplify(0.0003, preserve_topology=True) for g in geoms]


LATIN_NAME_KEYS = ('name:en', 'int_name')


def latin_names(ids):
    """English or Latin-letter names from OpenStreetMap today, for areas whose outline was saved without an English name
    (some Thai sub-districts, for example). Asks the Overpass service by relation id, a few hundred at a time; an area it
    has nothing for keeps its own name. Uses name:en, then int_name, then any name written in Latin letters (name:th-Latn)."""
    found = {}
    rel = [i[1:] for i in ids if i.startswith('r') and i[1:].isdigit()]
    for start in range(0, len(rel), 400):
        query = f'[out:json][timeout:120];rel(id:{",".join(rel[start:start + 400])});out tags;'
        try:
            req = urllib.request.Request('https://overpass-api.de/api/interpreter', data=urllib.parse.urlencode({'data': query}).encode(), headers=UA)
            elements = json.loads(urllib.request.urlopen(req, timeout=180).read())['elements']
        except Exception as e:
            print(f'  English names: Overpass did not answer ({e!r}); keeping the names we have', flush=True)
            return found
        for el in elements:
            tags = el.get('tags', {})
            name = next((tags[k] for k in LATIN_NAME_KEYS if tags.get(k)), None) or next((v for k, v in sorted(tags.items()) if k.startswith('name:') and k.endswith('-Latn')), None)
            if name and name != tags.get('name'):
                found[f'r{el["id"]}'] = name
    return found


GEOFABRIK = 'https://download.geofabrik.de/index-v1-nogeom.json'


def region_places(region):
    """Places and administrative names of the region's OpenStreetMap extract, or None when it cannot be had (the lists are
    then made without friendly names rather than not at all)."""
    try:
        gf = {f['properties']['urls']['pbf'].split('/')[-1].replace('-latest', ''): f['properties'] for f in fetch_json(GEOFABRIK)['features']}
        url = gf[f'{region}.osm.pbf']['urls']['pbf']
        with tempfile.TemporaryDirectory() as work:
            pbf = os.path.join(work, 'region.osm.pbf')
            # "-latest" points at today's file, which Geofabrik sometimes has not finished (404/503): then yesterday's, or the day before's.
            tries = [url] + [url.replace('-latest.osm.pbf', f'-{(datetime.date.today() - datetime.timedelta(days=d)).strftime("%y%m%d")}.osm.pbf') for d in (1, 2)]
            for i, u in enumerate(tries):
                if subprocess.run(['curl', '-sSfL', '--retry', '3', '--retry-delay', '5', '-A', UA['User-Agent'], '-o', pbf, u]).returncode == 0:
                    break
                if i == len(tries) - 1:
                    raise RuntimeError('no download of the last three days worked')
            return friendly_names.load_places(pbf, work), gf[f'{region}.osm.pbf'].get('name') or region
    except Exception as e:
        print(f'{region}: no friendly names ({e!r})', flush=True)
        return None, None


def one(region):
    data = fetch_json(f'{PUBLIC}/areas/{region}.geojson')
    sizes = fetch_json(f'{AREA_FILES_PUBLIC}/areas/files/{region}/sizes.json')
    feats = [f for f in data['features'] if f['properties']['id'] in sizes]
    full = [shapely.make_valid(shape(f['geometry'])) for f in feats]
    # Areas of the same level (all the towns, all the provinces) tile the region, so each level is simplified as one cover.
    simple = list(full)
    for level in {f['properties']['level'] for f in feats}:
        idx = [i for i, f in enumerate(feats) if f['properties']['level'] == level]
        for i, g in zip(idx, shared_simplify([full[i] for i in idx])):
            simple[i] = g
    areas = []
    for f, g, sg in zip(feats, full, simple):
        p = f['properties']
        rs = rings(sg) or rings(g.simplify(0.0003))
        if not rs:
            continue
        w, s_, e, n = g.bounds
        a = {'id': p['id'], 'n': p['name'], 'b': sizes[p['id']], 'l': p['level'], 'x': [round(w, 3), round(s_, 3), round(e, 3), round(n, 3)], 'r': rs}
        if p.get('name_en'):
            a['e'] = p['name_en']
        if p.get('parent'):
            a['p'] = p['parent']
        areas.append(a)
    loaded, official_title = region_places(region)
    title = None
    if loaded:
        places, admin_names = loaded
        places += friendly_names.wikidata_archipelagos([[min(a['x'][0] for a in areas), min(a['x'][1] for a in areas), max(a['x'][2] for a in areas), max(a['x'][3] for a in areas)]] if max(a['x'][2] for a in areas) - min(a['x'][0] for a in areas) < 60 else [])
        kids = {}
        for a in areas:
            if 'p' in a:
                kids.setdefault(a['p'], []).append(a['id'])
        geoms = {p['id']: g for p, g in ((f['properties'], g) for f, g in zip(feats, full))}
        byid = {a['id']: a for a in areas}
        friendly_kid = lambda k: k.get('e') or k['n']
        rows = [{'id': a['id'], 'name': a['n'], 'name_en': a.get('e'), 'geom': geoms[a['id']], 'kid_list': [{'name': friendly_kid(k), 'geom': geoms[k['id']], 'b': k['b']} for k in (byid[i] for i in kids.get(a['id'], []) if i in geoms)], 'kids': shapely.union_all([geoms[k] for k in kids[a['id']] if k in geoms]) if a['id'] in kids else None} for a in areas]
        got = friendly_names.name_areas(rows, places)
        for a in areas:
            if a['id'] in got:
                a['f'] = got[a['id']]
        title = friendly_names.region_title(official_title, places, admin_names, shapely.union_all(list(geoms.values())))
        print(f'{region}: {len(got)} of {len(areas)} areas got a friendly name; region title {title!r}', flush=True)
    missing = [a['id'] for a in areas if 'e' not in a and any(ord(ch) > 0x24F for ch in a['n'])]
    if missing:
        names = latin_names(missing)
        for a in areas:
            if a['id'] in names:
                a['e'] = names[a['id']]
        print(f'{region}: {len(missing)} areas had no English name, {len(names)} found', flush=True)
    if os.environ.get('CHECK_ONLY'):
        for a in areas:
            if 'f' in a:
                print(f'  {a["n"]}  ->  {a["f"]}')
        return
    body = gzip.compress(json.dumps({'built': datetime.date.today().isoformat(), 'region': region, **({'title': title} if title else {}), 'areas': areas}, ensure_ascii=False, separators=(',', ':')).encode(), 9)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(body)
    subprocess.run(['aws', 's3', 'cp', f.name, f's3://{BUCKET}/areas/lists/{region}.json', '--endpoint-url', ENDPOINT, '--content-type', 'application/json',
                    '--content-encoding', 'gzip', '--only-show-errors'], check=True)
    print(f'{region}: {len(areas)} areas, {len(body) / 1000:.0f} KB', flush=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('regions', nargs='+')
    failed = []
    for r in ap.parse_args().regions:
        try:
            one(r)
        except Exception as e:
            print(f'{r}: FAILED {e!r}', flush=True)
            failed.append(r)
    if failed:
        sys.exit('failed: ' + ' '.join(failed))
