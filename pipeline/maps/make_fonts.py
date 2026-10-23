#!/usr/bin/env python3
"""Makes the map's letters: Roboto (the typeface Google Maps uses, Apache 2.0, from the openmaptiles/fonts release) for
Latin, Greek and Cyrillic, with Noto Sans filling in every other script, merged into one set per style:

  shared/glyphs/VF Regular/<range>.pbf     Roboto Regular + Noto Sans Regular
  shared/glyphs/VF Medium/<range>.pbf      Roboto Medium  + Noto Sans Medium
  shared/glyphs/VF Italic/<range>.pbf      Roboto Italic  + Noto Sans Italic

  python3 pipeline/maps/make_fonts.py <folder with the unzipped openmaptiles fonts> <out folder>

Noto Sans comes from our own shared/glyphs (already published). Plain Python; reads and writes the glyph files directly.
"""
import os, sys, urllib.parse, urllib.request

PUBLIC = 'https://pub-e5b664167e394466a0e48820dffb17df.r2.dev/maps/shared/glyphs'
PAIRS = {'VF Regular': ('Roboto Regular', 'Noto Sans Regular'), 'VF Medium': ('Roboto Medium', 'Noto Sans Medium'),
         'VF Italic': ('Roboto Italic', 'Noto Sans Italic')}


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


def field(no, payload):
    return put_varint((no << 3) | 2) + put_varint(len(payload)) + payload


def glyphs_of(pbf):
    """{codepoint: raw glyph message} from a glyphs file (glyphs > stacks > glyph)."""
    out = {}
    i = 0
    while i < len(pbf):
        key, i = varint(pbf, i)
        n, i = varint(pbf, i)
        stack, i = pbf[i:i + n], i + n
        if key >> 3 != 1:
            continue
        j = 0
        while j < len(stack):
            k, j = varint(stack, j)
            if k & 7 != 2:
                _, j = varint(stack, j)
                continue
            m, j = varint(stack, j)
            body, j = stack[j:j + m], j + m
            if k >> 3 == 3:  # glyph
                gid, _ = varint(body, 1)  # field 1 (id) comes first
                out[gid] = body
    return out


def noto(name, rng):
    url = f'{PUBLIC}/{urllib.parse.quote(name)}/{rng}.pbf'
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'map-build/1.0'}), timeout=60).read()
    except Exception:
        return b''


def main(src, out):
    for merged, (roboto, noto_name) in PAIRS.items():
        os.makedirs(os.path.join(out, merged), exist_ok=True)
        for start in range(0, 65536, 256):
            rng = f'{start}-{start + 255}'
            path = os.path.join(src, roboto, f'{rng}.pbf')
            glyphs = glyphs_of(open(path, 'rb').read()) if os.path.exists(path) else {}
            for gid, body in glyphs_of(noto(noto_name, rng)).items():
                glyphs.setdefault(gid, body)
            stack = field(1, merged.encode()) + field(2, rng.encode()) + b''.join(field(3, glyphs[g]) for g in sorted(glyphs))
            with open(os.path.join(out, merged, f'{rng}.pbf'), 'wb') as f:
                f.write(field(1, stack))
        print(merged, 'done', flush=True)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
