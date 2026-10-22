#!/usr/bin/env python3
"""Counts what Foursquare's open places data (FSQ OS Places, Apache 2.0) could add to ours, without keeping any of it.

  HF_TOKEN=... MATCH_RULES='{...}' python3 pipeline/maps/count_fsq.py

Reads the release straight from Hugging Face with DuckDB. Counts open places in the matching restaurant category, places
whose names match our words, and a few related shop categories; then how many are not already near a place in our data
(our tiles are read from the bucket with the aws program).
"""
import glob, json, os, subprocess, tempfile

import duckdb

RELEASE = os.environ.get('FSQ_RELEASE', '2026-10-15')
BASE = f'hf://datasets/foursquare/fsq-os-places/release/dt={RELEASE}'
R = json.loads(os.environ['MATCH_RULES'])
db = duckdb.connect()
db.execute("INSTALL httpfs; LOAD httpfs;")
db.execute(f"CREATE SECRET hf (TYPE HUGGINGFACE, TOKEN '{os.environ['HF_TOKEN']}')")
db.execute(f"CREATE VIEW cats AS SELECT * FROM read_parquet('{BASE}/categories/parquet/*.parquet')")
print('category columns:', [c[0] for c in db.execute('DESCRIBE cats').fetchall()])
label_col = [c[0] for c in db.execute('DESCRIBE cats').fetchall() if 'label' in c[0] or 'name' in c[0]][0]
id_col = [c[0] for c in db.execute('DESCRIBE cats').fetchall() if c[0].endswith('id')][0]
words = R['k'].capitalize()
wanted = db.execute(f"""SELECT {id_col}, {label_col} FROM cats WHERE {label_col} ILIKE '%{words}%'
    OR {label_col} ILIKE '%Juice Bar%' OR {label_col} ILIKE '%Health Food%' OR {label_col} ILIKE '%Organic Grocery%'
    OR {label_col} ILIKE '%Farmers Market%'""").fetchall()
print('categories found:', wanted)
ids = [w[0] for w in wanted]
main = [w[0] for w in wanted if words.lower() in str(w[1]).lower()]

db.execute(f"""CREATE TABLE p AS SELECT fsq_place_id id, name, latitude lat, longitude lon, country, fsq_category_ids cats
    FROM read_parquet('{BASE}/places/parquet/*.parquet')
    WHERE date_closed IS NULL AND latitude IS NOT NULL
      AND (list_has_any(fsq_category_ids, {ids}) OR regexp_matches(lower(name), '{R['name_a']}') OR regexp_matches(lower(name), '{R['name_b']}'))""")
q = lambda s: db.execute(s).fetchone()[0]
print('total open places in the release:', q(f"SELECT count(*) FROM read_parquet('{BASE}/places/parquet/*.parquet') WHERE date_closed IS NULL"))
print('in the combined restaurant category:', q(f"SELECT count(*) FROM p WHERE list_has_any(cats, {main})"))
print('name matches word A:', q(f"SELECT count(*) FROM p WHERE regexp_matches(lower(name), '{R['name_a']}')"))
print('name matches word B:', q(f"SELECT count(*) FROM p WHERE regexp_matches(lower(name), '{R['name_b']}')"))
for cid, label in wanted:
    print(f'  {label}: {q(f"SELECT count(*) FROM p WHERE list_contains(cats, {chr(39)}{cid}{chr(39)})")}')

# Which of the restaurant-category and name-matched places we don't have yet (nothing of ours within about 100 m).
work = tempfile.mkdtemp()
subprocess.run(['aws', 's3', 'sync', f's3://{os.environ["S3_BUCKET"]}/tiles/', work, '--endpoint-url', os.environ['S3_ENDPOINT'],
                '--only-show-errors'], check=True)
ours = set()
import gzip
for f in glob.glob(os.path.join(work, '*.json')):
    raw = open(f, 'rb').read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    for pl in json.loads(raw).get('places', []):
        ours.add((round(pl['lat'] * 1000), round(pl['lon'] * 1000)))
rows = db.execute(f"""SELECT lat, lon, country FROM p WHERE list_has_any(cats, {main})
    OR regexp_matches(lower(name), '{R['name_a']}') OR regexp_matches(lower(name), '{R['name_b']}')""").fetchall()
new, by = 0, {}
for lat, lon, c in rows:
    a, b = round(lat * 1000), round(lon * 1000)
    if not any((a + i, b + j) in ours for i in (-1, 0, 1) for j in (-1, 0, 1)):
        new += 1
        by[c] = by.get(c, 0) + 1
print(f'restaurant category or name match: {len(rows)}; not near any place we already have: {new}')
print('new by country (top 25):', sorted(by.items(), key=lambda x: -x[1])[:25])
