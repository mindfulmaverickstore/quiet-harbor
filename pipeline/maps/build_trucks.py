#!/usr/bin/env python3
"""Builds the food truck layer: places that are food trucks or street food stalls, one file per 1-degree square like our place
data: trucks/<lat>_<lon>.json (gzip), plus trucks/index.json.

  HF_TOKEN=... MATCH_RULES='{...}' python3 pipeline/maps/build_trucks.py

Sources: Overture Maps, category "food_truck_stand" (CDLA Permissive 2.0), and Foursquare Open Source Places, category "Food Truck"
(Apache 2.0). Closed ones and ones Overture doubts exist (confidence under 0.3) are left out. A truck seen in both is kept once.
Trucks move, so each one carries the day its source last had it ("updated") and the layer says so; "diet" is set only when
the name carries one of our matching words (MATCH_RULES), since neither source flags diets for trucks.
"""
import datetime, gzip, json, math, os, re, subprocess, tempfile, unicodedata, urllib.request

import duckdb

RULES = json.loads(os.environ['MATCH_RULES'])
ENDPOINT, BUCKET = os.environ['S3_ENDPOINT'], os.environ['S3_BUCKET']
FSQ_RELEASE = os.environ.get('FSQ_RELEASE', '2026-10-15')
FSQ_TRUCK = '4bf58dd8d48988d1cb941735'
# Trucks come and go: only Foursquare listings refreshed in the last two years are kept.
CUTOFF = (datetime.date.today() - datetime.timedelta(days=730)).isoformat()
NAME_A = re.compile(RULES['name_a'])
norm = lambda s: re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', (s or '').lower()))

cat = json.load(urllib.request.urlopen('https://stac.overturemaps.org/catalog.json', timeout=30))
OVERTURE = cat['latest']
OV_DAY = OVERTURE[:10]
db = duckdb.connect()
db.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
# Overture's files are public: read them without our storage keys (they would be refused).
db.execute("CREATE OR REPLACE SECRET ov (TYPE S3, KEY_ID '', SECRET '', REGION 'us-west-2', SCOPE 's3://overturemaps-us-west-2')")

ov = db.execute(f"""
    select id, names.primary, bbox.ymin, bbox.xmin, websites[1], phones[1], addresses[1].freeform, addresses[1].locality
    from read_parquet('s3://overturemaps-us-west-2/release/{OVERTURE}/theme=places/type=place/*', hive_partitioning=1)
    where taxonomy.primary = 'food_truck_stand' and names.primary is not null
      and coalesce(operating_status, 'open') <> 'permanently_closed' and (confidence is null or confidence >= 0.3)""").fetchall()
print('Overture trucks:', len(ov))

db.execute(f"CREATE SECRET hf (TYPE HUGGINGFACE, TOKEN '{os.environ['HF_TOKEN']}')")
fsq = db.execute(f"""
    select fsq_place_id, name, latitude, longitude, website, tel, address, locality, cast(date_refreshed as varchar)
    from read_parquet('hf://datasets/foursquare/fsq-os-places/release/dt={FSQ_RELEASE}/places/parquet/*.parquet')
    where date_closed is null and name is not null and latitude is not null and list_contains(fsq_category_ids, '{FSQ_TRUCK}')
      and cast(date_refreshed as varchar) >= '{CUTOFF}'""").fetchall()
print('Foursquare trucks:', len(fsq))

trucks, grid = [], {}
def add(t):
    gy, gx = int(t['lat'] / 0.0005), int(t['lon'] / 0.0005)
    n = norm(t['name'])
    # The same truck in both sources: within 60 m with a similar name.
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for o in grid.get((gy + dy, gx + dx), []):
                if abs(o['lat'] - t['lat']) < 0.0006 and abs(o['lon'] - t['lon']) < 0.0008:
                    m = norm(o['name'])
                    if n and m and (n in m or m in n):
                        for k in ('website', 'phone', 'street', 'city'):
                            if t.get(k) and not o.get(k):
                                o[k] = t[k]
                        return
    grid.setdefault((gy, gx), []).append(t)
    trucks.append(t)

for gid, name, lat, lon, web, phone, street, city in ov:
    t = {'id': f'ov/{gid}', 'name': name, 'lat': round(lat, 6), 'lon': round(lon, 6), 'src': 'Overture Maps', 'updated': OV_DAY}
    for k, v in (('website', web), ('phone', phone), ('street', street), ('city', city)):
        if v: t[k] = str(v)
    add(t)
for fid, name, lat, lon, web, phone, street, city, refreshed in fsq:
    t = {'id': f'fsq/{fid}', 'name': name, 'lat': round(lat, 6), 'lon': round(lon, 6), 'src': 'Foursquare', 'updated': (refreshed or '')[:10]}
    for k, v in (('website', web), ('phone', phone), ('street', street), ('city', city)):
        if v: t[k] = str(v)
    add(t)

tiles = {}
for t in trucks:
    t['diet'] = bool(NAME_A.search(t['name'].lower()))
    tiles.setdefault(f'{math.floor(t["lat"])}_{math.floor(t["lon"])}', []).append(t)
print(f'kept {len(trucks)} trucks ({sum(t["diet"] for t in trucks)} with a matching name) in {len(tiles)} squares')

out = tempfile.mkdtemp()
for key, ts in tiles.items():
    with open(os.path.join(out, f'{key}.json'), 'wb') as f:
        f.write(gzip.compress(json.dumps({'trucks': ts}, ensure_ascii=False, separators=(',', ':')).encode(), 9))
with open(os.path.join(out, 'index.json'), 'w') as f:
    json.dump({'built': datetime.date.today().isoformat(), 'overture': OVERTURE, 'foursquare': FSQ_RELEASE,
               'credit': 'Overture Maps Foundation; Foursquare Open Source Places (Apache 2.0)', 'tiles': {k: len(v) for k, v in tiles.items()}}, f)
subprocess.run(['aws', 's3', 'sync', out, f's3://{BUCKET}/trucks/', '--endpoint-url', ENDPOINT, '--delete', '--content-type', 'application/json',
                '--content-encoding', 'gzip', '--exclude', 'index.json', '--only-show-errors'], check=True)
subprocess.run(['aws', 's3', 'cp', os.path.join(out, 'index.json'), f's3://{BUCKET}/trucks/index.json', '--endpoint-url', ENDPOINT,
                '--content-type', 'application/json', '--only-show-errors'], check=True)
print('uploaded', len(tiles), 'squares')
