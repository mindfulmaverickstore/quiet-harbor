#!/usr/bin/env python3
"""Builds the Foursquare layer: places from FSQ OS Places (Apache 2.0) that match our words or the combined restaurant
category and that we don't already have, one file per 1-degree square like our place data: fsq/<lat>_<lon>.json.

  HF_TOKEN=... MATCH_RULES='{...}' python3 pipeline/maps/build_fsq.py

A place is "already ours" if one of ours is within 30 m, or one with the same name within 150 m. For those matched by
name, any website, phone, Instagram or Facebook that we lack is listed under "fill" (gaps only; nothing is replaced). Each kept place has
"fresh": true when Foursquare refreshed it in the last two years (shown as an unconfirmed pin), false otherwise (shown as
a grey dot). It also counts how many of the Overture places we use are closed or low-confidence, to decide on a filter.
"""
import datetime, glob, gzip, json, math, os, re, subprocess, tempfile, unicodedata, urllib.request

import duckdb

RELEASE = os.environ.get('FSQ_RELEASE', '2026-10-15')
BASE = f'hf://datasets/foursquare/fsq-os-places/release/dt={RELEASE}'
R = json.loads(os.environ['MATCH_RULES'])
CUTOFF = (datetime.date.today() - datetime.timedelta(days=730)).isoformat()
MAIN = '4bf58dd8d48988d1d3941735'  # the combined restaurant category
ENDPOINT, BUCKET = os.environ['S3_ENDPOINT'], os.environ['S3_BUCKET']

db = duckdb.connect()
db.execute("INSTALL httpfs; LOAD httpfs;")
db.execute(f"CREATE SECRET hf (TYPE HUGGINGFACE, TOKEN '{os.environ['HF_TOKEN']}')")
cols = {c[0] for c in db.execute(f"DESCRIBE SELECT * FROM read_parquet('{BASE}/places/parquet/*.parquet')").fetchall()}
want = ['fsq_place_id', 'name', 'latitude', 'longitude', 'address', 'locality', 'postcode', 'country', 'website', 'tel',
        'instagram', 'facebook_id', 'fsq_category_labels', 'date_refreshed']
have = [c for c in want if c in cols]
print('using columns:', have)
rows = db.execute(f"""SELECT {', '.join(have)} FROM read_parquet('{BASE}/places/parquet/*.parquet')
    WHERE date_closed IS NULL AND latitude IS NOT NULL
      AND (list_contains(fsq_category_ids, '{MAIN}') OR regexp_matches(lower(name), '{R['name_a']}') OR regexp_matches(lower(name), '{R['name_b']}'))""").fetchall()
rows = [dict(zip(have, r)) for r in rows]


def food_place(r):
    """A name match alone isn't enough (a dance school whose name holds our word): the place must also be one that serves food or drink.
    This is only for the food layer. Places that serve no food but matter to us (animal sanctuaries, shelters) are planned
    as a layer of their own, chosen by their own categories, so this rule never hides them."""
    labels = ' | '.join(r.get('fsq_category_labels') or [])
    return bool(re.search(r'Dining and Drinking|Food|Restaurant|Caf|Bakery|Grocery|Market|Juice|Dessert|Ice Cream|Deli', labels))


before = len(rows)
rows = [r for r in rows if food_place(r)]
print('Foursquare matches:', len(rows), f'({before - len(rows)} dropped: not a place to eat or drink)')

work = tempfile.mkdtemp()
subprocess.run(['aws', 's3', 'sync', f's3://{BUCKET}/tiles/', work, '--endpoint-url', ENDPOINT, '--only-show-errors'], check=True)
norm = lambda s: re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', (s or '').lower()))
grid, ov_ids = {}, []
for f in glob.glob(os.path.join(work, '*.json')):
    raw = open(f, 'rb').read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    for pl in json.loads(raw).get('places', []):
        grid.setdefault((int(pl['lat'] / 0.0005), int(pl['lon'] / 0.0005)), []).append((pl['lat'], pl['lon'], norm(pl['tags'].get('name')), pl))
        if pl['id'].startswith('ov/'):
            ov_ids.append(pl['id'][3:])


def metres(a, b, c, d):
    return math.hypot((d - b) * 111320 * math.cos(math.radians(a)), (c - a) * 110540)


def known(lat, lon, name):
    """Our place for this Foursquare one, if we have it: the same name within 150 m, else anything within 30 m."""
    gy, gx = int(lat / 0.0005), int(lon / 0.0005)
    n = norm(name)
    near = None
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            for a, b, nm, pl in grid.get((gy + dy, gx + dx), []):
                m = metres(lat, lon, a, b)
                if n and nm and (n in nm or nm in n) and m <= 150:
                    return pl
                if m <= 30 and near is None:
                    near = pl
    return near


# Details we may lack, and where OpenStreetMap or Overture would keep them. We only ever fill a gap; we never replace.
FILL = {'website': ('website', 'contact:website'), 'phone': ('phone', 'contact:phone'), 'instagram': ('contact:instagram', 'instagram'),
        'facebook': ('contact:facebook', 'facebook')}


def kind(labels):
    s = ' '.join(labels or []).lower()
    return 'cafe' if 'café' in s or 'cafe' in s or 'coffee' in s else 'fast_food' if 'fast food' in s else 'restaurant'


tiles = {}
fills = {}
gap_counts = {}
fresh_n = 0
for r in rows:
    ours = known(r['latitude'], r['longitude'], r['name'])
    if ours:
        # Same place: only note what Foursquare knows that we don't (only matched by name, to be sure it's the same place).
        if norm(ours['tags'].get('name')) and (norm(r['name']) in norm(ours['tags'].get('name')) or norm(ours['tags'].get('name')) in norm(r['name'])):
            add = {}
            for key, src in (('website', 'website'), ('phone', 'tel'), ('instagram', 'instagram'), ('facebook', 'facebook_id')):
                if r.get(src) and not any(k in ours['tags'] for k in FILL[key]):
                    add[key] = str(r[src])
                    gap_counts[key] = gap_counts.get(key, 0) + 1
            if add:
                fkey = f'{math.floor(ours["lat"])}_{math.floor(ours["lon"])}'
                fills.setdefault(fkey, []).append({'id': ours['id'], **add})
        continue
    refreshed = str(r.get('date_refreshed') or '')[:10]
    fresh = refreshed >= CUTOFF
    fresh_n += fresh
    p = {'id': f'fsq/{r["fsq_place_id"]}', 'name': r['name'], 'lat': round(r['latitude'], 6), 'lon': round(r['longitude'], 6),
         'kind': kind(r.get('fsq_category_labels')), 'fresh': fresh, 'refreshed': refreshed}
    for src, dst in (('address', 'street'), ('locality', 'city'), ('postcode', 'postcode'), ('website', 'website'), ('tel', 'phone'),
                     ('instagram', 'instagram'), ('facebook_id', 'facebook')):
        if r.get(src):
            p[dst] = str(r[src])
    key = f'{math.floor(r["latitude"])}_{math.floor(r["longitude"])}'
    tiles.setdefault(key, []).append(p)
total = sum(len(v) for v in tiles.values())
print(f'kept {total} new places ({fresh_n} refreshed since {CUTOFF}) in {len(tiles)} squares')
print(f'gaps in our places that Foursquare fills: {gap_counts} (places: {sum(len(v) for v in fills.values())})')

out = tempfile.mkdtemp()
for key in set(tiles) | set(fills):
    with open(os.path.join(out, f'{key}.json'), 'wb') as f:
        f.write(gzip.compress(json.dumps({'places': tiles.get(key, []), 'fill': fills.get(key, [])}, ensure_ascii=False, separators=(',', ':')).encode(), 9))
with open(os.path.join(out, 'index.json'), 'w') as f:
    json.dump({'built': datetime.date.today().isoformat(), 'release': RELEASE, 'fresh_since': CUTOFF,
               'credit': 'Foursquare Open Source Places (Apache 2.0)',
               'tiles': {k: len(tiles.get(k, [])) + len(fills.get(k, [])) for k in set(tiles) | set(fills)}}, f)
subprocess.run(['aws', 's3', 'rm', f's3://{BUCKET}/fsq/', '--recursive', '--endpoint-url', ENDPOINT, '--only-show-errors'], check=True)
subprocess.run(['aws', 's3', 'cp', out, f's3://{BUCKET}/fsq/', '--recursive', '--endpoint-url', ENDPOINT, '--content-type',
                'application/json', '--content-encoding', 'gzip', '--exclude', 'index.json', '--only-show-errors'], check=True)
subprocess.run(['aws', 's3', 'cp', os.path.join(out, 'index.json'), f's3://{BUCKET}/fsq/index.json', '--endpoint-url', ENDPOINT,
                '--content-type', 'application/json', '--only-show-errors'], check=True)
print('uploaded to fsq/')

# Overture: how many of the places we use are closed or low-confidence.
rel = json.load(urllib.request.urlopen(urllib.request.Request('https://pub-e5b664167e394466a0e48820dffb17df.r2.dev/index.json',
                                                               headers={'User-Agent': 'x'})))['overture']
db.execute("CREATE OR REPLACE SECRET ov (TYPE S3, KEY_ID '', SECRET '', REGION 'us-west-2', SCOPE 's3://overturemaps-us-west-2')")
db.execute('CREATE TABLE ours AS SELECT unnest(?) id', [ov_ids])
try:
    res = db.execute(f"""SELECT operating_status, CASE WHEN confidence IS NULL THEN 'none' WHEN confidence < 0.3 THEN '<0.3'
        WHEN confidence < 0.5 THEN '0.3-0.5' WHEN confidence < 0.7 THEN '0.5-0.7' ELSE '0.7+' END c, count(*)
        FROM read_parquet('s3://overturemaps-us-west-2/release/{rel}/theme=places/type=place/*', hive_partitioning=1) o
        JOIN ours ON o.id = ours.id GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    print(f'Overture places in our data: {len(ov_ids)}')
    for st, c, n in res:
        print(f'  status {st}, confidence {c}: {n}')
except Exception as e:
    print('Overture check failed:', e)
