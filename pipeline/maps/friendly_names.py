#!/usr/bin/env python3
"""Friendly names for download areas: a real place people know, instead of an administrative label.

Official boundary names are often labels no local uses ("Electoral Area A", "Area C (Lois Lake/Saltery Bay)",
"Southcoast-Admreg"). One rule fixes them in every country, with no list of label patterns:

  1. If the official name is itself a place (OpenStreetMap place=* in or around the area), or names one ("Whistler Resort
     Municipality" holds "Whistler"), use that place.
  2. Otherwise use the most prominent place inside the area: an island that covers most of it, else an archipelago, else the
     most populous city, town or village, else the largest hamlet, else the largest island.
  3. Otherwise keep the official name.

An area made of several separate pieces (Electoral Area A: a northern wilderness, the UBC campus, Barnston Island, small islands)
is named after its most populated piece plus " and others": the place with the highest population tag; failing that, the piece
holding the largest smaller area of its own (the campus); failing that, the order above. A piece is land at least GAP away from
the rest; slivers holding nothing are ignored. One cluster of islands is one piece, so it keeps its group name.

Two guards keep names honest: only an area with no smaller areas inside it is renamed after a place within it (a district
holding cities is not "named after" one of them), and a friendly name never repeats the name of another area in the same
list (the "District of North Vancouver" must not become "North Vancouver", which is the city next door).

Areas hold 'kids' (the shapely union of the smaller areas inside them, or None). An area those cover less than half of still counts as
an area with its own places (an electoral area holding only a university campus); places inside the kids belong to the kids.

Pure functions here (tested in pipeline/tests); the osmium step that reads places from a Geofabrik extract is load_places().
"""
import json, os, re, subprocess, unicodedata

import shapely
from shapely.geometry import shape

# suburb and the like are never picked for an area (rule 2) but do count as "the official name is itself a place" (rule 1): Verdun,
# Mermaid Waters and West Lakes are real neighbourhoods, not labels.
RANK = {'city': 4, 'town': 3, 'village': 2, 'hamlet': 1, 'locality': 0, 'suburb': -1, 'neighbourhood': -1, 'quarter': -1, 'borough': -1}
NEAR = 0.03  # degrees, about 3 km: "in or around" the area
WIDE = 0.2   # about 20 km: a place the official name itself mentions may sit this far off (a city centre beside the boundary)
MOST = 0.5   # an island or archipelago "covers most" of an area at this share
GAP = 0.05   # about 5 km: land closer together than this is one piece (a cluster of islands, a town and its fields)
SLIVER = 0.002  # a piece smaller than this share of the area, holding no place or smaller area, is not a real piece


def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    return re.sub(r'\s+', ' ', ''.join(c for c in s if not unicodedata.combining(c)).casefold().replace('-', ' ').replace('’', "'")).strip()


def words(s):
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", norm(s))


def mentions(label, place):
    """True when `label` holds the place's name as whole words ("Bowen Island Municipality" holds "Bowen Island")."""
    w, p = words(label), words(place)
    if not p or len(norm(place)) < 3:
        return False
    return any(w[i:i + len(p)] == p for i in range(len(w) - len(p) + 1))


def parse_pop(v):
    try:
        return int(str(v or '0').replace(',', '').replace(' ', '').split('.')[0] or 0)
    except ValueError:
        return 0


def name_areas(areas, places, taken_names=()):
    """areas: [{'id','name','name_en','geom','kids','kid_list'}]; places: [{'kind','name','pop','geom'}] (geom: shapely point or polygon).
    Returns {area id: friendly name} for the areas whose name changes. taken_names: names already used by other areas."""
    pts = [(p, p['geom'] if p['geom'].geom_type == 'Point' else p['geom'].representative_point()) for p in places]
    tree = shapely.STRtree([pt for _, pt in pts])
    own = {}
    for a in areas:
        own.setdefault(norm(a['name_en'] or a['name']), set()).add(a['id'])
    out = {}
    for a in areas:
        shown = a['name_en'] or a['name']
        g = a['geom']
        kids = a.get('kids')
        free = kids is None or kids.area / (g.area or 1e-12) < MOST
        near = [pts[i] for i in tree.query(g.buffer(NEAR), predicate='intersects')]
        labels = [a['name'], a['name_en']] if a['name_en'] else [a['name']]
        # The part before a bracket ("Area C (Lois Lake/Saltery Bay)" is "Area C" in brackets), and each bracket part.
        labels += [x for lab in list(labels) for x in re.split(r'[()/,]', lab) if x.strip()]
        from_rule2 = False
        pick = next((p for p, _ in near if any(norm(lab) == norm(p['name']) for lab in labels)), None)
        if not pick and free:
            wide = [pts[i] for i in tree.query(g.buffer(WIDE), predicate='intersects')]
            named = [(p, pt) for p, pt in wide if any(mentions(lab, p['name']) for lab in labels)]
            # (An archipelago's point is often in the sea between its islands, so it only has to be near.)
            held = [p for p, pt in named if (g.contains(pt) and not (kids is not None and kids.contains(pt))) or p['kind'] in ('archipelago', 'island', 'island_node')]
            pick = max(held, key=lambda p: (len(words(p['name'])), RANK.get(p['kind'], 0), p['pop']), default=None)
            # A name that mentions a place outside the area ("Fort Langley" mentions Langley, "District of North Vancouver"
            # mentions North Vancouver) already is a place-style name: it is not an administrative label, so keep it.
            if not pick and not named:
                from_rule2 = True
                pick = best_inside(a, [p for p, pt in near if g.contains(pt) and not (kids is not None and kids.contains(pt))])
        if not pick:
            continue
        name = pick['name']
        if from_rule2 and pick['kind'] != 'archipelago':
            name = several_pieces(a, [p for p, pt in near if g.contains(pt)], pick)
        taken = own.get(norm(name), set()) - {a['id']}
        if norm(name) == norm(shown) or taken or norm(name) in taken_names:
            continue
        out[a['id']] = name
    # Two areas that would share a friendly name (the quarters "Matsqui 2", "Matsqui 3") keep their official names.
    count = {}
    for v in out.values():
        count[norm(v)] = count.get(norm(v), 0) + 1
    return {k: v for k, v in out.items() if count[norm(v)] == 1}


def pieces_of(area, inside):
    """The separate pieces of an area's land: [(geometry, places inside, smaller areas inside)], slivers holding nothing left out."""
    g = area['geom']
    polys = list(g.geoms) if g.geom_type in ('MultiPolygon', 'GeometryCollection') else [g]
    polys = [q for q in polys if q.geom_type == 'Polygon' and not q.is_empty]
    if len(polys) < 2:
        return []
    merged = shapely.union_all([q.buffer(GAP / 2) for q in polys])
    comps = list(merged.geoms) if merged.geom_type == 'MultiPolygon' else [merged]
    out = []
    for c in comps:
        mine = [q for q in polys if c.contains(q.representative_point())]
        land = sum(q.area for q in mine)
        here = [p for p in inside if c.contains(p['geom'] if p['geom'].geom_type == 'Point' else p['geom'].representative_point())]
        kids = [k for k in area.get('kid_list') or [] if c.contains(k['geom'].representative_point())]
        if land / (g.area or 1e-12) >= SLIVER or here or kids:
            out.append((c, here, kids))
    return out if len(out) >= 2 else []


def several_pieces(area, inside, pick):
    """The name for an area of several pieces: its most populated piece plus " and others" (see the top of this file);
    the plain pick's name when the area is one piece."""
    pieces = pieces_of(area, inside)
    if not pieces:
        return pick['name']
    known = [p for _, here, _ in pieces for p in here if p['pop'] > 0 and p['kind'] != 'locality']
    if known:
        return max(known, key=lambda p: p['pop'])['name'] + ' and others'
    kids = [k for _, _, ks in pieces for k in ks]
    if kids:
        return max(kids, key=lambda k: k['b'])['name'] + ' and others'
    return pick['name'] + ' and others'


def best_inside(area, inside):
    g = area['geom']
    area_size = g.area or 1e-12
    # An island far bigger than the area is not what the area is named for (a district of Iceland is not "Iceland").
    islands = [p for p in inside if p['kind'] == 'island' and p['geom'].geom_type != 'Point' and p['geom'].area <= 3 * area_size]
    covered = sum(p['geom'].intersection(g).area for p in islands) if islands else 0
    # One island covering most of the area.
    for p in sorted(islands, key=lambda p: -p['geom'].intersection(g).area):
        if p['geom'].intersection(g).area / area_size >= MOST:
            return p
    # Islands together covering most of it: the archipelago they belong to.
    count = sum(1 for p in inside if p['kind'] in ('island', 'island_node'))
    if covered / area_size >= MOST or count >= 3:
        arch = [p for p in inside if p['kind'] == 'archipelago']
        if arch:
            return max(arch, key=lambda p: (p['geom'].geom_type != 'Point', p['pop']))
    towns = [p for p in inside if p['kind'] in ('city', 'town', 'village')]
    if towns:
        return max(towns, key=lambda p: (p['pop'], RANK[p['kind']]))
    # Hamlets before a bare island: people say where they live ("Oyster River"), not the little island off the coast
    # ("Treasure Island" is one private islet). An island is named only when the area has no village or hamlet at all
    # (Electoral Area A: Barnston Island and the water around it). Among hamlets with no population, the one nearest the
    # middle of the area stands for it. (A locality is a spot on the map, often a gully or a point: an official label can name
    # one, but it is never picked by itself.)
    hamlets = [p for p in inside if p['kind'] == 'hamlet']
    if hamlets:
        mid = g.representative_point()
        return max(hamlets, key=lambda p: (p['pop'], -mid.distance(p['geom'] if p['geom'].geom_type == 'Point' else p['geom'].representative_point())))
    # A bare island is only a fair name for an area that is not made of smaller areas (kids cover under a tenth of it).
    kids = area.get('kids')
    if islands and (kids is None or kids.area / area_size < 0.1):
        return max(islands, key=lambda p: p['geom'].intersection(g).area)
    return None


def region_title(official, places, admin_names, region_geom=None):
    """The region's name for people. Kept when it is a real place or administrative area ("Berlin", "Portugal"); otherwise
    "<its most populous place> area" ("Southcoast-Admreg" becomes "Vancouver area"). None when nothing is known."""
    n = norm(official)
    if any(norm(p['name']) == n for p in places) or n in admin_names:
        return official
    # A region that is one island ("Island-Admreg" is Vancouver Island) takes the island's name.
    if region_geom is not None and region_geom.area:
        isl = [(p, p['geom'].intersection(region_geom).area / region_geom.area) for p in places if p['kind'] == 'island' and p['geom'].geom_type != 'Point']
        best = max(isl, key=lambda x: x[1], default=None)
        if best and best[1] >= MOST:
            return best[0]['name']
    towns = [p for p in places if p['kind'] in ('city', 'town', 'village')]
    if not towns:
        return None
    return max(towns, key=lambda p: (p['pop'], RANK[p['kind']]))['name'] + ' area'


def wikidata_archipelagos(boxes):
    """Archipelagos Wikidata knows inside the boxes (west, south, east, north), as places: OpenStreetMap often has the islands
    but not the group they are called ("Southern Gulf Islands"). Returns [] when Wikidata does not answer."""
    found = []
    for w, s, e, n in boxes:
        q = ('SELECT ?l ?c WHERE { SERVICE wikibase:box { ?i wdt:P625 ?c . bd:serviceParam wikibase:cornerSouthWest "Point(%s %s)"^^geo:wktLiteral . '
             'bd:serviceParam wikibase:cornerNorthEast "Point(%s %s)"^^geo:wktLiteral } ?i wdt:P31 wd:Q33837 . ?i rdfs:label ?l FILTER(LANG(?l)="en") }') % (w, s, e, n)
        try:
            raw = subprocess.run(['curl', '-s', '--max-time', '90', '-A', 'quiet-harbor-map-build/1.0 (https://github.com/mindfulmaverickstore/quiet-harbor)', '-H', 'Accept: application/sparql-results+json',
                                  '--data-urlencode', 'query=' + q, 'https://query.wikidata.org/sparql'], capture_output=True, check=True).stdout
            for b in json.loads(raw)['results']['bindings']:
                x, y = b['c']['value'][6:-1].split()
                found.append({'kind': 'archipelago', 'name': b['l']['value'], 'pop': 0, 'geom': shapely.Point(float(x), float(y))})
        except Exception as ex:
            print(f'  Wikidata archipelagos: no answer ({ex!r})', flush=True)
    return found


def unescape(s):
    return re.sub(r'%([0-9a-fA-F]+)%', lambda m: chr(int(m.group(1), 16)), s)


def load_places(pbf, work):
    """Places and administrative-area names from a Geofabrik extract (osmium-tool). Returns (places, admin_names)."""
    pl, out = os.path.join(work, 'fn-places.pbf'), os.path.join(work, 'fn-places.geojsonseq')
    subprocess.run(['osmium', 'tags-filter', pbf, 'nwr/place=island,archipelago,city,town,village,hamlet,locality,suburb,neighbourhood,quarter,borough', '-o', pl, '--overwrite'], check=True)
    subprocess.run(['osmium', 'export', pl, '-f', 'geojsonseq', '--geometry-types=point,polygon', '-o', out, '--overwrite'], check=True)
    places = []
    for line in open(out):
        f = json.loads(line.lstrip('\x1e'))
        p = f['properties']
        name = p.get('name:en') or p.get('name')
        kind = p.get('place')
        if not name or kind not in (*RANK, 'island', 'archipelago'):
            continue
        try:
            geom = shape(f['geometry'])
        except Exception:
            continue
        if geom.geom_type == 'MultiPolygon' and kind not in ('island', 'archipelago'):
            geom = geom.representative_point()
        if kind == 'island' and geom.geom_type == 'Point':
            kind = 'island_node'
        places.append({'kind': kind, 'name': name, 'pop': parse_pop(p.get('population')), 'geom': geom})
    adm = os.path.join(work, 'fn-admin.opl')
    subprocess.run(['osmium', 'tags-filter', pbf, 'r/boundary=administrative', '-f', 'opl', '-o', adm, '--overwrite'], check=True)
    admin_names = set()
    for line in open(adm):
        if not line.startswith('r'):
            continue
        field = next((x for x in line.rstrip('\n').split(' ') if x.startswith('T')), 'T')[1:]
        tags = dict(unescape(t).split('=', 1) for t in field.split(',') if '=' in unescape(t))
        if tags.get('admin_level') in ('2', '3', '4', '5'):
            for k in ('name:en', 'name'):
                if tags.get(k):
                    admin_names.add(norm(tags[k]))
    return places, admin_names
