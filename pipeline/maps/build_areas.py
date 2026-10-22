#!/usr/bin/env python3
"""Cuts the world into download areas that follow real boundaries: towns, districts, counties.

For one Geofabrik region it reads OpenStreetMap's administrative boundaries, measures each one with the density lists
that make_light.py writes (bytes of map per zoom-13 square, full detail by default), and keeps the largest boundaries that are no bigger
than MAX_MB (45). Anything bigger is replaced by the boundaries inside it; land inside it that none of them cover becomes
"Rest of ...". A boundary with nothing smaller inside it that is still too big is cut into quarters, named after
the biggest town in each. Inside every area offered, its towns and districts are listed too (with "parent" set), so the
app can offer Metro Vancouver as a whole or just Surrey.

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
MAX_MB = 45        # largest area offered as one download (the towns inside it are offered too)
SUB_MIN_MB = 0.2  # towns inside an offered area are listed too, down to this size
REST_MIN_MB = 2    # leftover land smaller than this is left out (it is still inside a neighbouring area's map squares)
SIMPLIFY = 0.0008  # about 80 m: plenty for outlines drawn on a phone
ENDPOINT, BUCKET = os.environ.get('S3_ENDPOINT', ''), os.environ.get('S3_BUCKET', '')
# Which map the sizes are measured on: the full-detail map (chosen 22 Oct 2026) or the light one.
DENSITY_FILE = os.environ.get('AREA_DENSITY', 'density-full.json')
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
    """Bytes of map per zoom-13 square, from every map that touches the region (the largest value wins where maps overlap)."""

    def __init__(self, bounds):
        w, s, e, n = bounds
        cells = {}
        for m in fetch_json(f'{PUBLIC}/maps/catalog.json')['maps']:
            mw, ms, me, mn = m['bbox']
            if mw > e or me < w or ms > n or mn < s:
                continue
            try:
                d = fetch_json(f'{PUBLIC}/maps/{m["id"]}/{DENSITY_FILE}')
            except Exception as e:
                # Sizes would come out too small without it; better to fail and run this region again later.
                raise RuntimeError(f'no {DENSITY_FILE} for map {m["id"]}') from e
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


RANK = {'city': 3, 'town': 2, 'village': 1}


def towns(pbf, work):
    """Cities, towns and villages, to name the pieces of an area that had to be cut up."""
    pl, out = os.path.join(work, 'places.pbf'), os.path.join(work, 'places.geojsonseq')
    run('osmium', 'tags-filter', pbf, 'n/place=city,town,village', '-o', pl, '--overwrite')
    run('osmium', 'export', pl, '-f', 'geojsonseq', '--geometry-types=point', '-o', out, '--overwrite')
    found = []
    for line in open(out):
        f = json.loads(line.lstrip('\x1e'))
        p = f['properties']
        name = p.get('name:en') or p.get('name')
        if not name:
            continue
        try:
            pop = int(str(p.get('population', '0')).replace(',', '').replace(' ', '').split('.')[0] or 0)
        except ValueError:
            pop = 0
        x, y = f['geometry']['coordinates']
        found.append((RANK.get(p.get('place'), 0), pop, x, y, name))
    found.sort(reverse=True)
    print(f'{len(found)} towns and villages', flush=True)
    return found


def biggest_town(geom, places):
    w, s, e, n = geom.bounds
    for rank, pop, x, y, name in places:  # largest first
        if w <= x <= e and s <= y <= n and geom.contains(shapely.Point(x, y)):
            return name
    return None


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
            # (A district can share its province's exact outline, so "bigger" allows equal.)
            if c['level'] < u['level'] and c is not u and c['geom'].area >= u['geom'].area * 0.98 and c['geom'].contains(pt):
                if best is None or c['geom'].area < best['geom'].area:
                    best = c
        u['up'] = best
        if best:
            best['children'].append(u)
    return [u for u in units if u['up'] is None]


# ---- Choosing the areas ----

def compass(geom, bounds):
    """Where a piece sits inside the area it was cut from: north-west ... south-east, or centre."""
    w, s, e, n = bounds
    c = geom.representative_point()
    fx, fy = (c.x - w) / ((e - w) or 1), (c.y - s) / ((n - s) or 1)
    ns = 'north' if fy > 2 / 3 else 'south' if fy < 1 / 3 else ''
    ew = 'east' if fx > 2 / 3 else 'west' if fx < 1 / 3 else ''
    return '-'.join(x for x in (ns, ew) if x) or 'centre'


def inner(node, path, dens, out):
    """The towns and districts inside an area we offer, so someone zoomed in can pick just Surrey instead of all of Metro Vancouver."""
    for k in node.get('children') or []:
        # A district with the same outline as its city (Paris is a city, a département and an arrondissement level at once)
        # adds nothing: list what is inside it instead.
        if k['geom'].area >= node['geom'].area * 0.98:
            inner({**k, 'id': node['id'], 'name': node['name']}, path, dens, out)
            continue
        size = dens.mb(k['geom'])
        if size >= SUB_MIN_MB:
            out.append({**k, 'mb': size, 'path': path + [node['name']], 'parent': node['id']})
            inner(k, path + [node['name']], dens, out)


def choose(node, path, dens, out, top=False, places=()):
    size = dens.mb(node['geom'])
    if size <= MAX_MB:
        out.append({**node, 'mb': size, 'path': path})
        inner(node, path, dens, out)
        return
    kids = node.get('children') or []
    if kids:
        for k in kids:
            choose(k, path + [node['name']], dens, out, places=places)
        rest = node['geom'].difference(unary_union([k['geom'] for k in kids]))
        rest_mb = dens.mb(rest)
        # Around a whole region, the leftover is mostly the strip of neighbouring countries in Geofabrik's outline; those
        # are done by their own region. Keep it only when the boundaries here cover less than half of the map.
        if rest_mb >= REST_MIN_MB and not (top and rest_mb < size / 2):
            choose({'id': node['id'] + '-rest', 'name': f'Rest of {node["name"]}', 'name_en': None,
                    'level': node['level'], 'geom': rest}, path + [node['name']], dens, out, places=places)
        return
    split(node, node['name'], node['geom'].bounds, path, dens, out, places)


def split(node, base, base_bounds, path, dens, out, places=()):
    """Too big, with nothing smaller inside: cut into quarters until each piece fits. Named after where it sits."""
    w, s, e, n = node['geom'].bounds
    size = dens.mb(node['geom'])
    if size <= MAX_MB or (e - w < 0.02 and n - s < 0.02):  # fits, or a dense city block not worth cutting further
        if node['name'] != base:
            town = biggest_town(node['geom'], places)
            label = f'around {town}' if town else compass(node['geom'], base_bounds)
            node = {**node, 'name': f'{base} ({label})'}
        out.append({**node, 'mb': size, 'path': path})
        return
    mx, my = (w + e) / 2, (s + n) / 2
    for i, (x0, y0, x1, y1) in enumerate(((w, my, mx, n), (mx, my, e, n), (w, s, mx, my), (mx, s, e, my))):
        part = node['geom'].intersection(box(x0, y0, x1, y1))
        if not part.is_empty and dens.mb(part) > 0.2:
            split({'id': f'{node["id"]}-{i}', 'name': '', 'name_en': None, 'level': node['level'], 'geom': part},
                  base, base_bounds, path, dens, out, places)


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
        places = towns(pbf, work)
    # Only boundaries that sit mostly in this region; their neighbours are done by the neighbouring region.
    units = [u for u in units if outline.contains(u['geom'].representative_point())]
    roots = tree(units)
    dens = Density(outline.bounds)
    title = props.get('name') or name
    if '/' in title:
        title = title.split('/')[-1].replace('-', ' ').title()
    top = {'id': f'g-{name}', 'name': title, 'name_en': None, 'level': 2, 'geom': outline, 'children': roots}
    out = []
    choose(top, [], dens, out, top=True, places=places)
    # Two pieces of one area can land on the same compass point; number them.
    # (A town may share its region's name, like Milan inside Milan; the app tells them apart by type, region or town.)
    seen = {}
    for a in out:
        if a.get('parent'):
            continue
        seen[a['name']] = seen.get(a['name'], 0) + 1
        if seen[a['name']] > 1:
            a['name'] = f'{a["name"]} {seen[a["name"]]}'
    feats = []
    for a in out:
        g = a['geom'].simplify(SIMPLIFY)
        if g.is_empty:
            continue
        g = shapely.set_precision(g, 0.0001)
        feats.append({'type': 'Feature', 'geometry': mapping(g), 'properties': {
            'id': a['id'], 'name': a['name'], **({'name_en': a['name_en']} if a.get('name_en') and a['name_en'] != a['name'] else {}),
            'level': a['level'], 'mb': round(a['mb'], 1), 'in': a['path'][1:], **({'parent': a['parent']} if a.get('parent') else {})}})
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
