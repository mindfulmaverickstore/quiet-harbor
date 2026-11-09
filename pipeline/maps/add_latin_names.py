#!/usr/bin/env python3
"""Adds a Latin-letter spelling to the names on a map that exist only in Thai script, so a traveller who cannot read Thai
still sees something readable ("Thanon Nimman He Min" next to the Thai street name's place on the map).

  python3 pipeline/maps/add_latin_names.py city.pmtiles city-latin.pmtiles

Every feature whose `name` contains Thai letters and that has no `name:en` of its own gets a `name:en` made by the Royal
Thai General System of Transcription (pythainlp's "royin" engine). Names OpenStreetMap already gives in English are never
touched, and nothing else in the tile changes. The app already prefers `name:en` over `name` for English-reading phones
(src/components/ourMapStyle.ts), so no app change is needed.

The spelling is a machine transliteration: right for most ordinary words, rough for some proper names. A hand-made
correction list (FIXES below) covers the most common wrong ones.

Needs: pythainlp. Reads and writes PMTiles version 3, using the same plain-Python tile reading as make_light.py.
"""
import functools, gzip, multiprocessing, re, sys

sys.path.insert(0, __file__.rsplit('/', 1)[0] if '/' in __file__ else '.')
from make_light import fields, put_varint, read_pmtiles, write_pmtiles  # noqa: E402

THAI = re.compile('[฀-๿]')
# Words the transliteration gets wrong, or whose usual English spelling is not the official transcription (Mahidol,
# Sukhumvit, Central). Found inside any name, longest first, so a compound is matched before its parts. Add to this list
# whenever someone spots a wrong spelling on the map; rebuild the Thailand maps to apply it.
FIXES = {
    'เชียงใหม่': 'Chiang Mai', 'ใหม่': 'Mai', 'ไหม': 'Mai', 'ไทย': 'Thai', 'ตลาด': 'Talat',
    'มหิดล': 'Mahidol', 'เซ็นทรัล': 'Central', 'เซนทรัล': 'Central', 'สุขุมวิท': 'Sukhumvit', 'ราชดำเนิน': 'Ratchadamnoen',
    'วโรรส': 'Warorot', 'นิมมานเหมินท์': 'Nimmanhaemin', 'ลาดพร้าว': 'Lat Phrao', 'พหลโยธิน': 'Phahonyothin', 'สีลม': 'Silom',
    'สาทร': 'Sathon', 'เพชรบุรี': 'Phetchaburi', 'รามคำแหง': 'Ramkhamhaeng', 'ราชวิถี': 'Ratchawithi', 'พญาไท': 'Phaya Thai',
    'ศรีนครินทร์': 'Si Nakarin', 'ห้วยขวาง': 'Huai Khwang', 'เจริญกรุง': 'Charoen Krung', 'ท่าแพ': 'Tha Phae',
    'ช้างคลาน': 'Chang Khlan', 'ห้วยแก้ว': 'Huai Kaeo', 'สุเทพ': 'Suthep', 'ช้างเผือก': 'Chang Phueak', 'มหาราช': 'Maharat',
    'สุวรรณภูมิ': 'Suvarnabhumi', 'ดอนเมือง': 'Don Mueang', 'พระราม': 'Rama', 'กรุงเทพ': 'Bangkok', 'ภูเก็ต': 'Phuket',
}
_FIXES_RE = re.compile('(' + '|'.join(re.escape(k) for k in sorted(FIXES, key=len, reverse=True)) + ')')


@functools.lru_cache(maxsize=None)
def latin(name):
    """The Latin spelling of a name, or None when there is none to add."""
    from pythainlp.tokenize import word_tokenize
    from pythainlp.transliterate import romanize

    out = []
    for piece in _FIXES_RE.split(name):
        if piece in FIXES:
            out.append(f' {FIXES[piece]} ')
            continue
        for word in word_tokenize(piece, keep_whitespace=True) if piece else []:
            if not THAI.search(word):
                out.append(word)  # digits, Latin letters, spaces and punctuation stay as written
            else:
                out.append(f' {romanize(word, engine="royin").strip().capitalize()} ')
    text = re.sub(r'\s+', ' ', ''.join(out)).strip()
    if THAI.search(text):
        return None  # the transliteration could not read part of it (often a foreign word): better no spelling than a garbled one
    # "ตลาดวโรรส Warorot" must not become "Talat Warorot Warorot".
    words = []
    for w in text.split(' '):
        if not words or words[-1].lower() != w.lower():
            words.append(w)
    text = ' '.join(words)
    return text if text and text != name else None


def string_of(value_message):
    for no, wt, _, v, _ in fields(value_message):
        if no == 1 and wt == 2:
            return value_message[v[0]:v[1]].decode('utf-8', 'replace')
    return None


def rewrite_layer(body):
    """The layer with `name:en` added where a Thai-only name has none; the same bytes back when nothing needs adding."""
    keys, values, feats, other = [], [], [], []
    for no, wt, start, v, end in fields(body):
        if no == 3:
            keys.append(body[v[0]:v[1]].decode())
        elif no == 4:
            values.append(body[v[0]:v[1]])
        elif no == 2:
            feats.append(body[v[0]:v[1]])
        else:
            other.append(body[start:end])
    if 'name' not in keys:
        return body
    name_key = keys.index('name')
    en_key = keys.index('name:en') if 'name:en' in keys else len(keys)
    value_ids = {}
    new_values = []

    def value_id(text):
        if text not in value_ids:
            value_ids[text] = len(values) + len(new_values)
            data = text.encode()
            new_values.append(b'\x0a' + put_varint(len(data)) + data)
        return value_ids[text]

    changed = False
    out_feats = []
    for f in feats:
        parts, tags = [], []
        for no, wt, start, v, end in fields(f):
            if no == 2 and wt == 2:
                i = v[0]
                while i < v[1]:
                    t = 0
                    s = 0
                    while True:
                        c = f[i]
                        i += 1
                        t |= (c & 0x7F) << s
                        s += 7
                        if c < 0x80:
                            break
                    tags.append(t)
                parts.append(None)  # where the tags go
            else:
                parts.append(f[start:end])
        pairs = list(zip(tags[::2], tags[1::2]))
        spelt = None
        if any(k == name_key for k, _ in pairs) and not any(k == en_key for k, _ in pairs):
            text = string_of(values[next(v for k, v in pairs if k == name_key)])
            if text and THAI.search(text):
                spelt = latin(text)
        if spelt:
            tags = tags + [en_key, value_id(spelt)]
            changed = True
        packed = b''.join(put_varint(t) for t in tags)
        out_feats.append(b''.join((b'\x12' + put_varint(len(packed)) + packed) if p is None else p for p in parts))
    if not changed:
        return body
    if en_key == len(keys):
        keys.append('name:en')
    res = [b''.join(other)]
    res += [b'\x1a' + put_varint(len(k.encode())) + k.encode() for k in keys]
    res += [b'\x22' + put_varint(len(v)) + v for v in values + new_values]
    res += [b'\x12' + put_varint(len(f)) + f for f in out_feats]
    return b''.join(res)


def rewrite_tile(tile):
    # Quick skip: a tile with no Thai letters (UTF-8 starts E0 B8 or E0 B9) has nothing to add.
    if b'\xe0\xb8' not in tile and b'\xe0\xb9' not in tile:
        return tile
    out = []
    for no, wt, start, v, end in fields(tile):
        if no == 3:
            body = rewrite_layer(tile[v[0]:v[1]])
            out.append(b'\x1a' + put_varint(len(body)) + body)
        else:
            out.append(tile[start:end])
    return b''.join(out)


def work(item):
    tid, run, blob, gz = item
    raw = gzip.decompress(blob) if gz else blob
    new = rewrite_tile(raw)
    if new is raw and gz:
        return tid, run, blob  # nothing added: the tile is kept exactly as it was
    return tid, run, gzip.compress(new, 9)


def main(src, dst, processes=None):
    header, meta, ic, tc, tiles = read_pmtiles(src)
    if ic != 2:
        meta = gzip.compress(meta, 9)
    items = [(tid, run, blob, tc == 2) for tid, run, blob in tiles]
    with multiprocessing.Pool(processes) as pool:
        done = list(pool.imap(work, items, chunksize=64))
    header = bytearray(header)
    header[98] = 2  # tiles gzipped
    write_pmtiles(dst, bytes(header), meta, done)
    before = sum(len(b) for _, _, b in tiles)
    after = sum(len(b) for _, _, b in done)
    print(f'{len(tiles)} tiles; tile bytes {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB ({(after / before - 1) * 100:+.1f}%)')


if __name__ == '__main__':
    main(*sys.argv[1:3])
