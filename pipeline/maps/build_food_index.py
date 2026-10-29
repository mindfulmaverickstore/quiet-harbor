#!/usr/bin/env python3
"""Lists every named food place (restaurant, cafe, bar, pub, ice cream shop, bakery...) inside a published map, so the app can
search them by name or kind from any zoom, not only when the map is zoomed in to street level.

  python3 pipeline/maps/build_food_index.py --ids viana-do-castelo porto            # build and upload
  python3 pipeline/maps/build_food_index.py --ids porto --out /tmp/food --no-upload  # keep the files, upload nothing

It downloads each published maps/<id>/city.pmtiles, reads the base-map places in its most detailed tiles and writes
  maps/<id>/food.json        {"chunks": N, "places": M}
  maps/<id>/food-<i>.json    {"name": [...], "kind": [...], "lat": [...], "lon": [...]}   (columns, up to 20,000 places each)
Chunks of 20,000 because the phone's JavaScript engine drops everything after 65,535 entries of one list.
Needs: pip install pmtiles mapbox-vector-tile
"""
import argparse, gzip, json, math, os, sys, tempfile, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_maps as bm  # upload() and the public address
from pmtiles.reader import Reader, MmapSource, all_tiles
from pmtiles.tile import zxy_to_tileid  # noqa: F401  (kept for readers who want to inspect tiles)
import mapbox_vector_tile as mvt

FOOD_KINDS = {'restaurant', 'fast_food', 'cafe', 'bar', 'pub', 'ice_cream', 'bakery', 'biergarten', 'food_court', 'marketplace'}
CHUNK = 20_000
EXTENT = 4096


def food_places(path):
    """(name, kind, lat, lon) of every named food place in the file's most detailed tiles."""
    seen = {}
    with open(path, 'rb') as f:
        source = MmapSource(f)
        top = Reader(source).header()['max_zoom']
        for (z, x, y), data in all_tiles(source):
            if z != top:
                continue
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
            layer = mvt.decode(data).get('pois')
            if not layer:
                continue
            n = 2 ** z
            for ft in layer['features']:
                p = ft['properties']
                if p.get('kind') not in FOOD_KINDS or not p.get('name'):
                    continue
                px, py = ft['geometry']['coordinates']
                lon = (x + px / EXTENT) / n * 360 - 180
                lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + (EXTENT - py) / EXTENT) / n))))
                name = p.get('name:en') or p['name']
                # The same place can sit on a tile edge: keep one.
                seen[(name, round(lat, 4), round(lon, 4))] = (name, p['kind'], round(lat, 5), round(lon, 5))
    return merge_close(list(seen.values()))


def merge_close(rows):
    """One entry per place: OpenStreetMap sometimes lists a place twice (a point and its building), a door apart. The same
    name (accents and punctuation ignored) within 80 m is one place."""
    import unicodedata, re
    def simple(s):
        return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', s.lower()))
    kept, out = {}, []
    for r in rows:
        key = (simple(r[0]), round(r[2] / 0.001), round(r[3] / 0.001))
        near = [o for dy in (-1, 0, 1) for dx in (-1, 0, 1) for o in kept.get((key[0], key[1] + dy, key[2] + dx), [])]
        k = math.cos(math.radians(r[2]))
        if any(math.hypot((o[2] - r[2]) * 111000, (o[3] - r[3]) * 111000 * k) < 80 for o in near):
            continue
        kept.setdefault(key, []).append(r)
        out.append(r)
    return out


def write(rows, out, pid, upload):
    os.makedirs(out, exist_ok=True)
    chunks = [rows[i:i + CHUNK] for i in range(0, len(rows), CHUNK)] or [[]]
    files = []
    for i, chunk in enumerate(chunks):
        cols = {'name': [r[0] for r in chunk], 'kind': [r[1] for r in chunk], 'lat': [r[2] for r in chunk], 'lon': [r[3] for r in chunk]}
        path = os.path.join(out, f'food-{i}.json')
        with gzip.open(path, 'wt', encoding='utf-8') as g:
            json.dump(cols, g, ensure_ascii=False, separators=(',', ':'))
        files.append((path, f'maps/{pid}/food-{i}.json'))
    manifest = os.path.join(out, 'food.json')
    json.dump({'chunks': len(chunks), 'places': len(rows)}, open(manifest, 'w'))
    files.append((manifest, f'maps/{pid}/food.json'))
    if upload:
        for local, remote in files:
            if not bm.upload_encoded(local, remote):
                return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ids', nargs='+', required=True)
    ap.add_argument('--out')
    ap.add_argument('--no-upload', action='store_true')
    a = ap.parse_args()
    failed = []
    for pid in a.ids:
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, 'city.pmtiles')
            try:
                req = urllib.request.Request(f'{bm.PUBLIC}/maps/{pid}/city.pmtiles', headers=bm.UA)
                with urllib.request.urlopen(req, timeout=120) as r, open(local, 'wb') as f:
                    while chunk := r.read(8 << 20):
                        f.write(chunk)
                rows = food_places(local)
            except Exception as e:  # a missing or broken map must not stop the others
                print(f'{pid}: failed ({e})')
                failed.append(pid)
                continue
            out = os.path.join(a.out, pid) if a.out else os.path.join(tmp, 'food')
            ok = write(rows, out, pid, not a.no_upload)
            print(f'{pid}: {len(rows)} food places' + ('' if ok else ' (upload failed)'))
            if not ok:
                failed.append(pid)
    if failed:
        sys.exit('failed: ' + ' '.join(failed))


if __name__ == '__main__':
    main()
