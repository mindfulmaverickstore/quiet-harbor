#!/usr/bin/env python3
"""Makes the "light" copy of one of our maps: the same map without building outlines, and without the plain land-use
shading (housing estates, farmland, industry) at street zoom. Parks, woods, beaches, cemeteries, campuses and hospitals
stay. Roads, water, places and points of interest (including every food place) are untouched.

Measured on 22 Oct 2026: Langley and Surrey 35 MB -> about 19 MB; the British Columbia piece 320 MB -> about 239 MB.

  python3 pipeline/maps/make_light.py city.pmtiles light.pmtiles density.json

It also writes a "density" list: how many bytes of map sit in each zoom-13 square (about 5 by 5 km at the equator,
smaller further north). That lets us tell the size of any area (a town, a district) without cutting it first.
Plain Python, no extra packages. Reads and writes PMTiles version 3.
"""
import gzip, hashlib, json, struct, sys

DROP_LAYERS = {'buildings'}
# Land-use kinds left out at street zoom (15). Anything not listed stays, so a new kind is never lost by surprise.
PLAIN_LANDUSE = {
    'residential', 'commercial', 'industrial', 'retail', 'farmland', 'farmyard', 'railway', 'brownfield', 'construction',
    'garages', 'landfill', 'quarry', 'military', 'orchard', 'vineyard', 'plant_nursery', 'greenhouse_horticulture',
    'allotments', 'meadow', 'animal_keeping', 'religious', 'parking',
}
DENSITY_ZOOM = 13


# ---- Protocol buffers, just the parts a map tile needs ----

def varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def put_varint(v):
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        if v:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


def fields(b):
    """(field number, wire type, start of the whole field, value or (payload start, payload end), end)"""
    i = 0
    while i < len(b):
        start = i
        key, i = varint(b, i)
        no, wt = key >> 3, key & 7
        if wt == 0:
            v, i = varint(b, i)
        elif wt == 2:
            n, i = varint(b, i)
            v = (i, i + n)
            i += n
        elif wt == 1:
            v = b[i:i + 8]
            i += 8
        elif wt == 5:
            v = b[i:i + 4]
            i += 4
        else:
            raise ValueError('unexpected wire type')
        yield no, wt, start, v, i


def layer_name(body):
    for no, wt, _, v, _ in fields(body):
        if no == 1:
            return body[v[0]:v[1]].decode()
    return ''


def thin_landuse(body):
    """The landuse layer without the plain kinds."""
    keys, values, feats, other = [], [], [], []
    for no, wt, start, v, end in fields(body):
        if no == 3:
            keys.append(body[v[0]:v[1]].decode())
        elif no == 4:
            s = None
            val = body[v[0]:v[1]]
            for vno, vwt, _, vv, _ in fields(val):
                if vno == 1:
                    s = val[vv[0]:vv[1]].decode()
            values.append(s)
        if no == 2:
            feats.append((start, end, v))
        else:
            other.append(body[start:end])
    if 'kind' not in keys:
        return body
    kind_key = keys.index('kind')
    kept = []
    for start, end, (ps, pe) in feats:
        f = body[ps:pe]
        kind = None
        for fno, fwt, _, fv, _ in fields(f):
            if fno == 2:
                tags, j = [], fv[0]
                while j < fv[1]:
                    t, j = varint(f, j)
                    tags.append(t)
                for k in range(0, len(tags) - 1, 2):
                    if tags[k] == kind_key:
                        kind = values[tags[k + 1]]
        if kind not in PLAIN_LANDUSE:
            kept.append(body[start:end])
    # The name, keys, values and extent come first in the original order; features after them is also valid.
    return b''.join(other) + b''.join(kept)


def lighten(tile, z):
    out = []
    for no, wt, start, v, end in fields(tile):
        body = tile[v[0]:v[1]]
        name = layer_name(body)
        if name in DROP_LAYERS:
            continue
        if z >= 15 and name == 'landuse':
            body = thin_landuse(body)
            out.append(put_varint((no << 3) | 2) + put_varint(len(body)) + body)
        else:
            out.append(tile[start:end])
    return b''.join(out)


# ---- PMTiles version 3 ----

def zxy(tid):
    acc, z = 0, 0
    while acc + (1 << (2 * z)) <= tid:
        acc += 1 << (2 * z)
        z += 1
    n, t, x, y, s = 1 << z, tid - acc, 0, 0, 1
    while s < n:
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        t //= 4
        s *= 2
    return z, x, y


def read_dir(b):
    n, i = varint(b, 0)
    ids, runs, lens, offs = [], [], [], []
    last = 0
    for _ in range(n):
        v, i = varint(b, i)
        last += v
        ids.append(last)
    for _ in range(n):
        v, i = varint(b, i)
        runs.append(v)
    for _ in range(n):
        v, i = varint(b, i)
        lens.append(v)
    for k in range(n):
        v, i = varint(b, i)
        offs.append(offs[k - 1] + lens[k - 1] if v == 0 and k > 0 else v - 1)
    return list(zip(ids, runs, lens, offs))


def write_dir(entries):
    out = [put_varint(len(entries))]
    last = 0
    for e in entries:
        out.append(put_varint(e[0] - last))
        last = e[0]
    out += [put_varint(e[1]) for e in entries]
    out += [put_varint(e[2]) for e in entries]
    for k, e in enumerate(entries):
        nxt = k > 0 and e[3] == entries[k - 1][3] + entries[k - 1][2]
        out.append(put_varint(0 if nxt else e[3] + 1))
    return gzip.compress(b''.join(out), 9)


def read_pmtiles(path):
    f = open(path, 'rb').read()
    h = f[:127]
    if h[:7] != b'PMTiles' or h[7] != 3:
        raise ValueError('not a PMTiles v3 file')
    rdo, rdl, mo, ml, ldo, ldl, tdo, tdl = struct.unpack('<8Q', h[8:72])
    ic, tc = h[97], h[98]
    dec = (lambda x: gzip.decompress(x)) if ic == 2 else (lambda x: x)
    tiles = []  # (tile id, run length, bytes of the tile as stored)

    def walk(off, length):
        for tid, run, ln, o in read_dir(dec(f[off:off + length])):
            if run == 0:
                walk(ldo + o, ln)
            else:
                tiles.append((tid, run, f[tdo + o:tdo + o + ln]))

    walk(rdo, rdl)
    tiles.sort()
    return h, f[mo:mo + ml], ic, tc, tiles


def write_pmtiles(path, header, meta, tiles):
    """tiles: (tile id, run length, gzipped tile), sorted. Identical tiles are stored once."""
    data, where, entries = [], {}, []
    pos = 0
    for tid, run, blob in tiles:
        key = hashlib.sha1(blob).digest()
        if key not in where:
            where[key] = (pos, len(blob))
            data.append(blob)
            pos += len(blob)
        off, ln = where[key]
        last = entries[-1] if entries else None
        if last and last[3] == off and last[0] + last[1] == tid:
            entries[-1] = (last[0], last[1] + run, ln, off)
        else:
            entries.append((tid, run, ln, off))
    # The first directory must fit in the first 16 KB with the header; otherwise it points to smaller "leaf" lists.
    root, leaves = write_dir(entries), b''
    size = 4096
    while 127 + len(root) > 16384:
        leaf_entries, parts, lpos = [], [], 0
        for k in range(0, len(entries), size):
            chunk = write_dir(entries[k:k + size])
            leaf_entries.append((entries[k][0], 0, len(chunk), lpos))
            parts.append(chunk)
            lpos += len(chunk)
        root, leaves = write_dir(leaf_entries), b''.join(parts)
        size *= 2
    tile_data = b''.join(data)
    n_addressed = sum(e[1] for e in entries)
    head = bytearray(header[:127])
    offsets = [127, len(root), 127 + len(root), len(meta), 127 + len(root) + len(meta), len(leaves),
               127 + len(root) + len(meta) + len(leaves), len(tile_data), n_addressed, len(entries), len(data)]
    head[8:96] = struct.pack('<11Q', *offsets)
    head[96], head[97] = 1, 2  # clustered; directories gzipped
    with open(path, 'wb') as out:
        out.write(bytes(head) + root + meta + leaves + tile_data)


def main(src, dst, density_path):
    header, meta, ic, tc, tiles = read_pmtiles(src)
    if ic != 2:
        meta = gzip.compress(meta, 9)
    out, density = [], {}
    for tid, run, blob in tiles:
        z, x, y = zxy(tid)
        raw = gzip.decompress(blob) if tc == 2 else blob
        light = gzip.compress(lighten(raw, z), 9)
        out.append((tid, run, light))
        if z >= DENSITY_ZOOM:
            k = f'{x >> (z - DENSITY_ZOOM)}/{y >> (z - DENSITY_ZOOM)}'
            density[k] = density.get(k, 0) + len(light) * run
    header = bytearray(header)
    header[98] = 2  # tiles gzipped
    write_pmtiles(dst, bytes(header), meta, out)
    json.dump({'z': DENSITY_ZOOM, 'bytes': density}, open(density_path, 'w'), separators=(',', ':'))


def density_only(src, density_path):
    """The density list of a map as it is (full detail), without making a light copy."""
    _, _, _, _, tiles = read_pmtiles(src)
    density = {}
    for tid, run, blob in tiles:
        z, x, y = zxy(tid)
        if z >= DENSITY_ZOOM:
            k = f'{x >> (z - DENSITY_ZOOM)}/{y >> (z - DENSITY_ZOOM)}'
            density[k] = density.get(k, 0) + len(blob) * run
    json.dump({'z': DENSITY_ZOOM, 'bytes': density}, open(density_path, 'w'), separators=(',', ':'))


if __name__ == '__main__':
    main(*sys.argv[1:4])
