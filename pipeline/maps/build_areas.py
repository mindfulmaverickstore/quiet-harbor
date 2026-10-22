#!/usr/bin/env python3
"""Cuts the world into download areas that follow real boundaries: towns, districts, counties.

For one Geofabrik region it reads OpenStreetMap's administrative boundaries, measures each one with the density lists
that make_light.py writes (bytes of light map per zoom-13 square), and keeps the largest boundaries that are no bigger
than MAX_MB (40). Anything bigger is replaced by the boundaries inside it; land inside it that none of them cover becomes
"Rest of ...". A boundary with nothing smaller inside it that is still too big is cut into quarters.

  python3 pipeline/maps/build_areas.py --region luxembourg.osm.pbf     # writes areas/<region>.geojson
  python3 pipeline/maps/build_areas.py --index                         # writes areas/index.json

Needs osmium (osmium-tool), shapely and numpy, the aws program and S3_ENDPOINT, S3_BUCKET and AWS_* variables.
"""
import argparse, gzip, json, math, os, subprocess, sys, tempfile, urllib.request

import numpy as np
import shapely
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

PUBLIC = 'https://pub-e5b664167e394466a0e48820dffb17df.r2.dev'
GEOFABRIK = 'https://download.geofabrik.de/index-v1-nogeom.json'
MAX_MB = 40        # largest area we offer as one download (small enough that a city splits into its towns)
REST_MIN_MB = 2    # leftover land smaller than this is left out (it is still inside a neighbouring area's map squares)
SIMPLIFY = 0.0008  # about 80 m: plenty for outlines drawn on a phone
ENDPOINT, BUCKET = os.environ.get('S3_ENDPOINT', ''), os.environ.get('S3_BUCKET', '')
UA = {'User-Agent': 'map-build/1.0'}


def fetch_json(url):
    raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw)


def aws(*args):
    return subprocess.run(['aws', 's3', *args, '--endpoint-url', ENDPOINT], check=True, capture_output=True, text=True)


def run(*cmd):
    subprocess.run(cmd, check=True)


# ---- How big is an area? ----

class Density:
    """Bytes of light map per zoom-13 square, from every map that touches the region (the largest value wins where maps overlap)."""

    def __init__(self, bounds):
        w, s, e, n = bounds
        cells = {}
        for m in fetch_json(f'{PUBLIC}/maps/catalog.json')['maps']:
            mw, ms, me, mn = m['bbox']
            if mw > e or me < w or ms > n or mn < s:
                continue
            try:
                d = fetch_json(f'{PUBLIC}/maps/{m["id"]}/density.json')
            except Exception:
                continue
            for k, v in d['bytes'].items():
                if v > cells.get(k, 0):
                    cells[k] = v
        z = 1 << 13
        xs, ys, vs = [], [], []
        for k, v in cells.items():
            x, y = map(int, k.split('/'))
            xs.append((x + 0.5) / z * 360 - 180)
            ys.append(math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / z)))))
            vs.append(v)
        self.x, self.y, self.v = np.array(xs), np.array(ys), np.array(vs, dtype=np.float64)
        print(f'density: {len(vs)} squares from the maps around this region', flush=True)

    def mb(self, geom):
        if geom.is_empty or not len(self.v):
            return 0.0
        w, s, e, n = geom.bounds
        sel = (self.x >= w) & (self.x <= e) & (self.y >= s) & (self.y <= n)
        if not sel.any():
            return 0.0
        inside = shapely.contains_xy(geom, self.x[sel], self.y[sel])
        return float(self.v[sel][inside].sum()) / 1e6


# ---- Boundaries ----

def boundaries(pbf, work):
    adm, out = os.path.join(work, 'adm.pbf'), os.path.join(work, 'adm.geojsonseq')
    run('osmium', 'tags-filter', pbf, 'r/boundary=administrative', '-o', adm, '--overwrite')
    run('osmium', 'export', adm, '-f', 'geojsonseq', '--geometry-types=polygon', '-a', 'type,id', '-o', out, '--overwrite')
    units = []
    for line in open(out):
        f = json.loads(line.lstrip('\x1e'))
        p = f['properties']
        if p.get('@type') != 'relation' or p.get('boundary') != 'administrative' or not p.get('name'):
            continue
        try:
            level = int(p.get('admin_level', ''))
        except ValueError:
            continue
        if not 3 <= level <= 10:
            continue
        g = shapely.make_valid(shape(f['geometry'])).simplify(SIMPLIFY / 4)
        if g.is_empty or g.area == 0:
            continue
        units.append({'id': f'r{p["@id"]}', 'name': p['name'], 'name_en': p.get('name:en'), 'level': level, 'geom': g})
    print(f'{len(units)} named boundaries (levels 3 to 10)', flush=True)
    return units


def tree(units):
    """Each boundary's parent is the smallest boundary of a higher rank (lower level number) around it."""
    units.sort(key=lambda u: u['geom'].area)
    for u in units:
        u['children'] = []
    idx = shapely.STRtree([u['geom'] for u in units])
    for u in units:
        pt = u['geom'].representative_point()
        best = None
        for j in idx.query(pt):
            c = units[j]
            if c['level'] < u['level'] and c['geom'].area > u['geom'].area and c['geom'].contains(pt):
                if best is None or c['geom'].area < best['geom'].area:
                    best = c
        u['parent'] = best
        if best:
            best['children'].append(u)
    return [u for u in units if u['parent'] is None]


# ---- Choosing the areas ----

QUARTERS = (('north-west', 0, 1), ('north-east', 1, 1), ('south-west', 0, 0), ('south-east', 1, 0))


def choose(node, path, dens, out):
    size = dens.mb(node['geom'])
    if size <= MAX_MB:
        out.append({**node, 'mb': size, 'path': path})
        return
    kids = node.get('children') or []
    if kids:
        for k in kids:
            choose(k, path + [node['name']], dens, out)
        rest = node['geom'].difference(unary_union([k['geom'] for k in kids]))
        if dens.mb(rest) >= REST_MIN_MB:
            choose({'id': node['id'] + '-rest', 'name': f'Rest of {node["name"]}', 'name_en': None,
                    'level': node['level'], 'geom': rest}, path + [node['name']], dens, out)
        return
    w, s, e, n = node['geom'].bounds
    if e - w < 0.02 and n - s < 0.02:  # a dense city block that can't usefully be split further
        out.append({**node, 'mb': size, 'path': path})
        return
    mx, my = (w + e) / 2, (s + n) / 2
    for label, ix, iy in QUARTERS:
        part = node['geom'].intersection(box(w if ix == 0 else mx, s if iy == 0 else my, mx if ix == 0 else e, my if iy == 0 else n))
        if not part.is_empty and dens.mb(part) > 0.2:
            choose({'id': f'{node["id"]}-{label}', 'name': f'{node["name"]} ({label})', 'name_en': None,
                    'level': node['level'], 'geom': part}, path, dens, out)


def region(file_id):
    name = file_id.replace('.osm.pbf', '')
    gf = {f['properties']['urls']['pbf'].split('/')[-1].replace('-latest', ''): f['properties']
          for f in fetch_json(GEOFABRIK)['features']}
    props = gf[file_id]
    rings = next(c['rings'] for c in fetch_json(f'{PUBLIC}/index.json')['covered'] if c['id'] == file_id)
    outline = shapely.make_valid(shapely.MultiPolygon([shapely.Polygon(r) for r in rings]))
    with tempfile.TemporaryDirectory() as work:
        pbf = os.path.join(work, 'region.osm.pbf')
        run('curl', '-sSfL', '--retry', '5', '-A', UA['User-Agent'], '-o', pbf, props['urls']['pbf'])
        units = boundaries(pbf, work)
    # Only boundaries that sit mostly in this region; their neighbours are done by the neighbouring region.
    units = [u for u in units if outline.contains(u['geom'].representative_point())]
    roots = tree(units)
    dens = Density(outline.bounds)
    top = {'id': f'g-{name}', 'name': props.get('name', name), 'name_en': None, 'level': 2, 'geom': outline, 'children': roots}
    out = []
    choose(top, [], dens, out)
    feats = []
    for a in out:
        g = a['geom'].simplify(SIMPLIFY)
        if g.is_empty:
            continue
        g = shapely.set_precision(g, 0.0001)
        feats.append({'type': 'Feature', 'geometry': mapping(g), 'properties': {
            'id': a['id'], 'name': a['name'], **({'name_en': a['name_en']} if a.get('name_en') and a['name_en'] != a['name'] else {}),
            'level': a['level'], 'mb': round(a['mb'], 1), 'in': a['path'][1:]}})
    feats.sort(key=lambda f: -f['properties']['mb'])
    body = gzip.compress(json.dumps({'type': 'FeatureCollection', 'region': name, 'bbox': [round(v, 3) for v in outline.bounds],
                                     'features': feats}, ensure_ascii=False, separators=(',', ':')).encode(), 9)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(body)
    aws('cp', f.name, f's3://{BUCKET}/areas/{name}.geojson', '--content-type', 'application/geo+json', '--content-encoding', 'gzip')
    sizes = sorted(f['properties']['mb'] for f in feats)
    print(f'{name}: {len(feats)} areas, median {sizes[len(sizes) // 2] if sizes else 0} MB, largest {sizes[-1] if sizes else 0} MB')
    for f in feats[:15]:
        p = f['properties']
        print(f'  {p["mb"]:6.1f} MB  {p["name"]}  (level {p["level"]}{", in " + " / ".join(p["in"]) if p["in"] else ""})')


def index():
    regions = []
    for line in aws('ls', f's3://{BUCKET}/areas/').stdout.splitlines():
        name = line.split()[-1]
        if name.endswith('.geojson'):
            d = fetch_json(f'{PUBLIC}/areas/{name}')
            regions.append({'region': d['region'], 'bbox': d['bbox'], 'areas': len(d['features'])})
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump({'regions': regions}, f, separators=(',', ':'))
    aws('cp', f.name, f's3://{BUCKET}/areas/index.json', '--content-type', 'application/json')
    print(f'{len(regions)} regions, {sum(r["areas"] for r in regions)} areas')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--region', nargs='*')
    ap.add_argument('--index', action='store_true')
    a = ap.parse_args()
    failed = []
    for r in a.region or []:
        try:
            region(r)
        except Exception as e:
            print(f'{r}: FAILED {e!r}', flush=True)
            failed.append(r)
    if a.index:
        index()
    if failed:
        sys.exit('failed: ' + ' '.join(failed))
