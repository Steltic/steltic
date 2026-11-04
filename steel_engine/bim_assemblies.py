"""Versioned physical assembly templates with explicit rigid occurrence frames.

Templates contain local parts; occurrences supply a real joint transform and
member bindings. No template is automatically placed, sized or designed here.
Shared by both producers and the openBIM consumer.
"""
import copy
import hashlib
import json
import math


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def cross(a,b):return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
def dot(a,b):return sum(x*y for x,y in zip(a,b))
def finite(v):return isinstance(v,list) and len(v)==3 and all(type(x) in (int,float) and math.isfinite(x) for x in v)


def frame_errors(frame):
    if not isinstance(frame,dict) or not finite(frame.get('origin')):return ['Assembly origin needs three finite coordinates']
    x,y=frame.get('x_axis'),frame.get('y_axis')
    if not finite(x) or not finite(y) or abs(dot(x,x)-1)>1e-6 or abs(dot(y,y)-1)>1e-6 or abs(dot(x,y))>1e-6:
        return ['Assembly axes must define an orthonormal right-handed frame']
    return []


def transformed(point,frame,direction=False):
    x,y=frame['x_axis'],frame['y_axis'];z=cross(x,y)
    return [(0 if direction else frame['origin'][k])+sum(point[i]*v[k] for i,v in enumerate((x,y,z))) for k in range(3)]


def transform_geometry(geometry,frame):
    g=copy.deepcopy(geometry)
    for k in ('origin',):
        if k in g:g[k]=transformed(g[k],frame)
    for k in ('axis','x_axis','y_axis','local_y'):
        if k in g:g[k]=transformed(g[k],frame,True)
    if 'points' in g:g['points']=[transformed(p,frame) for p in g['points']]
    if 'faces' in g:g['faces']=[[transformed(p,frame) for p in face] for face in g['faces']]
    return g


def index(physical):return {row['id']:row for row in physical.get('assemblies',[])}


def parts(connection,templates,*,world=False):
    """Return explicit parts plus the bound template, without expanding the file.

    Validation of quantities and local geometry can use local coordinates. Use
    world=True for spatial checks and rendering. Instance transforms are checked
    separately and are never silently normalised.
    """
    result=list(connection.get('parts',[]));instance=connection.get('assembly_instance')
    if not instance:return result
    template=templates.get(instance.get('template_id'))
    if template is None:return result
    if not world:return result+template.get('parts',[])
    if frame_errors(instance):return result
    for part in template.get('parts',[]):
        p=dict(part,id=connection['id']+':'+part['id'])
        p['geometry']=transform_geometry(part['geometry'],instance)
        p['template_part_id']=part['id'];result.append(p)
    return result


def has_physical_connection(connection,templates,*,verified_bonds=()):
    """Solid hardware or an explicit weld joining two real receiver members.

    A direct welded joint need not invent a plate/bolt solid. A text callout,
    continuity note or weld to an unbound local label cannot satisfy this check.
    Geometry, receiver placement and selected categories are validated elsewhere.
    """
    if connection.get('id') in verified_bonds:return True
    components=parts(connection,templates)
    if any(p.get('geometry',{}).get('type')!='polyline' for p in components):return True
    placement=connection.get('assembly_instance',{});template=templates.get(placement.get('template_id'),{})
    receivers={r['id'] for r in template.get('receivers',[]) if r.get('kind')=='member'}
    bindings=placement.get('bindings',{})
    for part in components:
        if part.get('kind')!='weld':continue
        points=part.get('geometry',{}).get('points',[])
        if len(points)<2 or not all(finite(p) for p in points) or math.dist(points[0],points[-1])<1e-8:continue
        joined={bindings[r] for r in part.get('connects',[]) if r in receivers and r in bindings}
        if len(joined)>=2:return True
    return False


def add_template(model,id,parts,*,source_record_ids=(),receivers=(),bond_interfaces=(),basis):
    rows=model.value['declarations'].setdefault('assemblies',[])
    if any(row['id']==id for row in rows):raise ValueError('Duplicate assembly template ID')
    value=dict(id=id,parts=copy.deepcopy(parts),source_record_ids=list(source_record_ids),receivers=copy.deepcopy(list(receivers)),basis=basis)
    if bond_interfaces:value['bond_interfaces']=copy.deepcopy(list(bond_interfaces))
    value['revision']=fingerprint(value)
    rows.append(value);return value['revision']


def instance(model,id,joint_id,template_id,origin,x_axis,y_axis,*,bindings,design_record_ids,scope='designed',**data):
    templates=index(model.value['declarations']);template=templates.get(template_id)
    if template is None:raise ValueError('Select an existing assembly template')
    placement=dict(template_id=template_id,revision=template['revision'],origin=origin,x_axis=x_axis,y_axis=y_axis,bindings=bindings)
    errors=frame_errors(placement)
    if errors:raise ValueError(errors[0])
    return model.record('connections',dict(id=id,joint_id=joint_id,scope=scope,design_record_ids=list(design_record_ids),parts=[],assembly_instance=placement,**data))


def findings(package):
    try:from .bim_contract import issue,geometry_errors
    except ImportError:from bim_contract import issue,geometry_errors
    try:from . import bim_bonds
    except ImportError:import bim_bonds
    physical=package['physical'];templates=index(physical);result=[]
    profiles={p['id']:p for p in physical['profiles']};materials={p['id'] for p in physical['materials']}
    records={r['id'] for r in package['design_records']}
    entities={(kind,r['id']):r for kind,key in (('member','members'),('panel','panels'),('interface','interfaces')) for r in physical[key]}
    joints={r['id']:r for r in physical['joints']}
    def bad(code,message,*ids):result.append(issue(code,message,ids,'error'))
    if len(templates)!=len(physical.get('assemblies',[])):bad('assembly_template','Duplicate assembly template IDs')
    for tid,template in templates.items():
        content={k:v for k,v in template.items() if k!='revision'}
        if template.get('revision')!=fingerprint(content):bad('assembly_revision','Assembly template content differs from its revision',tid)
        if not template.get('basis') or not (template.get('parts') or template.get('bond_interfaces')):bad('assembly_template','An assembly needs physical parts or a validated reinforcement bond, and its basis',tid)
        for message in bim_bonds.template_errors(template):bad('reinforcement_bond',message,tid)
        if any(r not in records for r in template.get('source_record_ids',[])):bad('assembly_design','Assembly references unknown selected design records',tid)
        seen=set()
        for part in template.get('parts',[]):
            if not part.get('id') or part['id'] in seen:bad('assembly_part','Invalid or duplicate local part ID',tid)
            seen.add(part.get('id'))
            if not part.get('kind') or part.get('material_id') not in materials:bad('assembly_part','Local part kind/material missing',tid,str(part.get('id')))
            for error in geometry_errors(part.get('geometry'),profiles):bad('assembly_geometry',error,tid,str(part.get('id')))
        receiver_ids=set()
        for receiver in template.get('receivers',[]):
            rid=receiver.get('id')
            if not rid or rid in receiver_ids:bad('assembly_receiver','Receiver IDs must be unique',tid)
            receiver_ids.add(rid)
            if receiver.get('kind')=='member':
                axis=receiver.get('axis',[])
                if len(axis)!=2 or not all(finite(p) for p in axis) or axis[0]==axis[1]:bad('assembly_receiver','Member receiver needs a nonzero local axis',tid,str(rid))
                if 'local_y' in receiver and (not finite(receiver['local_y']) or abs(dot(receiver['local_y'],receiver['local_y'])-1)>1e-6):bad('assembly_receiver','Receiver local_y must be a unit vector',tid,str(rid))
            elif receiver.get('kind') not in ('panel','interface'):bad('assembly_receiver','Unknown assembly receiver kind',tid,str(rid))
        if not receiver_ids:bad('assembly_receiver','Bind the actual receiving physical entities, not a representative detail',tid)
    for c in physical['connections']:
        frame=c.get('assembly_instance')
        if not frame:continue
        cid=c['id'];template=templates.get(frame.get('template_id'))
        if template is None:bad('assembly_instance','Unknown assembly template',cid);continue
        if c.get('parts'):bad('assembly_instance','Use one placed template or explicit global parts; do not mix coordinate systems',cid)
        if frame.get('revision')!=template.get('revision'):bad('assembly_revision','Occurrence refers to a different template revision',cid)
        errors=frame_errors(frame)
        if errors:
            for error in errors:bad('assembly_frame',error,cid)
            continue
        joint=joints.get(c.get('joint_id'))
        if joint and finite(joint.get('point')) and math.dist(frame['origin'],joint['point'])>1e-5:bad('assembly_joint','Assembly frame origin must equal its actual joint point',cid)
        if not set(template.get('source_record_ids',[]))<=set(c.get('design_record_ids',[])):bad('assembly_design','Occurrence must carry its template selected-design references',cid)
        bindings=frame.get('bindings',{})
        expected={r.get('id') for r in template.get('receivers',[])}
        if not isinstance(bindings,dict) or set(bindings)!=expected:bad('assembly_binding','Bind each local receiver exactly once to this occurrence',cid);continue
        for receiver in template.get('receivers',[]):
            rid=receiver['id'];eid=bindings[rid];entity=entities.get((receiver.get('kind'),eid))
            if entity is None:bad('assembly_binding','Assembly receiver references an unknown physical entity',cid,str(eid));continue
            if receiver.get('kind')!='member':continue
            actual=entity.get('axis',[]);local=receiver.get('axis',[])
            offsets=entity.get('end_offsets',[[0,0,0],[0,0,0]])
            if len(actual)==2 and len(offsets)==2 and all(finite(v) for v in actual+offsets):
                actual=[[a+b for a,b in zip(p,o)] for p,o in zip(actual,offsets)]
            if len(actual)!=2 or not all(finite(p) for p in actual) or actual[0]==actual[1] or len(local)!=2 or not all(finite(p) for p in local):continue
            axis=[transformed(p,frame) for p in local];d=[b-a for a,b in zip(*actual)];length=math.sqrt(dot(d,d));d=[v/length for v in d]
            def off_line(p):
                v=[a-b for a,b in zip(p,actual[0])];t=dot(v,d)
                return math.sqrt(sum((v[k]-t*d[k])**2 for k in range(3)))
            if max(off_line(p) for p in axis)>.01:bad('assembly_receiver_fit','Placed receiver axis does not match the bound physical member',cid,eid)
            stations=[dot([a-b for a,b in zip(p,actual[0])],d) for p in axis]
            if min(stations)<-.01 or max(stations)>length+.01:
                bad('assembly_receiver_extent','Placed receiver extends beyond its actual bound member',cid,eid)
            if finite(receiver.get('local_y')):
                expected_y=transformed(receiver['local_y'],frame,True)
                actual_y=entity.get('local_y',[])
                if not finite(actual_y) or math.dist(expected_y,actual_y)>1e-5:
                    bad('assembly_receiver_roll','Placed receiver roll differs from its actual bound member',cid,eid)
            if receiver.get('profile_id') and receiver['profile_id']!=entity.get('profile_id'):bad('assembly_receiver_profile','Placed receiver profile differs from its bound physical member',cid,eid)
    return result
