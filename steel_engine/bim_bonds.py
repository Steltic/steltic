"""Physical fit of declared straight reinforcement bonds in horizontal concrete.

No development length, cover requirement, lap class or structural capacity is
calculated here. A valid result proves the declared bars and concrete host exist
and fit the supplied geometry; it cannot approve an engineering design.
"""
import math

KINDS={'reinforcement_bond_development','reinforcement_bond_lap'}
BAR_KINDS={'reinforcement_bar','reinforcing_bar','diaphragm_collector_bar','diaphragm_chord_bar'}
TOL=.01


def positive(value):
    return type(value) in (int,float) and math.isfinite(value) and value>0


def template_errors(template):
    bonds=template.get('bond_interfaces',[])
    if not isinstance(bonds,list):return ['Bond interfaces must be a list']
    receivers={r.get('id'):r for r in template.get('receivers',[]) if isinstance(r,dict)}
    errors=[];ids=set()
    for bond in bonds:
        if not isinstance(bond,dict):errors.append('Invalid reinforcement bond declaration');continue
        kind=bond.get('kind');bars=bond.get('bar_receivers');host=bond.get('host_receiver')
        if not isinstance(bond.get('id'),str) or not bond['id'] or bond['id'] in ids:
            errors.append('Reinforcement bond IDs must be unique and nonempty')
        ids.add(str(bond.get('id')))
        if kind not in KINDS:errors.append('Unsupported reinforcement bond kind')
        expected=2 if kind=='reinforcement_bond_lap' else 1
        if (not isinstance(bars,list) or len(bars)!=expected or any(not isinstance(r,str) for r in bars) or
            len(set(bars))!=len(bars) or any(receivers.get(r,{}).get('kind')!='member' for r in bars)):
            errors.append('Bond must identify its actual reinforcing-bar receivers')
        if not isinstance(host,str) or receivers.get(host,{}).get('kind')!='panel':
            errors.append('Bond must bind its actual concrete panel receiver')
        if kind=='reinforcement_bond_lap' and not positive(bond.get('length_in')):
            errors.append('Contact lap needs a positive declared overlap length')
        if not isinstance(bond.get('basis'),str) or not bond['basis'] or bond.get('engineering_status')!='not_verified':
            errors.append('Bond must state its basis and unverified engineering status')
    return errors


def reference_lines(template):
    """Local bar centrelines at a declared bond, never fictitious hardware."""
    from . import bim_assemblies as A
    receivers={r['id']:r for r in template.get('receivers',[])}
    lines=[]
    for bond in template.get('bond_interfaces',[]):
        axes=[receivers[r]['axis'] for r in bond['bar_receivers']]
        if bond['kind']=='reinforcement_bond_development':
            lines.append(axes[0]);continue
        aa,bb=axes;length=math.dist(*aa)
        u=[(b-a)/length for a,b in zip(*aa)]
        stations=[A.dot([q[i]-aa[0][i] for i in range(3)],u) for q in bb]
        lo=max(0,min(stations));hi=min(length,max(stations))
        for axis,start in ((aa,0),(bb,stations[0])):
            lines.append([[axis[0][i]+(t-start)*u[i] for i in range(3)] for t in (lo,hi)])
    return lines


def _inside(q,ring):
    inside=False
    for a,b in zip(ring,ring[1:]+ring[:1]):
        if (a[1]>q[1])!=(b[1]>q[1]) and q[0]<(b[0]-a[0])*(q[1]-a[1])/(b[1]-a[1])+a[0]:inside=not inside
    return inside


def _distance(q,a,b):
    v=[b[i]-a[i] for i in (0,1)];length=sum(t*t for t in v)
    t=max(0,min(1,sum((q[i]-a[i])*v[i] for i in (0,1))/length)) if length else 0
    return math.hypot(*(q[i]-a[i]-t*v[i] for i in (0,1)))


def _segment_distance(a,b,c,d):
    def turn(p,q,r):return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    values=(turn(a,b,c),turn(a,b,d),turn(c,d,a),turn(c,d,b))
    if values[0]*values[1]<0 and values[2]*values[3]<0:return 0
    return min(_distance(p,x,y) for p,x,y in ((a,c,d),(b,c,d),(c,a,b),(d,a,b)))


def _clear(axis,radius,ring,hole=False):
    expected=not hole
    if any(_inside(q,ring)!=expected for q in axis):return False
    return all(_segment_distance(*axis,a,b)>=radius-TOL for a,b in zip(ring,ring[1:]+ring[:1]))


def evaluate(physical):
    """Return (errors, verified connection IDs); never use a client-ready flag."""
    try:from . import bim_assemblies as A
    except ImportError:import bim_assemblies as A
    templates=A.index(physical)
    connections=[c for c in physical.get('connections',[]) if templates.get(c.get('assembly_instance',{}).get('template_id'),{}).get('bond_interfaces')]
    if not connections:return [],set()
    members={m['id']:m for m in physical.get('members',[])}
    panels={p['id']:p for p in physical.get('panels',[])};profiles={p['id']:p for p in physical.get('profiles',[])}
    materials={m['id']:m for m in physical.get('materials',[])}
    openings={o['id']:o for o in physical.get('openings',[])}
    for interface in physical.get('interfaces',[]):
        if interface.get('kind')=='opening_reservation' and isinstance(interface.get('source_opening'),dict):
            opening=dict(interface['source_opening']);opening['vertices']=interface.get('vertices',[])
            openings.setdefault(opening['id'],opening)
    errors=[];valid=set()
    def bad(cid,message):errors.append((cid,message))
    for connection in connections:
        cid=connection['id'];frame=connection.get('assembly_instance',{});template=templates.get(frame.get('template_id'),{})
        if not template.get('bond_interfaces'):continue
        initial=len(errors)
        for message in template_errors(template):bad(cid,message)
        if len(errors)!=initial:continue
        if A.frame_errors(frame):bad(cid,'Reinforcement bond frame is invalid');continue
        bindings=frame.get('bindings',{});receivers={r['id']:r for r in template['receivers']}
        if not isinstance(bindings,dict):bad(cid,'Reinforcement bond bindings are invalid');continue
        for bond in template['bond_interfaces']:
            host=panels.get(bindings.get(bond['host_receiver']))
            if host is None:bad(cid,'Reinforcement bond has no actual concrete host');continue
            verts=host.get('vertices',[]);thickness=host.get('thickness',host.get('thickness_in'))
            material=materials.get(host.get('material_id'),{})
            if ('concrete' not in str(material.get('specification','')).lower() or host.get('thickness_direction')!='negative_global_z' or
                not positive(thickness) or len(verts)<3 or not all(A.finite(p) for p in verts) or max(p[2] for p in verts)-min(p[2] for p in verts)>TOL):
                bad(cid,'Bond requires an explicitly located horizontal concrete thickness below its top surface');continue
            top=verts[0][2];ring=[p[:2] for p in verts]
            declared=host.get('openings',[])
            if not isinstance(declared,list) or any(not isinstance(oid,str) for oid in declared):
                bad(cid,'Concrete host opening references must be a list of IDs');continue
            hole_ids=set(declared)|{o['id'] for o in openings.values() if o.get('host_id')==host['id']}
            holes=[]
            for oid in hole_ids:
                hole=openings.get(oid,{}).get('vertices',[])
                if len(hole)<3 or not all(A.finite(p) for p in hole):bad(cid,'Concrete host has an unresolved opening '+str(oid));continue
                holes.append([p[:2] for p in hole])
            actual=[]
            for rid in bond['bar_receivers']:
                member=members.get(bindings.get(rid),{});profile=profiles.get(member.get('profile_id'),{})
                diameter=profile.get('dimensions',{}).get('d');axis=member.get('axis',[])
                offsets=member.get('end_offsets',[[0,0,0],[0,0,0]])
                local=receivers[rid].get('axis',[])
                if (member.get('kind') not in BAR_KINDS or profile.get('family')!='round' or not positive(diameter) or
                    len(axis)!=2 or len(offsets)!=2 or len(local)!=2 or not all(A.finite(p) for p in axis+offsets+local)):
                    bad(cid,'Bond receiver must be a real straight round reinforcing bar');continue
                axis=[[a+b for a,b in zip(p,o)] for p,o in zip(axis,offsets)]
                if math.dist(*axis)<TOL or abs(axis[1][2]-axis[0][2])>TOL:
                    bad(cid,'This bond requires horizontal straight reinforcing bars');continue
                r=diameter/2
                if (min(q[2] for q in axis)-r<top-thickness-TOL or max(q[2] for q in axis)+r>top+TOL or
                    not _clear(axis,r,ring) or any(not _clear(axis,r,hole,True) for hole in holes)):
                    bad(cid,'Reinforcing bar leaves the concrete host or crosses an opening')
                placed=[A.transformed(q,frame) for q in local]
                u=[(b-a)/math.dist(*axis) for a,b in zip(*axis)]
                stations=[A.dot([q[i]-axis[0][i] for i in range(3)],u) for q in placed]
                if (any(math.dist(q,[axis[0][i]+t*u[i] for i in range(3)])>TOL for q,t in zip(placed,stations)) or
                    min(stations)<-TOL or max(stations)>math.dist(*axis)+TOL):
                    bad(cid,'Bond receiver extract does not lie on the actual bar')
                if bond['kind']=='reinforcement_bond_development' and (abs(min(stations))>TOL or abs(max(stations)-math.dist(*axis))>TOL):
                    bad(cid,'A whole-bar bond disposition must include both actual physical ends')
                actual.append((placed,diameter))
            if bond['kind']=='reinforcement_bond_lap' and len(actual)==2:
                (aa,da),(bb,db)=actual;la=math.dist(*aa);lb=math.dist(*bb)
                if min(la,lb)<TOL:bad(cid,'Contact lap has a zero-length receiver');continue
                u=[(b-a)/la for a,b in zip(*aa)];v=[(b-a)/lb for a,b in zip(*bb)]
                if abs(A.dot(u,v))<1-1e-8:bad(cid,'Contact-lap reinforcing bars must be parallel');continue
                station=[A.dot([q[i]-aa[0][i] for i in range(3)],u) for q in bb]
                distance=math.dist(bb[0],[aa[0][i]+station[0]*u[i] for i in range(3)])
                overlap=min(la,max(station))-max(0,min(station))
                if abs(distance-(da+db)/2)>TOL or overlap<=0 or abs(overlap-bond['length_in'])>TOL:
                    bad(cid,'Declared contact-lap length or bar-to-bar contact differs from actual geometry')
        if len(errors)==initial:valid.add(cid)
    return errors,valid
