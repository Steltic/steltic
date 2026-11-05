"""Engine-independent physical-data contract v1, shared by HR, CFS and openBIM.

Validation is structural data checking, not engineering certification.
Keep this file and its adjacent JSON schema byte-identical in all three repos.
"""
import json
import math
import re
from pathlib import Path
try:
    from . import bim_assemblies,bim_bonds,member_slots
except ImportError:
    import bim_assemblies,bim_bonds,member_slots

VERSION = "1.0"
COLLECTIONS = ("members", "panels", "openings", "profiles", "materials", "joints", "connections", "interfaces")
COVERAGE = ("primary_members", "secondary_members", "openings", "connections")
DRAWING_TABLES = {"members","connections","anchorage","holddowns","collectors","diaphragms","schedules","bases","chords","frames","framing","gravity_framing","headers","joists","wall_lines","studs","materials","segment_framing","braced_wall_layout","braced_bay_layout"}


def issue(code, message, ids=(), severity="needs_input"):
    return {"code": code, "message": message, "entity_ids": sorted(set(map(str, ids))), "severity": severity}


def number(value, positive=False):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and (not positive or value > 0)


def vector(value):
    return isinstance(value, list) and len(value) == 3 and all(number(v) for v in value)


def norm(value):
    return math.sqrt(sum(v*v for v in value))


def unit(value):
    return vector(value) and abs(norm(value)-1) < 1e-6


def perpendicular(a, b):
    return unit(a) and unit(b) and abs(sum(x*y for x,y in zip(a,b))) < 1e-6


def geometry_errors(g, profiles):
    if not isinstance(g, dict):
        return ["geometry must be an object"]
    kind = g.get("type")
    if kind == 'mesh':
        faces=g.get('faces',[])
        if not isinstance(faces,list) or not faces or any(not isinstance(f,list) or len(f)<3 or not all(vector(p) for p in f) for f in faces):return ['Mesh needs finite polygon faces']
        edges={}
        for face in faces:
            for a,b in zip(face,face[1:]+face[:1]):
                a=tuple(round(v,8) for v in a);b=tuple(round(v,8) for v in b)
                if a==b:return ['Mesh contains a zero-length edge']
                key=tuple(sorted((a,b)));edges[key]=edges.get(key,0)+1
        return [] if all(n==2 for n in edges.values()) else ['Solid mesh must have exactly two incident faces at each edge']
    if kind == "polyline":
        points = g.get("points", [])
        return [] if len(points) >= 2 and all(vector(p) for p in points) and any(p != points[0] for p in points) else ["invalid polyline"]
    errors = [] if vector(g.get("origin")) else ["origin must have three finite coordinates"]
    if kind == "box":
        if not vector(g.get("size")) or not all(x > 0 for x in g["size"]): errors.append("box size must be positive")
        if not perpendicular(g.get("x_axis"), g.get("y_axis")): errors.append("box axes must be orthonormal")
    elif kind in ("cylinder", "extruded_profile"):
        if not number(g.get("length"), True) or not unit(g.get("axis")): errors.append("invalid extrusion axis/length")
        if kind == "cylinder" and not number(g.get("radius"), True): errors.append("radius must be positive")
        if kind == "extruded_profile":
            if profiles.get(g.get("profile_id"),{}).get("status") != "declared": errors.append("unknown or undeclared profile")
            if not perpendicular(g.get("axis"), g.get("local_y")): errors.append("extrusion axes must be orthonormal")
    else:
        errors.append("unknown geometry type")
    return errors


def _simple_boundary(points, normal):
    """Test a planar ring without repairing or reordering its vertices."""
    if points[-1] == points[0]: points = points[:-1]
    drop = max(range(3), key=lambda k: abs(normal[k]))
    ring = [[v[k] for k in range(3) if k != drop] for v in points]
    if len(ring) < 3 or _polygon_area(ring) <= 1e-10: return False
    def cross(a, b, c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def on(a, b, p):
        return abs(cross(a,b,p)) < 1e-8 and all(min(a[k],b[k])-1e-8 <= p[k] <= max(a[k],b[k])+1e-8 for k in (0,1))
    edges = list(zip(ring, ring[1:]+ring[:1]))
    for i, (a,b) in enumerate(edges):
        if norm([x-y for x,y in zip(a,b)]) < 1e-8: return False
        # Adjacent collinear edges may continue straight, but cannot backtrack.
        c = edges[(i+1) % len(edges)][1]
        if abs(cross(a,b,c)) < 1e-8 and sum((x-y)*(z-y) for x,y,z in zip(a,b,c)) > 1e-8: return False
        for j in range(i+1,len(edges)):
            if j == i+1 or (i == 0 and j == len(edges)-1): continue
            c,d = edges[j]
            if (cross(a,b,c)*cross(a,b,d) < 0 and cross(c,d,a)*cross(c,d,b) < 0) or any((on(a,b,c),on(a,b,d),on(c,d,a),on(c,d,b))): return False
    return True


def _profile_bound_radius(profile_id, profiles):
    """Conservative section envelope for absence checks, not contact/fit proof."""
    profile = profiles.get(profile_id,{})
    if profile.get('family') == 'built_up':
        return max((norm(c['offset']) + _profile_bound_radius(c['profile_id'],profiles)
                    for c in profile.get('components',[]) if profiles.get(c['profile_id'],{}).get('family') != 'built_up'), default=0.)
    return sum(abs(v) for v in profile.get('dimensions',{}).values() if number(v))


def _part_bounds(geometry, profiles):
    """Conservative world bounds for supported solid and reference parts."""
    if geometry_errors(geometry,profiles): return None
    kind = geometry['type']
    if kind == 'polyline': points = geometry['points']; radius = 0.
    elif kind == 'mesh': points=[p for face in geometry['faces'] for p in face];radius=0.
    elif kind == 'box':
        x,y = geometry['x_axis'],geometry['y_axis']
        z = [x[1]*y[2]-x[2]*y[1],x[2]*y[0]-x[0]*y[2],x[0]*y[1]-x[1]*y[0]]
        points = [[geometry['origin'][k]+sum(t*s*v[k] for t,s,v in zip((a,b,c),geometry['size'],(x,y,z))) for k in range(3)] for a in (0,1) for b in (0,1) for c in (0,1)]
        radius = 0.
    else:
        points = [geometry['origin'],[p+geometry['length']*d for p,d in zip(geometry['origin'],geometry['axis'])]]
        radius = geometry['radius'] if kind == 'cylinder' else _profile_bound_radius(geometry['profile_id'],profiles)
    return [(min(p[k] for p in points)-radius,max(p[k] for p in points)+radius) for k in range(3)]


def joint_disposition_findings(package,*,verified_bonds=None):
    """Require explicit, traceable exceptions for jobs requesting located parts.

    Legacy reason-only declarations remain readable. These checks establish
    topology and references, not bearing capacity or connection fit/approval.
    """
    if not package.get('require_connection_parts'): return []
    physical = package['physical']
    if verified_bonds is None:_,verified_bonds=bim_bonds.evaluate(physical)
    templates=bim_assemblies.index(physical)
    members = {m['id']:m for m in physical.get('members',[])}
    panels = {p['id']:p for p in physical.get('panels',[])}
    profiles = {p['id']:p for p in physical.get('profiles',[])}
    joints = {j['id']:j for j in physical.get('joints',[])}
    connections = {c['id']:c for c in physical.get('connections',[])}
    joined = {c.get('joint_id') for c in connections.values()}
    records = {r['id']:r for r in package.get('design_records',[])}
    bounds = {}; findings = []
    def bad(code, message, jid): findings.append(issue(code,message,[jid]))
    def distance(point, box): return norm([max(lo-p,p-hi,0.) for p,(lo,hi) in zip(point,box)])
    for declaration in physical.get('joint_dispositions',[]):
        jid = declaration.get('joint_id'); joint = joints.get(jid)
        if not joint or jid in joined: continue
        kind = declaration.get('kind'); point = joint.get('point')
        if not vector(point): continue
        ends = joint.get('member_ends',[]); attachments = joint.get('attachments',[])
        if kind not in ('free_end','continuous_member','bearing_interface','shared_connection'):
            bad('joint_disposition_kind','Classify this unconnected joint as free_end, continuous_member, bearing_interface or shared_connection; a typical-detail note is not a located occurrence',jid)
        elif kind == 'free_end':
            if len(ends) != 1 or attachments:
                bad('joint_free_end','A free-end exception needs exactly one physical member end and no support attachments',jid)
        elif kind == 'continuous_member':
            directions = []; invalid = False
            for end in ends:
                member = members.get(end.get('member_id'),{}); axis = member.get('axis',[])
                if len(axis) != 2 or not all(vector(p) for p in axis) or end.get('end') not in ('start','end'):
                    invalid = True; continue
                i = 0 if end['end'] == 'start' else 1
                offsets = member.get('end_offsets',[[0,0,0],[0,0,0]])
                actual = [p+o for p,o in zip(axis[i],offsets[i])]
                delta = [p+o-q for p,o,q in zip(axis[1-i],offsets[1-i],point)]
                length = norm(delta)
                if distance(point,[(v,v) for v in actual]) > 1e-5 or length < 1e-8: invalid = True
                elif length: directions.append([v/length for v in delta])
            # A lone interior station on an uninterrupted member is also valid.
            interior = not ends and len(attachments) == 1 and 0 < attachments[0].get('station',0) < 1
            dots = [sum(a*b for a,b in zip(directions[0],v)) for v in directions[1:]] if directions else []
            through = len(directions) == 2 and not attachments and all(abs(d) > .9999 for d in dots) and any(d < -.9999 for d in dots)
            if invalid or not (interior or through):
                bad('joint_continuity','A continuous-member exception needs two opposing collinear physical ends at the joint, or one interior attachment with no other member ends; connected branches and cutback gaps need their actual connection',jid)
        elif kind == 'bearing_interface':
            refs = declaration.get('design_record_ids',[])
            supports = declaration.get('supporting_entity_ids',[])
            if not refs or any(r not in records or records[r].get('waived') for r in refs):
                bad('joint_bearing_design','A hardware-free bearing interface needs its actual completed, non-waived design record references',jid)
            valid_support = False
            lone_end = ends[0].get('member_id') if len(ends) == 1 else None
            for sid in supports:
                support = members.get(sid) or panels.get(sid)
                if not support or sid == lone_end: continue
                if sid in members:
                    pts = [[v+o for v,o in zip(p,off)] for p,off in zip(support['axis'],support.get('end_offsets',[[0,0,0],[0,0,0]]))]
                    radius = _profile_bound_radius(support.get('profile_id'),profiles)
                else: pts = support.get('vertices',[]); radius = support.get('thickness',0)
                if not pts: continue
                box = [(min(p[k] for p in pts)-radius,max(p[k] for p in pts)+radius) for k in range(3)]
                if distance(point,box) <= .25: valid_support = True
            if not supports or any(s not in members and s not in panels for s in supports) or not valid_support:
                bad('joint_bearing_support','Identify the actual supporting member/panel at a bearing interface; the loaded member cannot support its own lone end',jid)
        elif kind == 'shared_connection':
            refs = declaration.get('connection_ids',[])
            if not refs or any(r not in connections for r in refs):
                bad('joint_shared_connection','Reference the existing located connection IDs that actually serve this joint; do not duplicate hardware or cite a representative detail',jid)
                continue
            bond_ends=set()
            for cid in refs:
                if cid in verified_bonds:
                    connection=connections[cid]
                    template=templates[connection['assembly_instance']['template_id']]
                    covered=bim_bonds.joint_end_coverage(connection,template,joint,members)
                    if not covered:bad('joint_shared_connection','A whole-bar bond can serve only its actual bound physical bar ends; a lap or unrelated nearby bar cannot dispose of this joint',jid)
                    bond_ends.update(covered)
                    continue
                if cid not in bounds:
                    bounds[cid] = [box for p in bim_assemblies.parts(connections[cid],templates,world=True) if (box := _part_bounds(p.get('geometry',{}),profiles)) is not None]
                if not bounds[cid] or min(distance(point,box) for box in bounds[cid]) > 72:
                    bad('joint_shared_connection','The referenced assembly has no connection-part bounds within six feet of this joint; reference the actual local assembly (nearness alone does not prove fit)',jid)
            if all(cid in verified_bonds for cid in refs) and (attachments or not ends or bond_ends!=set(range(len(ends)))):
                bad('joint_shared_connection','Every end at a bond-only joint needs its own verified whole-bar bond; branch attachments need their actual connection',jid)
    return findings


def connection_component_findings(package):
    """Detect absent component categories explicitly selected by this design.

    This is a bounded data contradiction check, not a parts catalogue, NLP
    engineering decision, quantity check or fabrication acceptance. Located
    reference groups are supported alongside solids for design coordination.
    """
    if not package.get('require_connection_parts'): return []
    try:from .bim_record_clauses import applications
    except ImportError:from bim_record_clauses import applications
    selected_clauses,clause_errors=applications(package)
    templates=bim_assemblies.index(package['physical'])
    records={r['id']:r for r in package.get('design_records',[])}
    materials={m['id']:m for m in package['physical'].get('materials',[])}
    patterns={
        'anchor': r'\banchor(?:s|[- _]+(?:rod|bolt)s?)?\b(?![- _]+bars?\b)|\bF1554\b.{0,35}\brods?\b|\brods?\b.{0,35}\bF1554\b',
        'bolt': r'\bbolts?\b|\bbolted\b',
        'screw': r'\bscrews?\b|\bscrewed\b',
        'weld': r'\bwelds?\b|\bwelded\b|\b(?:CJP|PJP)\b',
        'anchor_bar': r'\banchor[- _]+bars?\b',
    }
    def positive(text):
        # Recognize explicit exclusions without turning them into requirements.
        # Reinforcing bars developed around an anchorage are foundation rebar,
        # not a selection of an additional mechanical anchor rod.
        text=re.sub(r'\banchor[- ]reinforcement\b','foundation reinforcement',text,flags=re.I)
        text=re.sub(r'\banchor[- ]plates?\b','uplift end plate',text,flags=re.I)
        item=r'(?:(?:field|shop|additional|designed|anchor|CJP|PJP)\s+)*(?:bolts?|screws?|welds?|anchors?|rods?|studs?)\b'
        text=re.sub(r'\b(?:no|without)\s+'+item+r'(?:\s*(?:,|\bor\b|\band\b)\s*'+item+r')*','',text,flags=re.I)
        return re.sub(r'\b(?:anchor\s+rods?|bolts?|screws?|welds?|anchors?)\s+(?:are\s+)?(?:not required|not used|omitted|unnecessary)\b','',text,flags=re.I)
    def selection_text(value):
        if isinstance(value,str): return [positive(value)]
        if isinstance(value,list): return [text for item in value for text in selection_text(item)]
        if isinstance(value,dict):
            texts=[]; counts=[]
            for key,item in value.items():
                if key.lower() in ('cited','basis','note','notes','limit_state'): continue
                texts.extend(selection_text(item))
                match=re.fullmatch(r'(?:n_|num_|number_of_)(bolts|screws|anchors|welds)',key,re.I)
                if match and number(item,True): counts.append(match[1].lower())
            # HR's n_bolts also stores the count of explicitly selected anchor
            # rods; it must not require a second, unrelated bolt assembly.
            for category in counts:
                if category=='bolts' and any(re.search(patterns['anchor'],text,re.I) for text in texts): continue
                texts.append(category)
            return texts
        return []
    def part_label(part):
        label=part.get('kind','').replace('_',' ').replace('-',' ')
        if (part.get('kind') in ('plate','bar','formed_component') and
            part.get('geometry',{}).get('type') in ('box','mesh','extruded_profile') and
            re.search(patterns['anchor_bar'],str(part.get('parameters',{}).get('description','')),re.I)):
            label+=' anchor bar'
        if re.search(r'\b(?:bolts?|rods?|fasteners?|anchors?)\b',part.get('kind','').replace('_',' ').replace('-',' '),re.I):
            metadata=json.dumps([part.get('id',''),part.get('parameters',{}),materials.get(part.get('material_id'),{})])
            if re.search(r'anchor|F1554',metadata,re.I):label+=' anchor'
            if re.search(r'screw|C1513',metadata,re.I):label+=' screw'
        return label
    def selected_text(record):
        data=record.get('data',{});texts=[]
        for key in ('components','selection'):texts.extend(selection_text(data.get(key)))
        capacity=data.get('capacity')
        if isinstance(capacity,str) and re.search(r'(?:\d|#).{0,45}\b(?:bolts?|screws?|anchor(?:s| rods?)?)\b',capacity,re.I):texts.append(positive(capacity))
        return ' '.join(texts)
    def missing_categories(selected,kinds):
        missing=[]
        for kind,pattern in patterns.items():
            text=re.sub(patterns['anchor'],'',selected,flags=re.I) if kind=='bolt' else selected
            if re.search(pattern,text,re.I) and not re.search(pattern,kinds,re.I):missing.append(kind)
        return missing
    findings=[]
    local_parts={}
    for c in package['physical'].get('connections',[]):
        for rid in c.get('design_record_ids',[]):local_parts.setdefault((c.get('joint_id'),rid),[]).extend(bim_assemblies.parts(c,templates))
    for connection in package['physical'].get('connections',[]):
        if connection.get('scope')=='delegated': continue
        for rid in connection.get('design_record_ids',[]):
            record=records.get(rid,{})
            if record.get('waived') or record.get('table') not in ('connections','anchorage','holddowns','bases','schedules'): continue
            kinds=' '.join(part_label(part) for part in local_parts.get((connection.get('joint_id'),rid),[]))
            clause=selected_clauses.get((connection['id'],rid))
            if clause is not None:
                applied=next(s for s in connection['source_record_selections'] if s['record_id']==rid)
                other='components' if applied['field']=='selection' else 'selection'
                selected=' '.join([positive(clause),*selection_text(record.get('data',{}).get(other))])
            else:selected=selected_text(record)
            missing=missing_categories(selected,kinds)
            if missing:findings.append(dict(connection_id=connection['id'],design_record_id=rid,missing_component_categories=missing,selected_components=selected[:1200],returned_part_kinds=kinds))
    # A member/assembly coverage note must not hide an entirely absent selected
    # fastener family. Real rods represented as members remain valid; ordinary
    # columns or straps do not also stand for their selected bolts or anchors.
    physical=package['physical'];dispositions={d.get('record_id'):d for d in physical.get('record_dispositions',[])}
    linked_records={rid for c in physical.get('connections',[]) for rid in c.get('design_record_ids',[])}
    entities={e['id']:e for key in ('members','connections','panels') for e in physical.get(key,[])}
    absent_families=[]
    for rid,record in records.items():
        if rid in linked_records or record.get('waived') or record.get('table') not in ('connections','anchorage','holddowns','bases','schedules'):continue
        disposition=dispositions.get(rid,{})
        if disposition.get('status') in ('delegated','not_applicable'):continue
        selected=selected_text(record)
        bound=[e for e in entities.values() if rid in e.get('design_record_ids',[])]
        bound += [entities[eid] for eid in disposition.get('entity_ids',[]) if eid in entities]
        parts=[part for e in bound for part in e.get('parts',[e])]
        kinds=' '.join(part_label(part) for part in parts)
        missing=missing_categories(selected,kinds)
        if missing:absent_families.append(dict(design_record_id=rid,missing_component_categories=missing,selected_components=selected[:1200],returned_part_kinds=kinds,bound_entity_ids=sorted({e['id'] for e in bound})))
    result=[issue('source_record_selection',message,[cid,rid],'error') for cid,rid,message in clause_errors]
    if findings:
        finding=issue('connection_selected_components','Located connection parts omit component categories explicitly selected in the linked design; export the actual parts/reference groups without relabelling an unrelated solid',[f['connection_id'] for f in findings])
        finding.update(count=len(findings),examples=findings[:100]);result.append(finding)
    if absent_families:
        finding=issue('connection_selected_family','A selected connection family has no corresponding located component categories in its bound entities; a typical coverage note alone does not place its hardware',[f['design_record_id'] for f in absent_families])
        finding.update(count=len(absent_families),examples=absent_families[:100]);result.append(finding)
    return result


def fastener_quantity_findings(package):
    """A count label cannot multiply one solid into several fasteners.

    Compare explicit part-level counts with distinct solid occurrences in the
    same local design assembly. This is a conservative lower-bound check, not
    an interpretation of free-text schedules or a fabrication quantity audit.
    """
    if not package.get('require_connection_parts'): return []
    connections=package['physical'].get('connections',[])
    templates=bim_assemblies.index(package['physical'])
    solids={'box','cylinder','extruded_profile','mesh'}
    def category(part):
        label=part.get('kind','').lower().replace('_',' ').replace('-',' ')
        if re.search(r'\b(?:plates?|washers?|nuts?|heads?)\b',label):return None
        if re.search(r'\banchors?\b',label):return 'anchor'
        if re.search(r'\bscrews?\b',label):return 'screw'
        if re.search(r'\bbolts?\b',label):return 'bolt'
        return None
    def frozen(value):
        if isinstance(value,dict):return tuple((k,frozen(v)) for k,v in sorted(value.items()))
        if isinstance(value,list):return tuple(frozen(v) for v in value)
        return value
    local={}
    for connection in connections:
        if connection.get('scope')=='delegated':continue
        for rid in connection.get('design_record_ids',[]):
            local.setdefault((connection.get('joint_id'),rid),[]).extend(bim_assemblies.parts(connection,templates))
    findings=[]
    for connection in connections:
        if connection.get('scope')=='delegated':continue
        pool=bim_assemblies.parts(connection,templates)
        for rid in connection.get('design_record_ids',[]):pool.extend(local.get((connection.get('joint_id'),rid),[]))
        for part in bim_assemblies.parts(connection,templates):
            kind=category(part)
            if not kind or part.get('geometry',{}).get('type') not in solids:continue
            counts=[value for key,value in part.get('parameters',{}).items()
                    if key in ('count','quantity','n_bolts','n_anchors','n_screws')
                    and number(value,True) and value>1 and int(value)==value]
            if not counts:continue
            required=int(max(counts))
            actual=len({frozen([p.get('material_id'),p['geometry']]) for p in pool
                        if category(p)==kind and p.get('geometry',{}).get('type') in solids})
            if actual<required:
                findings.append(dict(connection_id=connection['id'],part_id=part['id'],
                    category=kind,declared_count=required,actual_distinct_solids=actual))
    if not findings:return []
    finding=issue('connection_fastener_quantity','A solid fastener count exceeds the distinct located fastener solids in its local design assembly; instantiate the actual checked pattern instead of multiplying a representative solid by a count label',[f['connection_id'] for f in findings])
    finding.update(count=len(findings),examples=findings[:100])
    return [finding]


def duplicate_connection_part_findings(package):
    """Reject exact repeated solid components inside one designed assembly.

    Two differently named top/bottom plates or welds at identical coordinates
    are duplicate geometry. This bounded check does not test general clashes
    or infer the correct replacement position. Explicit groups and reference
    curves remain supported; a group must not be copied onto itself either.
    """
    if not package.get('require_connection_parts'): return []
    duplicates=[]
    templates=bim_assemblies.index(package['physical'])
    for connection in package['physical'].get('connections',[]):
        if connection.get('scope')=='delegated': continue
        seen={}
        for part in bim_assemblies.parts(connection,templates):
            geometry=part.get('geometry',{})
            if geometry.get('type') not in ('box','cylinder','extruded_profile','mesh'): continue
            # Numeric JSON values compare equally (1 versus 1.0), so use a
            # recursive tuple rather than string serialisation for geometry.
            def frozen(value):
                if isinstance(value,dict): return tuple((k,frozen(v)) for k,v in sorted(value.items()))
                if isinstance(value,list): return tuple(frozen(v) for v in value)
                return value
            token=frozen([part.get('kind','').lower(),part.get('material_id'),geometry,part.get('parameters',{})])
            if token in seen:duplicates.append(dict(connection_id=connection['id'],part_ids=[seen[token],part['id']],kind=part.get('kind')))
            else:seen[token]=part['id']
    if not duplicates:return []
    finding=issue('connection_duplicate_parts','Distinct component IDs repeat identical solid geometry inside the same assembly; resolve actual placement or duplicate emission, without dropping a required component',[d['connection_id'] for d in duplicates])
    finding.update(count=len(duplicates),examples=duplicates[:100])
    return [finding]


def mapping_geometry_findings(package):
    """A physical line may join collinear segments, never stand for a section group.

    IDs alone prove accounting, not occurrence geometry. Compare each claimed
    interval with the declared reference axis (physical cutbacks are separate).
    Keep mismatches as missing physical information so coordination can proceed.
    """
    nodes = {n['id']: n['point'] for n in package['analysis']['nodes']}
    elements = {e['id']: e for e in package['analysis']['elements']}
    findings = []
    for member in package['physical']['members']:
        if member.get('status') != 'declared': continue
        claims = [(aid, 0, 1) for aid in member.get('analysis_ids', [])]
        claims += [(s['analysis_id'], s['start'], s['end']) for s in member.get('analysis_segments', [])]
        mapping=member.get('analysis_coordinate_mapping')
        order=[0,1,2]
        if mapping is not None:
            if (not isinstance(mapping,dict) or set(mapping)!={'axis_order','basis'} or
                not isinstance(mapping.get('axis_order'),list) or len(mapping['axis_order'])!=3 or
                any(type(v) is not int for v in mapping['axis_order']) or set(mapping['axis_order'])!={0,1,2} or
                not isinstance(mapping.get('basis'),str) or not mapping['basis'].strip() or not claims):
                findings.append(issue('analysis_coordinate_mapping',
                    'Declare a complete source-axis permutation and its basis on a member with analysis claims; scaling, translation and dropped coordinates are not supported',
                    [member['id']],'error'))
                continue
            order=mapping['axis_order']
        if not claims: continue
        a, b = member['axis']; direction = [y-x for x,y in zip(a,b)]
        length = norm(direction)
        if length < 1e-9: continue
        direction = [v/length for v in direction]; intervals = []; mismatch = False
        for aid, start, end in claims:
            element = elements.get(aid, {}); ids = element.get('node_ids', [])
            if len(ids) != 2 or any(n not in nodes for n in ids):
                mismatch = True; continue
            p, q = [[nodes[n][i] for i in order] for n in ids]; stations = []
            for t in (start, end):
                point = [x+t*(y-x) for x,y in zip(p,q)]
                station = sum((x-y)*d for x,y,d in zip(point,a,direction))
                residual = norm([x-y-station*d for x,y,d in zip(point,a,direction)])
                if residual > 1e-5 or station < -1e-5 or station > length+1e-5: mismatch = True
                stations.append(station)
            intervals.append(sorted(stations))
        # A solver may split one shaft into coincident axial and flexural
        # components. Require the explicit roles AND this known mechanical
        # pattern; overlapping ordinary frame elements are still an error.
        roles=member.get('analysis_component_roles')
        if roles is not None:
            valid=isinstance(roles,dict) and len(roles)==2 and all(isinstance(v,str) for v in roles.values()) and set(roles.values())=={'axial','flexural'} and set(roles)==set(member.get('analysis_ids',[])) and len(member.get('analysis_ids',[]))==2 and not member.get('analysis_segments')
            if valid:
                axial=elements.get(next(k for k,v in roles.items() if v=='axial'),{})
                flexural=elements.get(next(k for k,v in roles.items() if v=='flexural'),{})
                aa=axial.get('builder_arguments',[]);fa=flexural.get('builder_arguments',[])
                valid=len(aa)>4 and len(fa)>4 and str(aa[0]).lower() in ('truss','corottruss') and fa[0]=='elasticBeamColumn'
                valid=valid and number(aa[4]) and number(fa[4]) and 0<fa[4]<=aa[4]*1e-4
                valid=valid and len(intervals)==2 and all(abs(lo)<1e-5 and abs(hi-length)<1e-5 for lo,hi in intervals)
            if valid:intervals=intervals[:1]
            else:mismatch=True
        cursor = 0.0
        for lo, hi in sorted(intervals):
            if lo > cursor+1e-5 or lo < cursor-1e-5: mismatch = True
            cursor = max(cursor,hi)
        if abs(cursor-length) > 1e-5: mismatch = True
        if mismatch:
            findings.append(issue('analysis_occurrence_geometry',
                'Mapped analysis intervals must form the actual physical reference axis; a section group or catch-all is not a located member',
                [member['id']]+[c[0] for c in claims]))
    return findings


def _section_core(profile):
    """Non-overlapping straight web/flange strips; omit lips/fillets/radii.

    Coordinates match the physical profile reference points used by openBIM.
    A reduced core gives a conservative gross-interpenetration screen without
    adding a geometry dependency to HR/CFS. Unsupported shapes are not screened.
    """
    d=profile.get('dimensions',{});family=profile.get('family')
    def rect(x0,y0,x1,y1):return [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]
    try:
        if family in ('channel','z'):
            h,b,t=(d[k] for k in ('d','b','t'))
            if min(h,b,t)<=0 or h<=2*t or b<=t:return []
            result=[rect(-t/2,-h/2+t,t/2,h/2-t)]
            for sign in (-1,1):
                direction=1 if family=='channel' else sign
                x0,x1=sorted((-direction*t/2,direction*(b-t/2)))
                y0,y1=sorted((sign*(h/2-t),sign*h/2))
                result.append(rect(x0,y0,x1,y1))
            return result
        if family=='i':
            h,b,tf,tw=(d[k] for k in ('d','bf','tf','tw'))
            if min(h,b,tf,tw)<=0 or h<=2*tf or b<=tw:return []
            return [rect(-b/2,-h/2,b/2,-h/2+tf),rect(-tw/2,-h/2+tf,tw/2,h/2-tf),rect(-b/2,h/2-tf,b/2,h/2)]
        if family=='rhs':
            h,b,t=(d[k] for k in ('h','b','t'))
            if min(h,b,t)<=0 or min(h,b)<=2*t:return []
            return [rect(-b/2,-h/2,b/2,-h/2+t),rect(-b/2,h/2-t,b/2,h/2),rect(-b/2,-h/2+t,-b/2+t,h/2-t),rect(b/2-t,-h/2+t,b/2,h/2-t)]
        if family=='flat':
            b,t=d['width'],d['thickness']
            if min(b,t)>0:return [rect(-b/2,-t/2,b/2,t/2)]
        if family=='angle':
            h,b,t=(d[k] for k in ('h','b','t'))
            if min(h,b,t)>0 and min(h,b)>t:return [rect(0,0,b,t),rect(0,t,t,h)]
    except (KeyError,TypeError,ValueError):pass
    return []


def _polygon_area(points):
    return abs(sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(points,points[1:]+points[:1])))/2


def _convex_overlap(first,second):
    """Sutherland-Hodgman clipping of counterclockwise convex polygons."""
    points=first
    for a,b in zip(second,second[1:]+second[:1]):
        if not points:return 0.
        def side(p):return (b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0])
        clipped=[]
        for p,q in zip(points,points[1:]+points[:1]):
            dp,dq=side(p),side(q);inside_p,inside_q=dp>=-1e-10,dq>=-1e-10
            if inside_p:clipped.append(p)
            if inside_p!=inside_q and abs(dp-dq)>1e-12:
                t=dp/(dp-dq);clipped.append([x+t*(y-x) for x,y in zip(p,q)])
        points=clipped
    return _polygon_area(points)


def _constituent_overlaps(profiles):
    findings=[]
    for profile in profiles.values():
        if profile.get('family')!='built_up':continue
        components=[]
        for component in profile.get('components',[]):
            polygons=_section_core(profiles.get(component.get('profile_id'),{}))
            # Shape screens also run on partial source data. The schema check
            # reports missing transforms; this screen must not invent them or
            # crash before those findings can reach the request register.
            offset=component.get('offset')
            if not polygons or not number(component.get('rotation_deg')) or not isinstance(offset,list) or len(offset)!=2 or not all(number(v) for v in offset):
                components.append([]);continue
            angle=math.radians(component['rotation_deg']);co,si=math.cos(angle),math.sin(angle);u,v=component['offset']
            components.append([[[co*x-si*y+u,si*x+co*y+v] for x,y in poly] for poly in polygons])
        for i,first in enumerate(components):
            for j in range(i+1,len(components)):
                second=components[j];area=min(sum(map(_polygon_area,first)),sum(map(_polygon_area,second)))
                if area<=0:continue
                overlap=sum(_convex_overlap(a,b) for a in first for b in second)
                if overlap>.005 and overlap/area>.05:
                    findings.append(dict(profile_id=profile['id'],components=[i,j],overlap_area_in2=round(overlap,6),overlap_fraction=round(overlap/area,6)))
    return findings


def _opening_penetrations(physical):
    """Explicit member-clearance openings, never a blanket clash exemption."""
    members={m['id']:m for m in physical.get('members',[])};allowed={};errors=[]
    for opening in physical.get('openings',[]):
        if 'penetrating_member_ids' not in opening:continue
        ids=opening['penetrating_member_ids']
        if (opening.get('kind') not in ('member_clearance','wall_bearing_holdback') or
            not isinstance(ids,list) or not ids or not all(isinstance(mid,str) for mid in ids) or
            len(set(ids))!=len(ids) or not isinstance(opening.get('penetration_basis'),str) or
            not opening['penetration_basis'].strip() or
            any(mid not in members or members[mid].get('status')!='declared' for mid in ids)):
            errors.append(dict(opening_id=opening.get('id'),reason='A member-clearance opening needs distinct actual declared member IDs and an explicit penetration basis'));continue
        allowed[opening['id']]=set(ids)
    return allowed,errors


def _opening_workline_clashes(physical):
    """Screen actual member axes against convex, planar opening reservations.

    Clip against the opening's own edge planes, never its enclosing rectangle.
    A five-millimetre jamb/head/sill tolerance avoids boundary contacts. The
    declared host thickness defines the normal slab. This is a limited axis
    check: absence of a finding does not establish full section/part clearance.
    Unsupported concave/nonplanar boundaries are left for geometry review.
    """
    def dot(a,b):return sum(x*y for x,y in zip(a,b))
    def sub(a,b):return [x-y for x,y in zip(a,b)]
    def cross(a,b):return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
    members=[];findings=[];tolerance=.005/.0254;penetrations,_=_opening_penetrations(physical)
    for member in physical.get('members',[]):
        if member.get('status')!='declared':continue
        axis=member.get('axis',[]);offsets=member.get('end_offsets',[])
        if not isinstance(axis,list) or not isinstance(offsets,list) or len(axis)!=2 or len(offsets)!=2 or not all(vector(v) for v in axis+offsets):continue
        a,b=[[v+off for v,off in zip(p,o)] for p,o in zip(axis,offsets)]
        members.append((member['id'],a,b,sub(b,a),[(min(x,y),max(x,y)) for x,y in zip(a,b)]))
    panels={p['id']:p for p in physical.get('panels',[])}
    for opening in physical.get('openings',[]):
        points=opening.get('vertices',[]);host=panels.get(opening.get('host_id'),{})
        if len(points)<3 or not all(vector(p) for p in points) or not number(host.get('thickness'),True):continue
        if points[-1]==points[0]:points=points[:-1]
        normal=[0.,0.,0.]
        for p,q in zip(points,points[1:]+points[:1]):
            normal=[x+y for x,y in zip(normal,cross(sub(p,points[0]),sub(q,points[0])))]
        length=norm(normal)
        if length<1e-10:continue
        normal=[v/length for v in normal]
        if any(abs(dot(normal,sub(p,points[0])))>1e-5 for p in points):continue
        thickness=host['thickness'];direction=host.get('thickness_direction')
        if direction in ('negative_global_z','positive_global_z'):
            # A declared top/bottom surface is not a mid-surface. Preserve the
            # global extrusion direction also for a sloped roof and either
            # polygon winding, rather than screening a symmetric fictitious slab.
            if abs(normal[2])<1e-10:continue
            depth=[0.,0.,-1. if direction=='negative_global_z' else 1.];start,end=0.,thickness
        else:depth=normal;start,end=-thickness/2,thickness/2
        offsets=sorted(dot(normal,depth)*v for v in (start,end));datum=dot(normal,points[0])
        planes=[(normal,datum+offsets[0]),([-v for v in normal],-datum-offsets[1])]
        inward_depth=depth if dot(normal,depth)>0 else [-v for v in depth]
        convex=True
        for p,q in zip(points,points[1:]+points[:1]):
            inward=cross(inward_depth,sub(q,p));length=norm(inward)
            if length<1e-10:continue
            inward=[v/length for v in inward]
            if any(dot(inward,sub(v,p)) < -1e-6 for v in points):convex=False;break
            planes.append((inward,dot(inward,p)+tolerance))
        if not convex or len(planes)<5:continue
        bounds=[(min(p[k]+depth[k]*v for p in points for v in (start,end)),
                 max(p[k]+depth[k]*v for p in points for v in (start,end))) for k in range(3)]
        for member_id,a,b,delta,member_bounds in members:
            if member_id in penetrations.get(opening['id'],()):continue
            if any(hi<lo2 or hi2<lo for (lo,hi),(lo2,hi2) in zip(bounds,member_bounds)):continue
            lo,hi=0.,1.
            for inward,minimum in planes:
                start=dot(inward,a)-minimum;rate=dot(inward,delta)
                if abs(rate)<1e-12:
                    if start < -1e-10:lo,hi=1.,0.;break
                elif rate>0:lo=max(lo,-start/rate)
                else:hi=min(hi,-start/rate)
                if hi<lo:break
            if (hi-lo)*norm(delta)>1e-5:
                findings.append(dict(opening_id=opening['id'],host_id=host['id'],member_id=member_id))
    return findings


def _part_dimension_mismatches(physical):
    """Detect contradictory explicit inch labels on supported part solids.

    This compares declared data, not engineering capacities. Plate thickness
    may lie on any local box axis. Cylindrical heads/nuts/washers are excluded
    because their nominal fastener diameter need not equal the outer solid.
    """
    findings=[]
    for connection in physical.get('connections',[]):
        for part in connection.get('parts',[]):
            geometry=part.get('geometry',{});parameters=part.get('parameters',{});kind=part.get('kind','').lower()
            tests=[]
            if geometry.get('type')=='box' and vector(geometry.get('size')) and any(word in kind for word in ('plate','gusset','stiffener','washer','strap','sheet')):
                tests=[(key,geometry['size']) for key in ('thickness_in','plate_thickness_in') if key in parameters]
            elif geometry.get('type')=='cylinder' and number(geometry.get('radius'),True) and not any(word in kind for word in ('head','nut','washer')):
                tests=[('diameter_in',[2*geometry['radius']])] if 'diameter_in' in parameters else []
            for key,dimensions in tests:
                expected=parameters[key]
                if number(expected,True) and min(abs(value-expected) for value in dimensions)>max(.005,.02*expected):
                    findings.append(dict(connection_id=connection['id'],part_id=part['id'],parameter=key,declared_value_in=expected,geometry_dimensions_in=dimensions))
    return findings


def physical_screens(physical):
    """Limited geometry screens; no source data is changed or approved."""
    findings=[]
    def add(code,title,rows):
        if rows:findings.append(dict(code=code,title=title,severity='needs_review',count=len(rows),examples=rows[:100]))
    profiles={p['id']:p for p in physical.get('profiles',[])};plies=[]
    for profile in profiles.values():
        if profile.get('family')!='built_up':continue
        components=profile.get('components',[])
        match=re.fullmatch(r'(?:box-)?([1-9][0-9]*)[xX](.+)',profile['id'])
        if match and components and all(c['profile_id']==match[2] for c in components) and len(components)!=int(match[1]):
            plies.append(dict(profile_id=profile['id'],named_count=int(match[1]),component_count=len(components),constituent=match[2]))
    add('physical_ply_count','Built-up section labels disagree with constituent counts',plies)
    add('physical_constituent_overlap','Built-up constituents substantially overlap; channel offsets use the web centre, not a bounding-box centre',_constituent_overlaps(profiles))
    panels={p['id']:p for p in physical.get('panels',[])};outside=[];off_plane=[]
    for opening in physical.get('openings',[]):
        host=panels.get(opening.get('host_id'),{});vertices=host.get('vertices',[])
        if len(vertices)<3 or not all(vector(v) for v in vertices):continue
        bounds=[(min(v[k] for v in vertices),max(v[k] for v in vertices)) for k in range(3)]
        excess=max((max(lo-v[k],v[k]-hi,0.) for v in opening.get('vertices',[]) for k,(lo,hi) in enumerate(bounds)),default=0.)
        if excess>1e-5:outside.append(dict(opening_id=opening['id'],host_id=host['id'],outside_distance_in=excess))
        a=[v-u for u,v in zip(vertices[0],vertices[1])]
        for vertex in vertices[2:]:
            b=[v-u for u,v in zip(vertices[0],vertex)]
            normal=[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
            length=norm(normal)
            if length<=1e-10:continue
            distance=max((abs(sum((v-u)*n for u,v,n in zip(vertices[0],point,normal)))/length for point in opening.get('vertices',[])),default=0.)
            if distance>1e-5:off_plane.append(dict(opening_id=opening['id'],host_id=host['id'],distance_to_host_plane_in=distance))
            break
    add('physical_opening_host','Opening boundaries extend outside their declared host bounds',outside)
    add('physical_opening_host_plane','Opening boundaries must lie in their declared panel plane to form an actual opening',off_plane)
    add('physical_opening_penetration','Identify the actual members intentionally passing through this material-clearance opening',_opening_penetrations(physical)[1])
    add('physical_opening_workline','Member worklines cross a declared opening; coordinate the framing and then check full section and connection clearance',_opening_workline_clashes(physical))
    add('physical_part_dimensions','Connection solid dimensions contradict explicit thickness/diameter parameters; reconcile both with the completed design',_part_dimension_mismatches(physical))
    joints={j['id']:j for j in physical.get('joints',[])};remote=[]
    for connection in physical.get('connections',[]):
        point=joints.get(connection.get('joint_id'),{}).get('point')
        if not vector(point):continue
        for part in connection.get('parts',[]):
            g=part.get('geometry',{})
            # Only explicitly sized boxes are screened here; other part types
            # still need review. A distant origin alone is not proof of distance.
            if g.get('type')!='box' or geometry_errors(g,profiles):continue
            x,y=g['x_axis'],g['y_axis'];z=[x[1]*y[2]-x[2]*y[1],x[2]*y[0]-x[0]*y[2],x[0]*y[1]-x[1]*y[0]]
            points=[[g['origin'][k]+i*g['size'][0]*x[k]+j*g['size'][1]*y[k]+l*g['size'][2]*z[k] for k in range(3)] for i in (0,1) for j in (0,1) for l in (0,1)]
            bounds=[(min(v[k] for v in points),max(v[k] for v in points)) for k in range(3)]
            distance=math.sqrt(sum(max(lo-point[k],point[k]-hi,0.)**2 for k,(lo,hi) in enumerate(bounds)))
            if distance>72:remote.append(dict(connection_id=connection['id'],joint_id=connection.get('joint_id'),part_id=part['id'],distance_to_part_bounds_in=round(distance,6)))
    add('physical_remote_part','Connection box parts are over six feet from their assigned joint',remote)
    return findings


def _polyline_distance(first,second):
    def distance(points,line):
        maximum=0.
        for point in points:
            minimum=float('inf')
            for a,b in zip(line,line[1:]):
                delta=[y-x for x,y in zip(a,b)];denom=sum(v*v for v in delta)
                if denom<1e-18:continue
                t=max(0.,min(1.,sum((p-x)*v for p,x,v in zip(point,a,delta))/denom))
                minimum=min(minimum,norm([p-x-t*v for p,x,v in zip(point,a,delta)]))
            maximum=max(maximum,minimum)
        return maximum
    length=lambda line:sum(norm([y-x for x,y in zip(a,b)]) for a,b in zip(line,line[1:]))
    return max(distance(first,second),distance(second,first),abs(length(first)-length(second)))


def architectural_input_findings(package):
    """Check preserved source requirements independently of claimed coverage."""
    source=package.get('architectural_input')
    if source is None:return []  # legacy and ordinary non-IFC jobs retain their contract
    findings=[];tolerance=.005/.0254;actual=package.get('reference_system',{})
    if source['reference_system'].get('source_ifc_sha256')!=source['source_ifc_sha256'] or any(p[0]>p[1] for row in source['openings'] for p in row['bounds_in']):
        return [issue('architectural_input_invalid','Architectural source identity or bounds are inconsistent',severity='error')]
    if actual.get('source_ifc_sha256')!=source['source_ifc_sha256']:
        findings.append(issue('architectural_source_identity','Retain the exact original IFC fingerprint'))
    missing=[]
    for row in source['reference_system']['levels']:
        if not any(row.get('name')==v.get('name') and abs(row['elevation_in']-v['elevation_in'])<tolerance for v in actual.get('levels',[])):
            missing.append(row.get('name',str(row['level'])))
    if missing:findings.append(issue('architectural_levels','Preserve every original named architectural datum',missing))
    missing=[]
    for row in source['reference_system']['grids']:
        if not any(row['label']==v['label'] and row['direction']==v['direction'] and _polyline_distance(row['points_in'],v['points_in'])<tolerance for v in actual.get('grids',[])):
            missing.append(row['label'])
    if missing:findings.append(issue('architectural_grids','Preserve original grid labels, axis families and coordinates',missing))
    missing=[];changed=[];duplicates=[]
    for row in source['openings']:
        matching=[o for o in package['physical']['openings'] if o.get('source_ifc_guid')==row['source_ifc_guid']]
        if not matching:missing.append(row['source_ifc_guid']);continue
        if len(matching)>1:duplicates.append(row['source_ifc_guid']);continue
        bounds=[[min(p[k] for p in matching[0]['vertices']),max(p[k] for p in matching[0]['vertices'])] for k in range(3)]
        expected=row['bounds_in'];normal=min(range(3),key=lambda k:expected[k][1]-expected[k][0])
        errors=[abs(sum(bounds[normal])/2-sum(expected[normal])/2)]
        errors += [abs(bounds[k][side]-expected[k][side]) for k in range(3) if k!=normal for side in (0,1)]
        if max(errors)>tolerance:changed.append(row['source_ifc_guid'])
    if missing:findings.append(issue('architectural_openings','Every original door/window needs a located physical opening with its source_ifc_guid; representative subsets are incomplete',missing))
    if changed:findings.append(issue('architectural_opening_bounds','Physical openings differ from their original architectural bounds',changed))
    if duplicates:findings.append(issue('architectural_opening_duplicates','An original opening was exported more than once',duplicates))
    seen={};duplicates=[]
    for member in package['physical']['members']:
        if member.get('status')!='declared':continue
        offsets=member.get('end_offsets')
        if not isinstance(offsets,list) or len(offsets)!=2 or not all(vector(v) for v in offsets) or not vector(member.get('local_y')):continue
        endpoints=[tuple(round(v+offset,6) for v,offset in zip(p,off)) for p,off in zip(member['axis'],offsets)]
        key=(tuple(sorted(endpoints)),member['profile_id'],tuple(round(v,6) for v in member['local_y']))
        if key in seen:duplicates.extend([seen[key],member['id']])
        seen[key]=member['id']
    if duplicates:findings.append(issue('duplicate_physical_members','Coincident identical member occurrences need a real offset or one combined physical declaration',duplicates))
    return findings


def member_record_findings(package):
    """Check explicit rolled/HSS section labels on directly linked member records.

    This is a data-link check, not capacity validation. Arbitrary profile IDs,
    constituent designs of built-up sections and records without an explicit
    section are not inferred from their names or dimensions.
    """
    def section(value):
        if not isinstance(value,str):return None
        value=re.sub(r'\s+','',value.upper()).replace('\u00d7','X')
        return value if re.fullmatch(r'(?:W|WT|S|HP|C|MC|L|HSS)\d+(?:\.\d+)?(?:X\d+(?:[./]\d+)?){1,3}',value) else None
    physical=package.get('physical',{})
    profiles={p['id']:p for p in physical.get('profiles',[])}
    records={r['id']:r for r in package.get('design_records',[])}
    mismatches=[]
    for member in physical.get('members',[]):
        profile=profiles.get(member.get('profile_id'),{})
        selected=section(profile.get('id'))
        if not selected or profile.get('family')=='built_up':continue
        for ref in member.get('design_record_ids',[]):
            record=records.get(ref,{})
            if record.get('table')!='members':continue
            data=record.get('data',{});inputs=data.get('inputs',{})
            if not isinstance(inputs,dict):continue
            checked=section(inputs.get('section'))
            if checked and checked!=selected:
                mismatches.append(dict(member_id=member['id'],profile_id=profile['id'],record_id=ref,record_section=inputs['section']))
    if not mismatches:return []
    finding=issue('member_design_section','A physical member links to a calculation for a different section; bind the actual matching member design record',
                  sorted({m['member_id'] for m in mismatches}|{m['record_id'] for m in mismatches}),'needs_input')
    finding.update(count=len(mismatches),examples=mismatches[:100])
    return [finding]


def validate(package):
    """Validate finite geometry, references, coverage and readiness independently.

    Returns findings. The reported ready flag is never trusted by a consumer.
    This is data validation, not a structural capacity or constructability check.
    """
    from jsonschema import Draft202012Validator
    try:
        json.dumps(package,allow_nan=False)
    except (ValueError,TypeError) as ex:
        return [issue("json_value",str(ex),severity="error")]
    schema=json.loads(Path(__file__).with_name("bim_package.schema.json").read_text(encoding="utf-8"))
    structural=list(Draft202012Validator(schema).iter_errors(package))
    if structural:
        return [issue("schema",e.message,["/"+"/".join(map(str,e.absolute_path))],"error") for e in structural]
    errors = []
    def bad(code, msg, ids=(), severity="error"):
        errors.append(issue(code, msg, ids, severity))
    if package.get("schema") != "steltic.bim_package" or package.get("schema_version") != VERSION:
        bad("schema", "Unsupported BIM schema/version")
    if package.get("units") != {"length":"in", "force":"kip", "stress":"ksi"}:
        bad("units", "Canonical units must be in / kip / ksi")
    physical = package.get("physical", {})
    templates=bim_assemblies.index(physical)
    errors.extend(bim_assemblies.findings(package))
    bond_errors,verified_bonds=bim_bonds.evaluate(physical)
    errors.extend(issue('reinforcement_bond',message,[cid],'error') for cid,message in bond_errors)
    indexes = {}
    for key in COLLECTIONS:
        rows = physical.get(key, [])
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"] for r in rows):
            bad("entity_id", "Invalid collection/ID: " + key)
            indexes[key] = {}
            continue
        indexes[key] = {r["id"]:r for r in rows}
        if len(indexes[key]) != len(rows): bad("duplicate_id", "Duplicate IDs in " + key)
    profiles, materials = indexes["profiles"], indexes["materials"]
    profile_dimensions={"i":("d","bf","tf","tw"),"i_tapered":("d","bf","tf_tip","tf_root","tw"),"rhs":("h","b","t"),"chs":("d","t"),
                        "channel":("d","b","lip","t","r"),"z":("d","b","lip","t","r"),
                        "angle":("h","b","t"),"flat":("width","thickness"),"round":("d",),"box_envelope":("width","depth")}
    for p in profiles.values():
        if p.get("status") == "declared":
            dims = p.get("dimensions", {})
            family=p.get("family")
            if family=="built_up":
                if not p.get("components"): bad("profile_geometry","Built-up profile needs constituent profiles and placements",[p["id"]])
                for component in p.get("components",[]):
                    target=profiles.get(component["profile_id"],{})
                    if target.get("status")!="declared" or target.get("family")=="built_up": bad("profile_geometry","Constituents must reference declared non-built-up profiles",[p["id"]])
            elif family not in profile_dimensions or any(not number(dims.get(k)) or (dims[k]<0 if k in ("lip","r") else dims[k]<=0) for k in profile_dimensions[family]):
                bad("profile_geometry", "Declare required dimensions for a supported profile family", [p["id"]])
            if family=='i_tapered' and all(number(dims.get(k),True) for k in profile_dimensions[family]):
                if dims['tf_tip']>dims['tf_root'] or 2*dims['tf_root']>=dims['d'] or dims['tw']>=dims['bf']:
                    bad('profile_geometry','Tapered flange tip/root and web must fit the section envelope',[p['id']])
            if family=="box_envelope" and (p.get("scope")!="delegated" or not p.get("basis")): bad("profile_geometry","An envelope needs delegated scope and its basis",[p["id"]])
            if family in profile_dimensions and all(number(dims.get(k),True) for k in profile_dimensions[family]):
                if ((family=="i" and (2*dims["tf"]>=dims["d"] or dims["tw"]>=dims["bf"])) or
                    (family=="rhs" and 2*dims["t"]>=min(dims["h"],dims["b"])) or
                    (family=="chs" and 2*dims["t"]>=dims["d"])):
                    bad("profile_geometry","Thickness consumes or exceeds the profile envelope",[p["id"]])
    for m in materials.values():
        if not m.get("specification"): bad("material_specification", "Material specification missing", [m["id"]])
    analysis_nodes=package["analysis"]["nodes"]
    node_ids={n["id"] for n in analysis_nodes}
    analysis_elements=package["analysis"]["elements"]
    analysis_ids = {str(e["id"]) for e in analysis_elements}
    if len(node_ids)!=len(analysis_nodes) or len(analysis_ids)!=len(analysis_elements): bad("analysis_ids","Duplicate analysis node/element IDs")
    for e in analysis_elements:
        if any(n not in node_ids for n in e["node_ids"]): bad("analysis_nodes","Analysis element references missing nodes",[e["id"]])
    represented = {}
    portions = {}
    candidates = []
    for m in indexes["members"].values():
        mid = m["id"]
        axis = m.get("axis", [])
        if len(axis) != 2 or not all(vector(p) for p in axis) or axis[0] == axis[1]:
            bad("member_axis", "Invalid member axis", [mid]); continue
        for aid in m.get("analysis_ids", []):
            if aid not in analysis_ids: bad("analysis_reference", "Unknown analysis element", [mid, aid])
            represented.setdefault(aid, []).append(mid)
            portions.setdefault(aid, []).append((0.0, 1.0, mid))
        for segment in m.get("analysis_segments", []):
            aid, a, b = segment["analysis_id"], segment["start"], segment["end"]
            if aid not in analysis_ids: bad("analysis_reference", "Unknown analysis element", [mid, aid])
            if not (0 <= a < b <= 1): bad("analysis_interval", "Analysis segment must be a nonempty fraction within [0,1]", [mid, aid])
            represented.setdefault(aid, []).append(mid)
            portions.setdefault(aid, []).append((a, b, mid))
        if m.get("status") != "declared":
            candidates.append(mid); continue
        direction = [b-a for a,b in zip(*axis)]
        direction = [v/norm(direction) for v in direction]
        if not perpendicular(direction, m.get("local_y")): bad("member_axes", "Member local_y must be perpendicular unit vector", [mid])
        if m.get("profile_id") not in profiles or profiles.get(m.get("profile_id"), {}).get("status") != "declared": bad("member_profile", "Unresolved physical profile", [mid])
        if m.get("material_id") not in materials: bad("member_material", "Unresolved material", [mid])
        if not m.get("reference_line") or not isinstance(m.get("end_offsets"), list) or len(m["end_offsets"]) != 2 or not all(vector(v) for v in m["end_offsets"]):
            bad("member_placement", "Declare reference line and both physical end offsets", [mid])
        elif 'slots' in m:
            ends=[[v+o for v,o in zip(p,offset)] for p,offset in zip(axis,m['end_offsets'])]
            try:member_slots.validated(m['slots'],math.dist(*ends))
            except (ValueError,TypeError) as error:bad('member_slot',str(error),[mid])
    if candidates: bad("member_placement", "Confirm physical placement/profile/material of recovered analysis members", candidates, "needs_input")
    for aid, rows in portions.items():
        cursor = 0.0
        for a, b, mid in sorted(rows):
            if a < cursor - 1e-7: bad("analysis_overlap", "Physical members claim overlapping analysis intervals", [aid, mid])
            if a > cursor + 1e-7: bad("analysis_mapping", "Unmapped analysis interval", [aid], "needs_input")
            cursor = max(cursor, b)
        if cursor < 1 - 1e-7: bad("analysis_mapping", "Unmapped analysis interval", [aid], "needs_input")
    excluded = {}
    for row in physical.get("analysis_only", []):
        if not isinstance(row, dict) or row.get("analysis_id") not in analysis_ids or not row.get("reason"):
            bad("analysis_only", "Invalid analysis-only declaration"); continue
        aid = row["analysis_id"]
        if aid in excluded or aid in represented: bad("analysis_overlap", "Duplicate/overlapping analysis-only declaration", [aid])
        excluded[aid] = row
    missing_analysis = analysis_ids - set(represented) - set(excluded)
    if missing_analysis: bad("analysis_mapping", "Map or explicitly classify every analysis element", missing_analysis, "needs_input")
    errors.extend(mapping_geometry_findings(package))
    panel_ids = indexes["panels"]
    for group in ("panels", "openings"):
        for p in indexes[group].values():
            points = p.get("vertices", [])
            if len(points) < 3 or not all(vector(v) for v in points): bad("panel_geometry", "Invalid boundary vertices", [p["id"]])
            elif len({tuple(v) for v in points}) < 3: bad("panel_geometry", "Degenerate boundary", [p["id"]])
            else:
                a=[x-y for x,y in zip(points[1],points[0])]
                normals=[]
                for v in points[2:]:
                    b=[x-y for x,y in zip(v,points[0])]
                    normal=[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
                    if norm(normal)>1e-8: normals.append(normal)
                if not normals: bad("panel_geometry","Collinear boundary",[p["id"]])
                elif any(abs(sum(n*(x-y) for n,x,y in zip(normals[0],v,points[0])))/norm(normals[0])>1e-6 for v in points): bad("panel_geometry","Nonplanar boundary",[p["id"]])
                elif not _simple_boundary(points,normals[0]): bad("panel_geometry","Boundary must be a simple, non-self-intersecting ring in perimeter order",[p["id"]])
            if group == "panels":
                if p.get("status") != "declared": bad("panel_placement", "Confirm recovered panel assembly and placement", [p["id"]], "needs_input")
                elif not number(p.get("thickness"), True) or p.get("material_id") not in materials: bad("panel_specification", "Panel thickness/material missing", [p["id"]])
            elif p.get("host_id") not in panel_ids and p.get("host_id") not in indexes["members"]:
                bad("opening_host", "Unknown opening host", [p["id"]])
    ends = {}
    for j in indexes["joints"].values():
        if not vector(j.get("point")) or not (j.get("member_ends") or j.get("attachments")): bad("joint_geometry", "Joint needs a point and member ends or support attachments", [j["id"]])
        for end in j.get("member_ends", []):
            key = (end.get("member_id"), end.get("end"))
            if key[0] not in indexes["members"] or key[1] not in ("start", "end"): bad("joint_member", "Invalid joint member/end", [j["id"]])
            else:
                member=indexes["members"][key[0]]
                endpoint=member["axis"][0 if key[1]=="start" else 1]
                offset=end.get("joint_offset",[0,0,0])
                if norm([p-a-o for p,a,o in zip(j["point"],endpoint,offset)])>1e-6:
                    bad("joint_placement","Joint point must match the member reference end plus its explicit joint_offset",[j["id"],key[0]])
            if key in ends: bad("duplicate_joint_end", "Member end assigned to multiple joints", [j["id"], ends[key]])
            ends[key] = j["id"]
        for attachment in j.get("attachments", []):
            mid, t = attachment["member_id"], attachment["station"]
            if mid not in indexes["members"] or not 0 <= t <= 1:
                bad("joint_attachment", "Support attachment must lie on a known member", [j["id"], mid]); continue
            a, b = indexes["members"][mid]["axis"]
            offset = attachment.get("offset", [0,0,0])
            expected = [x + t*(y-x) + o for x,y,o in zip(a,b,offset)]
            if norm([p-q for p,q in zip(j["point"],expected)]) > 1e-6:
                bad("joint_attachment", "Joint does not match its declared support station and offset", [j["id"], mid])
    records = {r["id"]:r for r in package.get("design_records", [])}
    if len(records)!=len(package["design_records"]): bad("design_record_ids","Duplicate design record IDs")
    errors.extend(member_record_findings(package))
    linked = set(); joined = set()
    for collection in COLLECTIONS:
        if collection=="connections": continue
        for entity in indexes[collection].values():
            refs=entity.get("design_record_ids",[])
            if not isinstance(refs,list) or any(not isinstance(ref,str) or ref not in records for ref in refs): bad("entity_design","Physical entity has invalid design record references",[entity["id"]])
            else: linked.update(refs)
    for c in indexes["connections"].values():
        cid = c["id"]
        if c.get("joint_id") not in indexes["joints"]: bad("connection_joint", "Connection has unknown joint", [cid])
        else: joined.add(c["joint_id"])
        refs = c.get("design_record_ids", [])
        if c.get('scope')=='coordination':
            info=package.get('information',{})
            logged={d.get('request_id') for d in info.get('decisions',[]) if d.get('action')=='accept_test' and d.get('author') and d.get('note')}
            accepted={r['id'] for r in info.get('requests',[]) if r.get('status')=='accepted_test' and r.get('fictional') is True and r.get('quantitatively_resolved') is True and r['id'] in logged}
            decisions=c.get('decision_ids',[])
            if info.get('fictional') is not True or c.get('engineering_status')!='not_checked' or not decisions or not set(decisions)<=accepted:
                bad('connection_coordination_basis','Fictional coordination geometry needs explicit accepted test decisions and not_checked engineering status', [cid])
            if any(ref not in records for ref in refs):bad('connection_design','Connection refers to unknown design records',[cid])
        elif not refs or any(ref not in records for ref in refs): bad("connection_design", "Connection needs valid design record IDs", [cid])
        linked.update(refs)
        parts = bim_assemblies.parts(c,templates)
        detail = c.get("detail_ref", {})
        if c.get("scope") == "delegated":
            if not c.get("responsible_party") or not detail.get("id") or not detail.get("revision") or not c.get("representation"):
                bad("delegated_interface", "Delegated connection needs party, versioned detail and representation", [cid])
        elif not parts and not (detail.get("id") and detail.get("revision")):
            bad("connection_geometry", "Connection needs parts or a versioned design-detail reference", [cid])
        if package.get('require_connection_parts') and c.get('scope')!='delegated' and not bim_assemblies.has_physical_connection(c,templates,verified_bonds=verified_bonds):
            bad('connection_parts_required','This job requires located physical parts for designed connections; a detail reference alone is incomplete',[cid],'needs_input')
        part_ids = set()
        # Local template geometry/materials were validated once above. Occurrence
        # frames and bindings are checked independently for every joint.
        for part in c.get('parts',[]):
            if not isinstance(part, dict) or not part.get("id") or part["id"] in part_ids:
                bad("connection_part", "Invalid/duplicate connection part ID", [cid]); continue
            part_ids.add(part["id"])
            if not part.get("kind") or part.get("material_id") not in materials: bad("connection_part", "Part kind/material missing", [cid, part["id"]])
            for msg in geometry_errors(part.get("geometry"), profiles): bad("connection_geometry", msg, [cid, part["id"]])
    for d in physical.get('joint_dispositions',[]):
        refs=d.get('design_record_ids',[])
        if any(ref not in records for ref in refs): bad('joint_disposition_design','Joint disposition references unknown design records',[d.get('joint_id')])
        elif d.get('kind')=='bearing_interface': linked.update(refs)
    dispositions = {}
    for d in physical.get("record_dispositions", []):
        rid = d.get("record_id")
        if rid not in records or rid in dispositions or not d.get("reason") or d.get("status") not in ("not_applicable", "delegated", "covered_by_assembly"):
            bad("record_disposition", "Invalid source-record disposition", [str(rid)])
        dispositions[rid] = d
        if d.get("status")=="delegated" and d.get("interface_id") not in indexes["interfaces"]: bad("record_disposition","Delegation needs a declared interface",[str(rid)])
        if d.get("status")=="covered_by_assembly" and (not d.get("entity_ids") or any(not any(e in indexes[k] for k in COLLECTIONS) for e in d["entity_ids"])): bad("record_disposition","Assembly coverage needs physical entity references",[str(rid)])
    unlinked = [r["id"] for r in records.values() if r["table"] == "connections" and not r.get("waived") and r["id"] not in linked and r["id"] not in dispositions]
    if unlinked: bad("connection_mapping", "Assign each connection record to occurrences or document its disposition", unlinked, "needs_input")
    unassigned=[r["id"] for r in records.values() if r["table"] in DRAWING_TABLES-{"connections"} and not r.get("waived") and r["id"] not in linked and r["id"] not in dispositions]
    if unassigned: bad("assembly_mapping","Assign drawing-related member/schedule records to physical entities or documented scope",unassigned,"needs_input")
    unjoined = [m["id"]+":"+end for m in indexes["members"].values() if m.get("status") == "declared" for end in ("start", "end") if (m["id"],end) not in ends]
    if unjoined: bad("member_end_mapping", "Assign physical member ends to joints", unjoined, "needs_input")
    free_joints = set(indexes["joints"]) - joined
    exemptions = physical.get("joint_dispositions", [])
    exempt_ids=set()
    for d in exemptions:
        if d.get("joint_id") not in indexes["joints"] or not d.get("reason"): bad("joint_disposition", "Invalid free/continuous joint declaration")
        if d.get('joint_id') in exempt_ids: bad('joint_disposition','Duplicate joint disposition',[d.get('joint_id')])
        exempt_ids.add(d.get('joint_id'))
        free_joints.discard(d.get("joint_id"))
    if free_joints: bad("joint_connection", "Provide connections or free/continuous-joint reasons", free_joints, "needs_input")
    errors.extend(joint_disposition_findings(package,verified_bonds=verified_bonds))
    errors.extend(connection_component_findings(package))
    errors.extend(fastener_quantity_findings(package))
    errors.extend(duplicate_connection_part_findings(package))
    coverage = physical.get("coverage", {})
    for key in COVERAGE:
        item = coverage.get(key, {})
        if not isinstance(item, dict) or item.get("complete") is not True or not item.get("basis"):
            bad("coverage_"+key, "Declare and substantiate completeness of "+key, severity="needs_input")
    if not indexes["members"] and not indexes["panels"]: bad("empty_model", "No physical members or panels", severity="needs_input")
    reference=package.get('reference_system',{})
    levels=[row['level'] for row in reference.get('levels',[])]
    if len(levels)!=len(set(levels)):bad('reference_levels','Duplicate physical reference level IDs')
    grid_ids=[row['id'] for row in reference.get('grids',[])]
    if len(grid_ids)!=len(set(grid_ids)):bad('reference_grids','Duplicate physical reference grid IDs')
    if grid_ids and not {'U','V'}.issubset({row['direction'] for row in reference['grids']}):
        bad('reference_grids','An IFC reference grid needs both U and V axis families')
    for grid in reference.get('grids',[]):
        if all(norm([a-b for a,b in zip(p,grid['points_in'][0])])<1e-8 for p in grid['points_in'][1:]):
            bad('reference_grids','Grid polyline has zero length',[grid['id']])
    for item in package.get("review", {}).get("items", []):
        if item["status"] != "resolved":
            bad("coordination_required", item["title"], item["entity_ids"] or [item["id"]], "needs_input")
    for supplied in package.get("source_findings", []):
        errors.append(supplied)
    errors.extend(architectural_input_findings(package))
    for finding in physical_screens(physical):
        ids=[str(value) for row in finding['examples'] for key,value in row.items() if key.endswith('_id')]
        bad(finding['code'],finding['title']+'; resolve the physical declaration before claiming readiness',ids,'needs_input')
    combined={}
    for finding in errors:
        key=(finding["code"],finding["message"],finding["severity"])
        if key not in combined: combined[key]=dict(finding,entity_ids=[])
        combined[key]["entity_ids"].extend(finding["entity_ids"])
    return [dict(f,entity_ids=sorted(set(f["entity_ids"]))) for f in combined.values()]
