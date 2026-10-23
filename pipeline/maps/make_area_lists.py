#!/usr/bin/env python3
"""Makes the small per-region list the app reads to offer download areas: no heavy outlines, just what the picker needs.

  python3 pipeline/maps/make_area_lists.py portugal nord-ovest      # writes areas/lists/<region>.json

From areas/<region>.geojson (outlines and measured sizes) and areas/files/<region>/sizes.json (the exact size of each
area's map file) it writes areas/lists/<region>.json:
  {"built": "2026-10-24", "region": "portugal", "areas": [
     {"id": "r123", "n": "Viana do Castelo", "e": "English name or absent", "b": 1234567 (bytes), "p": "parent id or absent",
      "l": 8 (admin level), "x": [west, south, east, north], "r": [[lon, lat, lon, lat, ...], ...] (rough outline, about 200 m)}]}
Plain Python plus shapely; needs the aws program and S3_ENDPOINT, S3_BUCKET and AWS_* variables.
"""
import argparse, datetime, gzip, json, os, subprocess, sys, tempfile, urllib.request

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
    """Outer outlines only, simplified to roughly 200 m, as flat lists of longitude, latitude."""
    polys = list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [geom] if geom.geom_type == 'Polygon' else []
    out = []
    for p in polys:
        if p.is_empty:
            continue
        s = p.simplify(0.002, preserve_topology=False)
        for q in (list(s.geoms) if s.geom_type == 'MultiPolygon' else [s]):
            if q.is_empty or q.geom_type != 'Polygon':
                continue
            coords = list(q.exterior.coords)
            if len(coords) < 4:
                continue
            # A fine outline of a very large area would be heavy: keep at most 400 points.
            step = max(1, len(coords) // 400)
            flat = []
            for x, y in coords[::step]:
                flat += [round(x, 3), round(y, 3)]
            out.append(flat)
    return out


def one(region):
    data = fetch_json(f'{PUBLIC}/areas/{region}.geojson')
    sizes = fetch_json(f'{PUBLIC}/areas/files/{region}/sizes.json')
    areas = []
    for f in data['features']:
        p = f['properties']
        if p['id'] not in sizes:
            continue
        g = shapely.make_valid(shape(f['geometry']))
        rs = rings(g)
        if not rs:
            continue
        w, s, e, n = g.bounds
        a = {'id': p['id'], 'n': p['name'], 'b': sizes[p['id']], 'l': p['level'], 'x': [round(w, 3), round(s, 3), round(e, 3), round(n, 3)], 'r': rs}
        if p.get('name_en'):
            a['e'] = p['name_en']
        if p.get('parent'):
            a['p'] = p['parent']
        areas.append(a)
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
