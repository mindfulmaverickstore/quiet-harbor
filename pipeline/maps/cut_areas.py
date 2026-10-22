#!/usr/bin/env python3
"""Cuts one map file per download area (made by build_areas.py), from our full-detail maps.

  python3 pipeline/maps/cut_areas.py --region portugal      # writes areas/files/portugal/<area id>.pmtiles and sizes.json

Each file holds every map square (zoom 6 to 15) that touches the area's outline, so it can be shown on its own with no
signal. The squares are copied, not redrawn, from whichever of our maps holds them (the smallest one first).
sizes.json lists the exact size of every file, which is what the app shows before you tap Save.
Plain Python plus shapely; needs the aws program and S3_ENDPOINT, S3_BUCKET and AWS_* variables.
"""
import argparse, gzip, json, math, os, struct, subprocess, sys, tempfile, urllib.request

import shapely
from shapely.geometry import box, shape

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_light import read_dir, write_pmtiles, zxy  # noqa: E402

PUBLIC = 'https://pub-e5b664167e394466a0e48820dffb17df.r2.dev'
ENDPOINT, BUCKET = os.environ.get('S3_ENDPOINT', ''), os.environ.get('S3_BUCKET', '')
MIN_ZOOM, MAX_ZOOM = 6, 15  # below 6 the app draws the shared world overview
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


# ---- Map squares ----

def tile_id(z, x, y):
    """PMTiles' number for a map square (the inverse of make_light.zxy)."""
    acc = sum(1 << (2 * i) for i in range(z))
    n, d, s = 1 << z, 0, (1 << z) // 2
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = n - 1 - x, n - 1 - y
            x, y = y, x
        s //= 2
    return acc + d


def lon_of(x, z):
    return x / (1 << z) * 360 - 180


def lat_of(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))


def square(z, x, y):
    return box(lon_of(x, z), lat_of(y + 1, z), lon_of(x + 1, z), lat_of(y, z))


def cover(geom):
    """Every square from MIN_ZOOM to MAX_ZOOM that touches the outline. A square wholly inside needs no more tests."""
    shapely.prepare(geom)
    w, s, e, n = geom.bounds
    z = MIN_ZOOM
    x0 = int((w + 180) / 360 * (1 << z))
    x1 = int((e + 180) / 360 * (1 << z))
    y0 = int((1 - math.asinh(math.tan(math.radians(n))) / math.pi) / 2 * (1 << z))
    y1 = int((1 - math.asinh(math.tan(math.radians(s))) / math.pi) / 2 * (1 << z))
    out = []
    stack = [(z, x, y, False) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    while stack:
        z, x, y, inside = stack.pop()
        if not inside:
            sq = square(z, x, y)
            if not geom.intersects(sq):
                continue
            inside = geom.contains(sq)
        out.append(tile_id(z, x, y))
        if z < MAX_ZOOM:
            for dx in (0, 1):
                for dy in (0, 1):
                    stack.append((z + 1, 2 * x + dx, 2 * y + dy, inside))
    return out


# ---- Reading our maps without loading them whole ----

class Source:
    def __init__(self, path):
        self.f = open(path, 'rb')
        h = self.f.read(127)
        self.header = h
        rdo, rdl, mo, ml, ldo, ldl, self.tdo, _ = struct.unpack('<8Q', h[8:72])
        self.ic = h[97]
        self.f.seek(mo)
        self.meta = self.f.read(ml)
        self.entries = {}
        self._walk(rdo, rdl, ldo)

    def _walk(self, off, length, ldo):
        self.f.seek(off)
        raw = self.f.read(length)
        raw = gzip.decompress(raw) if self.ic == 2 else raw
        for tid, run, ln, o in read_dir(raw):
            if run == 0:
                self._walk(ldo + o, ln, ldo)
            else:
                for k in range(run):
                    self.entries[tid + k] = (o, ln)

    def tile(self, tid):
        e = self.entries.get(tid)
        if not e:
            return None
        self.f.seek(self.tdo + e[0])
        return self.f.read(e[1])


def region(name):
    areas = fetch_json(f'{PUBLIC}/areas/{name}.geojson')['features']
    catalog = fetch_json(f'{PUBLIC}/maps/catalog.json')['maps']
    work = tempfile.mkdtemp()
    want = {}  # area id -> list of tile ids
    for a in areas:
        g = shapely.make_valid(shape(a['geometry']))
        if g.is_empty:
            continue
        want[a['properties']['id']] = (g, cover(g))
    w = min(g.bounds[0] for g, _ in want.values())
    s = min(g.bounds[1] for g, _ in want.values())
    e = max(g.bounds[2] for g, _ in want.values())
    n = max(g.bounds[3] for g, _ in want.values())
    needed = set(t for _, ts in want.values() for t in ts)
    print(f'{name}: {len(want)} areas, {len(needed)} map squares to find', flush=True)
    # The tiles each come from the smallest map that has them; stored once on disk, shared by every area that needs them.
    store = open(os.path.join(work, 'tiles.bin'), 'wb')
    where = {}
    template = None
    maps = sorted((m for m in catalog if not (m['bbox'][0] > e or m['bbox'][2] < w or m['bbox'][1] > n or m['bbox'][3] < s)),
                  key=lambda m: m['mb'])
    pos = 0
    for m in maps:
        if len(where) == len(needed):
            break
        path = os.path.join(work, 'src.pmtiles')
        aws('cp', f's3://{BUCKET}/maps/{m["id"]}/city.pmtiles', path, '--only-show-errors')
        src = Source(path)
        if template is None:
            template = (src.header, src.meta if src.ic == 2 else gzip.compress(src.meta, 9))
        got = 0
        for tid in needed:
            if tid in where:
                continue
            blob = src.tile(tid)
            if blob is not None:
                store.write(blob)
                where[tid] = (pos, len(blob))
                pos += len(blob)
                got += 1
        src.f.close()
        os.remove(path)
        print(f'  {m["id"]}: {got} squares', flush=True)
    store.close()
    store = open(os.path.join(work, 'tiles.bin'), 'rb')
    sizes = {}
    for aid, (g, ts) in want.items():
        tiles = []
        for tid in sorted(set(ts)):
            if tid in where:
                o, ln = where[tid]
                store.seek(o)
                tiles.append((tid, 1, store.read(ln)))
        if not tiles:
            continue
        head = bytearray(template[0])
        head[98] = 2  # gzipped tiles, as in our maps
        head[100], head[101] = MIN_ZOOM, MAX_ZOOM
        bw, bs, be, bn = g.bounds
        head[102:118] = struct.pack('<4i', int(bw * 1e7), int(bs * 1e7), int(be * 1e7), int(bn * 1e7))
        c = g.representative_point()
        head[118] = 12
        head[119:127] = struct.pack('<2i', int(c.x * 1e7), int(c.y * 1e7))
        out = os.path.join(work, f'{aid}.pmtiles')
        write_pmtiles(out, bytes(head), template[1], tiles)
        aws('cp', out, f's3://{BUCKET}/areas/files/{name}/{aid}.pmtiles', '--content-type', 'application/vnd.pmtiles', '--only-show-errors')
        sizes[aid] = os.path.getsize(out)
        os.remove(out)
    with open(os.path.join(work, 'sizes.json'), 'w') as f:
        json.dump(sizes, f, separators=(',', ':'))
    aws('cp', os.path.join(work, 'sizes.json'), f's3://{BUCKET}/areas/files/{name}/sizes.json', '--content-type', 'application/json')
    big = sorted(sizes.values())
    print(f'{name}: {len(sizes)} files, {sum(big) / 1e6:.0f} MB in all, largest {big[-1] / 1e6:.1f} MB' if big else f'{name}: no files')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--region', nargs='+', required=True)
    failed = []
    for r in ap.parse_args().region:
        try:
            region(r.replace('.osm.pbf', ''))
        except Exception as ex:
            print(f'{r}: FAILED {ex!r}', flush=True)
            failed.append(r)
    if failed:
        sys.exit('failed: ' + ' '.join(failed))
