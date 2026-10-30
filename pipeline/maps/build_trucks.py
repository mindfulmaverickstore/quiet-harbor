#!/usr/bin/env python3
"""Builds the food truck layer: places that are food trucks or street food stalls, one file per 1-degree square like our place
data: trucks/<lat>_<lon>.json (gzip), plus trucks/index.json.

  HF_TOKEN=... MATCH_RULES='{...}' python3 pipeline/maps/build_trucks.py

Sources: Overture Maps, category "food_truck_stand" (CDLA Permissive 2.0), and Foursquare Open Source Places, category "Food Truck"
(Apache 2.0). Closed ones and ones Overture doubts exist (confidence under 0.3) are left out. A truck seen in both is kept once.
Trucks move, so each one carries the day its source last had it ("updated") and the layer says so; "diet" is set only when
the name carries one of our matching words (MATCH_RULES), since neither source flags diets for trucks.
"""
import datetime, gzip, json, math, os, re, subprocess, tempfile, unicodedata, urllib.parse, urllib.request

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
    select id, names.primary, bbox.ymin, bbox.xmin, websites[1], phones[1], addresses[1].freeform, addresses[1].locality, socials
    from read_parquet('s3://overturemaps-us-west-2/release/{OVERTURE}/theme=places/type=place/*', hive_partitioning=1)
    where taxonomy.primary = 'food_truck_stand' and names.primary is not null
      and coalesce(operating_status, 'open') <> 'permanently_closed' and (confidence is null or confidence >= 0.3)""").fetchall()
print('Overture trucks:', len(ov))

db.execute(f"CREATE SECRET hf (TYPE HUGGINGFACE, TOKEN '{os.environ['HF_TOKEN']}')")
fsq = db.execute(f"""
    select fsq_place_id, name, latitude, longitude, website, tel, address, locality, cast(date_refreshed as varchar), instagram, facebook_id
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
                        for k in ('website', 'phone', 'street', 'city', 'instagram', 'facebook'):
                            if t.get(k) and not o.get(k):
                                o[k] = t[k]
                        return
    grid.setdefault((gy, gx), []).append(t)
    trucks.append(t)

for gid, name, lat, lon, web, phone, street, city, socials in ov:
    t = {'id': f'ov/{gid}', 'name': name, 'lat': round(lat, 6), 'lon': round(lon, 6), 'src': 'Overture Maps', 'updated': OV_DAY}
    for k, v in (('website', web), ('phone', phone), ('street', street), ('city', city)):
        if v: t[k] = str(v)
    for u in socials or []:
        low = str(u).lower()
        if 'instagram.com' in low and 'instagram' not in t:
            t['instagram'] = str(u)
        elif ('facebook.com' in low or 'fb.com' in low) and 'facebook' not in t:
            t['facebook'] = str(u)
    add(t)
for fid, name, lat, lon, web, phone, street, city, refreshed, ig, fb in fsq:
    t = {'id': f'fsq/{fid}', 'name': name, 'lat': round(lat, 6), 'lon': round(lon, 6), 'src': 'Foursquare', 'updated': (refreshed or '')[:10]}
    for k, v in (('website', web), ('phone', phone), ('street', street), ('city', city)):
        if v: t[k] = str(v)
    if ig:
        t['instagram'] = f'https://www.instagram.com/{str(ig).strip().lstrip("@")}'
    if fb:
        t['facebook'] = f'https://www.facebook.com/{fb}'
    add(t)

# City of Vancouver Open Data, "Food Vendors": permitted street food spots (Open Government Licence - Vancouver). Fixed spots, so
# they are marked "fixed"; the description is kept, and a description with one of our matching words sets "diet".
try:
    url = 'https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets/food-vendors/records?limit=100'
    for r in json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'map-build/1.0'}), timeout=60)).get('results', []):
        pt = r.get('geo_point_2d')
        if not pt or r.get('status') not in (None, 'open'):
            continue
        desc = (r.get('description') or '').strip()
        t = {'id': f'yvr/{r["key"]}', 'name': (r.get('business_name') or desc or 'Street food').strip(), 'lat': round(pt['lat'], 6), 'lon': round(pt['lon'], 6),
             'src': 'City of Vancouver Open Data', 'updated': datetime.date.today().isoformat(), 'fixed': True}
        if desc:
            t['note'] = desc
        if r.get('location'):
            t['street'] = r['location']
        t['city'] = 'Vancouver'
        add(t)
        if NAME_A.search(desc.lower()):
            t['diet'] = True
    print('Vancouver spots added')
except Exception as e:
    print('Vancouver feed skipped:', e)

# City and County of San Francisco, "Mobile Food Facility Permit" (public domain, ODC-PDDL): trucks and carts with a current
# permit, one record per spot they may use. Fixed spots, so they are marked "fixed"; what they sell is kept as the note.
try:
    url = "https://data.sf.gov/resource/rqzj-sfat.json?$limit=5000&$where=status in('APPROVED','ISSUED')"
    sf = json.load(urllib.request.urlopen(urllib.request.Request(urllib.parse.quote(url, safe=":/?=&$,'()"), headers={'User-Agent': 'map-build/1.0'}), timeout=60))
    today = datetime.date.today().isoformat()
    for r in sf:
        if not r.get('latitude') or not r.get('applicant') or str(r.get('expirationdate', '9999'))[:10] < today:
            continue
        food = (r.get('fooditems') or '').strip()
        t = {'id': f'sf/{r["objectid"]}', 'name': r['applicant'].strip(), 'lat': round(float(r['latitude']), 6), 'lon': round(float(r['longitude']), 6),
             'src': 'City and County of San Francisco', 'updated': today, 'fixed': True, 'city': 'San Francisco'}
        if food:
            t['note'] = food[:140]
        if r.get('address'):
            t['street'] = r['address'].title()
        add(t)
        if NAME_A.search(food.lower()):
            t['diet'] = True
    print('San Francisco spots added')
except Exception as e:
    print('San Francisco feed skipped:', e)

# City of Brussels, "Food trucks / commerces ambulants" (CC0, public domain): the selected food truck and mobile trader spots that
# have a map position (some only list a roaming area, so they are left out). Stalls selling jewellery and the like are skipped.
try:
    url = 'https://opendata.brussels.be/api/explore/v2.1/catalog/datasets/bxl_food_trucks_commerces_ambulants/records?limit=100'
    for r in json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'map-build/1.0'}), timeout=60)).get('results', []):
        pt = r.get('coordonnees_geographiques')
        food = (r.get('produits') or '').strip()
        if not pt or not r.get('nom') or re.search(r'bijou|v[eê]tement|jewel|textile|maroquin|accessoire', food.lower()):
            continue
        t = {'id': f'bxl/{norm(r["nom"])[:24]}{round(pt["lat"] * 1e4)}', 'name': r['nom'].strip().title(), 'lat': round(pt['lat'], 6), 'lon': round(pt['lon'], 6),
             'src': 'City of Brussels Open Data', 'updated': datetime.date.today().isoformat(), 'fixed': True, 'city': 'Brussels'}
        if food:
            t['note'] = food[:140]
        if r.get('localisation'):
            t['street'] = r['localisation'][:120]
        add(t)
        if NAME_A.search(food.lower()):
            t['diet'] = True
    print('Brussels spots added')
except Exception as e:
    print('Brussels feed skipped:', e)

tiles = {}
for t in trucks:
    t['diet'] = t.get('diet') or bool(NAME_A.search(t['name'].lower()))
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
