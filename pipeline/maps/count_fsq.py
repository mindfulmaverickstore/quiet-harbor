#!/usr/bin/env python3
"""Counts what Foursquare's open places data (FSQ OS Places, Apache 2.0) could add to ours, and how a "checked in the last
two years" rule would affect it and the data we already use. Nothing is kept.

  HF_TOKEN=... MATCH_RULES='{...}' python3 pipeline/maps/count_fsq.py

Foursquare: read straight from Hugging Face with DuckDB. Ours: the tiles in the bucket (aws program).
Overture: the update dates of the places we use, read from Overture's public release.
OpenStreetMap: counts from the Overpass server of matching places last edited in the past two years.
"""
import datetime, glob, gzip, json, math, os, re, subprocess, tempfile, unicodedata, urllib.parse, urllib.request

import duckdb

RELEASE = os.environ.get('FSQ_RELEASE', '2026-10-15')
BASE = f'hf://datasets/foursquare/fsq-os-places/release/dt={RELEASE}'
R = json.loads(os.environ['MATCH_RULES'])
CUTOFF = (datetime.date.today() - datetime.timedelta(days=730)).isoformat()
db = duckdb.connect()
db.execute("INSTALL httpfs; LOAD httpfs;")
db.execute(f"CREATE SECRET hf (TYPE HUGGINGFACE, TOKEN '{os.environ['HF_TOKEN']}')")
db.execute("SET s3_region='us-west-2'")
q = lambda s: db.execute(s).fetchone()[0]
print(f'Two-year rule: checked or edited on or after {CUTOFF}')

main = '4bf58dd8d48988d1d3941735'  # the combined restaurant category
db.execute(f"""CREATE TABLE p AS SELECT name, latitude lat, longitude lon, country, date_refreshed
    FROM read_parquet('{BASE}/places/parquet/*.parquet')
    WHERE date_closed IS NULL AND latitude IS NOT NULL
      AND (list_contains(fsq_category_ids, '{main}') OR regexp_matches(lower(name), '{R['name_a']}') OR regexp_matches(lower(name), '{R['name_b']}'))""")
fresh = f"CAST(date_refreshed AS DATE) >= DATE '{CUTOFF}'"
print('FSQ matches:', q('SELECT count(*) FROM p'), '| refreshed in two years:', q(f'SELECT count(*) FROM p WHERE {fresh}'))

# Ours.
work = tempfile.mkdtemp()
subprocess.run(['aws', 's3', 'sync', f's3://{os.environ["S3_BUCKET"]}/tiles/', work, '--endpoint-url', os.environ['S3_ENDPOINT'],
                '--only-show-errors'], check=True)
norm = lambda s: re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', (s or '').lower()))
grid = {}
osm_n = ov_n = osm_checked = 0
ov_ids = []
for f in glob.glob(os.path.join(work, '*.json')):
    raw = open(f, 'rb').read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    for pl in json.loads(raw).get('places', []):
        grid.setdefault((int(pl['lat'] / 0.0005), int(pl['lon'] / 0.0005)), []).append((pl['lat'], pl['lon'], norm(pl['tags'].get('name'))))
        if pl['id'].startswith('ov/'):
            ov_n += 1
            ov_ids.append(pl['id'][3:])
        else:
            osm_n += 1
            d = max((v for k, v in pl['tags'].items() if k.startswith('check_date')), default='')
            osm_checked += d >= CUTOFF


def metres(a, b, c, d):
    return math.hypot((d - b) * 111320 * math.cos(math.radians(a)), (c - a) * 110540)


def known(lat, lon, name, near):
    """Is one of ours within `near` metres, or one with the same name within 150 m?"""
    gy, gx = int(lat / 0.0005), int(lon / 0.0005)
    n = norm(name)
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            for a, b, nm in grid.get((gy + dy, gx + dx), []):
                m = metres(lat, lon, a, b)
                if m <= near or (n and nm and (n in nm or nm in n) and m <= 150):
                    return True
    return False


rows = db.execute(f'SELECT lat, lon, name, country, {fresh} FROM p').fetchall()
res = {}
for near in (30, 100):
    new = new_fresh = 0
    by = {}
    for lat, lon, name, c, f in rows:
        if not known(lat, lon, name, near):
            new += 1
            if f:
                new_fresh += 1
                by[c] = by.get(c, 0) + 1
    res[near] = (new, new_fresh, by)
    print(f'FSQ not already ours (within {near} m, or same name within 150 m): {new} | of those refreshed in two years: {new_fresh}')
print('New and fresh by country (30 m rule, top 30):', sorted(res[30][2].items(), key=lambda x: -x[1])[:30])

# Overture: when Overture's sources last updated the places we use.
rel = json.load(open(glob.glob(os.path.join(work, 'index.json'))[0])).get('overture') if glob.glob(os.path.join(work, 'index.json')) else None
if not rel:
    rel = json.load(urllib.request.urlopen(urllib.request.Request('https://pub-e5b664167e394466a0e48820dffb17df.r2.dev/index.json', headers={'User-Agent': 'x'})))['overture']
db.execute('CREATE TABLE ours AS SELECT unnest(?) id', [ov_ids])
ov_fresh = q(f"""SELECT count(*) FROM read_parquet('s3://overturemaps-us-west-2/release/{rel}/theme=places/type=place/*', hive_partitioning=1) o
    JOIN ours ON o.id = ours.id
    WHERE list_max([CAST(x.update_time AS DATE) for x in o.sources]) >= DATE '{CUTOFF}'""")
print(f'Overture places in our data: {ov_n} | updated by a source in two years: {ov_fresh}')

# OpenStreetMap: our places, and how many matching ones were edited in two years (any change to the place counts).
print(f'OpenStreetMap places in our data: {osm_n} | with a check date in two years: {osm_checked}')
for key in ('diet:' + R['k'], 'diet:' + R['cat_b'].split('_')[0]):
    for newer in ('', f'(newer:"{CUTOFF}T00:00:00Z")'):
        body = f'[out:json][timeout:900];nwr["{key}"~"^(only|yes|limited)$"]{newer};out count;'
        try:
            r = urllib.request.urlopen(urllib.request.Request('https://overpass-api.de/api/interpreter', data=urllib.parse.urlencode({'data': body}).encode(),
                                                               headers={'User-Agent': 'map-build/1.0'}), timeout=1000)
            c = json.load(r)['elements'][0]['tags']['total']
        except Exception as e:
            c = f'failed ({e})'
        print(f'OSM {key} (only/yes/limited){" edited in two years" if newer else ""}: {c}')
