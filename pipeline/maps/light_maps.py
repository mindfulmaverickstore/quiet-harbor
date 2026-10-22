#!/usr/bin/env python3
"""Runs make_light.py on published maps and uploads the results next to them. The original city.pmtiles is never touched.

  python3 pipeline/maps/light_maps.py --ids lisbon porto      # writes maps/<id>/light.pmtiles and maps/<id>/density.json
  python3 pipeline/maps/light_maps.py --summary               # writes maps/light.json: {"<id>": {"mb": .., "full_mb": ..}}
  python3 pipeline/maps/light_maps.py --full --ids lisbon      # only writes maps/<id>/density-full.json (the full-detail map's density)

Needs the aws program and S3_ENDPOINT, S3_BUCKET and AWS_* variables (GitHub Actions sets them). `pmtiles` on the PATH,
when present, checks each new file.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_light  # noqa: E402

ENDPOINT, BUCKET = os.environ.get('S3_ENDPOINT', ''), os.environ.get('S3_BUCKET', '')


def aws(*args, check=True):
    return subprocess.run(['aws', 's3', *args, '--endpoint-url', ENDPOINT], check=check, capture_output=True, text=True)


def full_density(pid, work):
    src, dens = os.path.join(work, 'city.pmtiles'), os.path.join(work, 'density-full.json')
    aws('cp', f's3://{BUCKET}/maps/{pid}/city.pmtiles', src, '--only-show-errors')
    make_light.density_only(src, dens)
    subprocess.run(['gzip', '-9', '-f', dens], check=True)
    aws('cp', dens + '.gz', f's3://{BUCKET}/maps/{pid}/density-full.json', '--content-type', 'application/json',
        '--content-encoding', 'gzip', '--only-show-errors')
    print(f'{pid}: full-detail density written', flush=True)


def one(pid, work):
    src, dst, dens = (os.path.join(work, n) for n in ('city.pmtiles', 'light.pmtiles', 'density.json'))
    aws('cp', f's3://{BUCKET}/maps/{pid}/city.pmtiles', src, '--only-show-errors')
    make_light.main(src, dst, dens)
    if shutil.which('pmtiles'):
        subprocess.run(['pmtiles', 'verify', dst], check=True, capture_output=True)
    aws('cp', dst, f's3://{BUCKET}/maps/{pid}/light.pmtiles', '--content-type', 'application/vnd.pmtiles', '--only-show-errors')
    subprocess.run(['gzip', '-9', '-f', dens], check=True)
    aws('cp', dens + '.gz', f's3://{BUCKET}/maps/{pid}/density.json', '--content-type', 'application/json',
        '--content-encoding', 'gzip', '--only-show-errors')
    full, light = os.path.getsize(src), os.path.getsize(dst)
    print(f'{pid}: {full / 1e6:.0f} MB -> {light / 1e6:.0f} MB ({100 * light / full:.0f}%)', flush=True)


def summary():
    out = {}
    for line in aws('ls', f's3://{BUCKET}/maps/', '--recursive').stdout.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[3].endswith(('/light.pmtiles', '/city.pmtiles')):
            pid, name = parts[3].split('/')[1], parts[3].split('/')[2]
            out.setdefault(pid, {})['mb' if name == 'light.pmtiles' else 'full_mb'] = round(int(parts[2]) / 1e6)
    out = {k: v for k, v in sorted(out.items()) if 'mb' in v}
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump(out, f, separators=(',', ':'))
    aws('cp', f.name, f's3://{BUCKET}/maps/light.json', '--content-type', 'application/json')
    full = sum(v.get('full_mb', 0) for v in out.values())
    light = sum(v['mb'] for v in out.values())
    print(f'{len(out)} light maps; {full / 1000:.0f} GB -> {light / 1000:.0f} GB')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--ids', nargs='*')
    ap.add_argument('--summary', action='store_true')
    ap.add_argument('--full', action='store_true', help='only the full-detail density list')
    a = ap.parse_args()
    if a.summary and not a.full:
        summary()
    failed = []
    for pid in a.ids or []:
        with tempfile.TemporaryDirectory() as work:
            try:
                (full_density if a.full else one)(pid, work)
            except Exception as e:  # one bad map shouldn't stop the others in its group
                print(f'{pid}: FAILED {e}', flush=True)
                failed.append(pid)
    if failed:
        sys.exit('failed: ' + ' '.join(failed))
