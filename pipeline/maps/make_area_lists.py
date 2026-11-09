#!/usr/bin/env python3
"""Makes the small per-region list the app reads to offer download areas: no heavy outlines, just what the picker needs.

  python3 pipeline/maps/make_area_lists.py portugal nord-ovest      # writes areas/lists/<region>.json

From areas/<region>.geojson (outlines and measured sizes) and areas/files/<region>/sizes.json (the exact size of each
area's map file) it writes areas/lists/<region>.json:
  {"built": "2026-10-24", "region": "portugal", "areas": [
     {"id": "r123", "n": "Viana do Castelo", "e": "English name or absent", "b": 1234567 (bytes), "p": "parent id or absent",
      "l": 8 (admin level), "x": [west, south, east, north], "r": [[lon, lat, lon, lat, ...], ...] (rough outline, about 200 m)}]}
Areas named only in another alphabet get an English or Latin-letter name from OpenStreetMap when it has one (Overpass).
Plain Python plus shapely; needs the aws program and S3_ENDPOINT, S3_BUCKET and AWS_* variables.
"""
import argparse, datetime, gzip, json, os, subprocess, sys, tempfile, urllib.parse, urllib.request

import shapely
from shapely.geometry import shape

PUBLIC = 'https://pub-e5b664167e394466a0e48820dffb17df.r2.dev'
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


def one(region):
    data = fetch_json(f'{PUBLIC}/areas/{region}.geojson')
    sizes = fetch_json(f'{PUBLIC}/areas/files/{region}/sizes.json')
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
    missing = [a['id'] for a in areas if 'e' not in a and any(ord(ch) > 0x24F for ch in a['n'])]
    if missing:
        names = latin_names(missing)
        for a in areas:
            if a['id'] in names:
                a['e'] = names[a['id']]
        print(f'{region}: {len(missing)} areas had no English name, {len(names)} found', flush=True)
    body = gzip.compress(json.dumps({'built': datetime.date.today().isoformat(), 'region': region, 'areas': areas}, ensure_ascii=False, separators=(',', ':')).encode(), 9)
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
