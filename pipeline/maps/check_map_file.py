#!/usr/bin/env python3
"""Read-only check of a published map: reads it the way the app does (header, directories, tiles over HTTP) and reports
whether tiles decode, how many have roads, and how many names carry a name:en.

  python3 pipeline/maps/check_map_file.py https://.../maps/chiangmai/city.pmtiles?v=2026-11-09 [more urls]
Needs: pip install pmtiles mapbox-vector-tile requests
"""
import gzip, sys
import requests
import mapbox_vector_tile as mvt
from pmtiles.reader import Reader


def getter(url):
    """What Reader needs: bytes by offset and length, read with HTTP range requests as the app does."""
    def get_bytes(offset, length):
        r = requests.get(url, headers={'Range': f'bytes={offset}-{offset + length - 1}'}, timeout=60)
        r.raise_for_status()
        if r.status_code != 206:
            raise RuntimeError(f'server did not answer the range request ({r.status_code})')
        return r.content
    return get_bytes


def plain(b):
    return gzip.decompress(b) if b[:2] == b'\x1f\x8b' else b


def check(url, lon, lat, zooms=(8, 12, 14, 15)):
    import math
    print(url)
    reader = Reader(getter(url))
    h = reader.header()
    print(f"  header ok: zoom {h['min_zoom']}-{h['max_zoom']}, tiles {h['addressed_tiles_count']}")
    for z in zooms:
        n = 2 ** z
        x = int((lon + 180) / 360 * n)
        y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        raw = reader.get(z, x, y)
        if raw is None:
            print(f'  z{z}: tile not found')
            continue
        try:
            layers = mvt.decode(plain(raw))
        except Exception as ex:
            print(f'  z{z}: TILE DOES NOT DECODE: {ex!r}')
            continue
        roads = len(layers.get('roads', {}).get('features', []))
        en = sum(1 for L in layers.values() for f in L['features'] if 'name:en' in f['properties'])
        print(f'  z{z}: decodes, layers {sorted(layers)}, road features {roads}, features with name:en {en}')


def road_names(url, word='มหิดล', lon=98.98, lat=18.78, z=14, span=4):
    """Prints how roads whose Thai name contains `word` are named in the file: the Thai name and the English one the app shows."""
    import math
    reader = Reader(getter(url))
    n = 2 ** z
    x0 = int((lon + 180) / 360 * n)
    y0 = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    found = {}
    for dx in range(-span, span + 1):
        for dy in range(-span, span + 1):
            raw = reader.get(z, x0 + dx, y0 + dy)
            if raw is None:
                continue
            for layer, body in mvt.decode(plain(raw)).items():
                for f in body['features']:
                    p = f['properties']
                    if word in str(p.get('name', '')):
                        found[(layer, p.get('name'), p.get('name:en'), p.get('name:th-Latn'))] = 1
    print(f'  roads and places with "{word}" in the name near Chiang Mai: {len(found)}')
    for layer, name, en, lat_ in sorted(found, key=str)[:15]:
        print(f'    {layer}: name={name!r} name:en={en!r} name:th-Latn={lat_!r}')


if __name__ == '__main__':
    for u in sys.argv[1:]:
        try:
            check(u, 98.99, 18.79)
            if '/chiangmai/' in u:
                road_names(u)
        except Exception as ex:
            print('  FAILED:', repr(ex))
