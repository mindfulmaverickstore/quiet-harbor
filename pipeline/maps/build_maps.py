#!/usr/bin/env python3
"""Builds our offline/online map packs from the Protomaps planet build and publishes them.

  python3 pipeline/maps/build_maps.py --pmtiles /path/to/pmtiles --only lisbon porto   # build and upload these
  python3 pipeline/maps/build_maps.py --pmtiles /path/to/pmtiles --sizes               # report sizes (no download)
  python3 pipeline/maps/build_maps.py --status                                         # what is published, what is missing
  python3 pipeline/maps/build_maps.py --missing                                        # ids not published yet (one line)
  python3 pipeline/maps/build_maps.py --catalog-only                                   # rebuild and upload catalog.json

For each place in places.json it cuts the area out of the planet build (zoom 0 up to the place's maxzoom, default 15),
uploads it as maps/<id>/city.pmtiles, then DELETES the local copy. The catalogue (catalog.json, which the app reads) is
always rebuilt from places.json plus the sizes of what is really published, so it can never disagree with the storage
and several machines can build at the same time.

Uploads use `aws s3` (no size limit) when S3_ENDPOINT (or R2_ENDPOINT), S3_BUCKET and AWS_* variables are set (GitHub Actions does this),
otherwise `npx wrangler` (max 300 MB per file, so bigger places must be split in places.json).
"""
import argparse, email.utils, functools, json, os, re, subprocess, sys, tempfile, time, urllib.request

print = functools.partial(print, flush=True)  # show progress straight away, even when output goes to a file
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
# Where to upload (any S3-compatible storage: Cloudflare R2, Backblaze B2) and where the same files can be read publicly.
BUCKET = os.environ.get('S3_BUCKET') or os.environ.get('R2_BUCKET') or ''
ENDPOINT = os.environ.get('S3_ENDPOINT') or os.environ.get('R2_ENDPOINT') or ''
if ENDPOINT and not ENDPOINT.startswith('http'):
    ENDPOINT = 'https://' + ENDPOINT
PUBLIC = os.environ.get('MAPS_PUBLIC_URL') or 'https://pub-e5b664167e394466a0e48820dffb17df.r2.dev'
if not PUBLIC.startswith('http'):
    PUBLIC = 'https://' + PUBLIC
# "latest" means the newest planet build Protomaps lists: they delete old builds, so a fixed address stops working.
DEFAULT_SOURCE = 'latest'
BUILDS_LIST = 'https://build-metadata.protomaps.dev/builds.json'


def resolve_source(source):
    if source != 'latest':
        return source
    req = urllib.request.Request(BUILDS_LIST, headers=UA)
    key = json.load(urllib.request.urlopen(req, timeout=60))[-1]['key']
    return f'https://build.protomaps.com/{key}'
MAX_WRANGLER_MB = 300
UA = {'User-Agent': 'map-build/1.0'}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def cut_area(place):
    """What to cut out: the place's exact outline when it has one (a file in regions/), else its box."""
    if place.get('region'):
        return f'--region={os.path.join(HERE, place["region"])}'
    return f'--bbox={",".join(map(str, place["bbox"]))}'


def dry_size_mb(pm, source, place, maxzoom=15):
    out = run([pm, 'extract', source, os.devnull, cut_area(place), f'--maxzoom={maxzoom}', '--dry-run'])
    m = re.search(r'archive size of ([\d.]+) (\w+)', out.stdout + out.stderr)
    if not m:
        return None
    v, u = float(m.group(1)), m.group(2)
    return v * {'B': 1e-6, 'kB': 0.001, 'MB': 1, 'GB': 1000}.get(u, 1)


def use_aws():
    return bool(ENDPOINT and os.environ.get('AWS_ACCESS_KEY_ID'))


def upload(local, remote, ctype):
    if use_aws():
        r = run(['aws', 's3', 'cp', local, f's3://{BUCKET}/{remote}', '--endpoint-url', ENDPOINT, '--content-type', ctype])
    else:
        r = run(['npx', 'wrangler', 'r2', 'object', 'put', f'{BUCKET}/{remote}', '--file', local, '--content-type', ctype, '--remote'], cwd=REPO)
    return r.returncode == 0


def upload_encoded(local, remote):
    """Uploads a small JSON file that is already gzip-compressed, marked so the phone unpacks it by itself."""
    ctype = 'application/json'
    if local.endswith('food.json'):  # the manifest is plain text
        return upload(local, remote, ctype)
    if use_aws():
        r = run(['aws', 's3', 'cp', local, f's3://{BUCKET}/{remote}', '--endpoint-url', ENDPOINT, '--content-type', ctype, '--content-encoding', 'gzip'])
    else:
        r = run(['npx', 'wrangler', 'r2', 'object', 'put', f'{BUCKET}/{remote}', '--file', local, '--content-type', ctype, '--content-encoding', 'gzip', '--remote'], cwd=REPO)
    return r.returncode == 0


def published_info(pid):
    """(size in bytes, version) of the published map for this id, or None if it isn't there.
    The version is the day it was uploaded (YYYYMMDD). The app puts it on the map's web address, so a rebuilt map is a
    new address and a phone never mixes pieces of the old file with the new one."""
    try:
        req = urllib.request.Request(f'{PUBLIC}/maps/{pid}/city.pmtiles', method='HEAD', headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            modified = email.utils.parsedate_to_datetime(r.headers['Last-Modified']) if r.headers.get('Last-Modified') else None
            return int(r.headers.get('Content-Length') or 0), modified.strftime('%Y%m%d') if modified else '0'
    except Exception:
        return None


def published_bytes(pid):
    """Size in bytes of the published map for this id, or None if it isn't there."""
    info = published_info(pid)
    return info[0] if info else None


def build_catalog(places):
    maps = []
    for p in places:
        info = published_info(p['id'])
        if info is None:
            continue
        size, version = info
        maps.append({
            'id': p['id'], 'name': p['name'], 'kind': p['kind'], 'bbox': p['bbox'], 'mb': max(1, round(size / 1e6)),
            'packs': p.get('packs', []), 'parent': p.get('parent'), 'maxzoom': p.get('maxzoom', 15), 'v': version, 'country': p.get('country'), 'names': p.get('names'),
        })
    return {'maps': maps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pmtiles')
    ap.add_argument('--source', default=DEFAULT_SOURCE)
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--places', default='places.json', help='which list of places to use (a file next to this script)')
    ap.add_argument('--sizes-out', help='with --sizes: write the measured sizes to this file as JSON')
    ap.add_argument('--sizes', action='store_true')
    ap.add_argument('--catalog-only', action='store_true')
    ap.add_argument('--no-catalog', action='store_true', help='do not rebuild the catalogue afterwards (a later job does it)')
    ap.add_argument('--no-upload', action='store_true')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--missing', action='store_true')
    ap.add_argument('--all-ids', action='store_true', help='print every id in places.json (one line)')
    a = ap.parse_args()
    a.source = resolve_source(a.source)

    places = json.load(open(os.path.join(HERE, a.places)))['places']
    if a.all_ids:
        print(' '.join(p['id'] for p in places))
        return
    done = {p['id'] for p in places if published_bytes(p['id']) is not None} if (a.status or a.missing) else set()

    if a.status:
        print(f'{len(done)} of {len(places)} places published')
        print('missing:', ', '.join(p['id'] for p in places if p['id'] not in done) or 'none')
        return
    if a.missing:
        print(' '.join(p['id'] for p in places if p['id'] not in done))
        return

    if not a.catalog_only:
        if not a.pmtiles:
            sys.exit('--pmtiles /path/to/pmtiles is required')
        failed = []
        sizes = []
        for p in places:
            if a.only and p['id'] not in a.only:
                continue
            mz = p.get('maxzoom', 15)
            mb = dry_size_mb(a.pmtiles, a.source, p, mz)
            print(f"{p['id']}: about {mb:.0f} MB" if mb else f"{p['id']}: size unknown")
            if a.sizes:
                sizes.append({'id': p['id'], 'name': p['name'], 'mb': round(mb) if mb else None})
                continue
            if mb and mb > MAX_WRANGLER_MB and not use_aws():
                print(f'  skipped: over {MAX_WRANGLER_MB} MB without the aws uploader; split it in places.json')
                continue
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, 'city.pmtiles')
                # The Protomaps server now and then answers with a temporary error (502): try again a few times.
                for attempt, wait in enumerate((0, 20, 60, 180)):
                    time.sleep(wait)
                    r = run([a.pmtiles, 'extract', a.source, out, cut_area(p), f'--maxzoom={mz}'])
                    if r.returncode == 0 and os.path.exists(out):
                        break
                    print(f'  extract attempt {attempt + 1} failed:', (r.stderr or r.stdout)[-120:].strip())
                if r.returncode != 0 or not os.path.exists(out):
                    print('  extract failed:', (r.stderr or r.stdout)[-200:])
                    failed.append(p['id'])
                    continue
                size_mb = os.path.getsize(out) / 1e6
                if not a.no_upload and not upload(out, f"maps/{p['id']}/city.pmtiles", 'application/octet-stream'):
                    print('  upload failed')
                    failed.append(p['id'])
                    continue
            print(f'  done: {size_mb:.0f} MB')
        if a.sizes and a.sizes_out:
            json.dump(sizes, open(a.sizes_out, 'w'))
        # A job must not report success when a map did not get built.
        if failed:
            sys.exit(f"failed: {' '.join(failed)}")

    if not a.sizes and not a.no_catalog:
        cat = build_catalog(places)
        path = os.path.join(HERE, 'catalog.json')
        json.dump(cat, open(path, 'w'), indent=1)
        print(f"catalogue: {len(cat['maps'])} maps")
        if not a.no_upload:
            print('catalog.json uploaded' if upload(path, 'maps/catalog.json', 'application/json') else 'catalog upload failed')


if __name__ == '__main__':
    main()
