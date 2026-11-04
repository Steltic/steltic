"""Explicit physical layouts, independent of analysis idealizations and solvers."""
import copy
import math


def _length(axis):
    if len(axis)!=2 or any(len(p)!=3 for p in axis):raise ValueError('Axis needs two XYZ points')
    delta=[float(b)-float(a) for a,b in zip(*axis)]
    length=math.sqrt(sum(v*v for v in delta))
    if not math.isfinite(length) or length<=0:raise ValueError('Axis length must be positive')
    return length,delta


def split_member(model,member,stations):
    """Cut selected physical pieces at supplied inch stations; preserve intervals.

    This is an authoring operation before adding the parent member. Splice
    connection selections are separate records, not inferred by a cut station.
    """
    length,direction=_length(member['axis'])
    stations=list(stations)
    if any(type(s) not in (float,int) or not math.isfinite(s) for s in stations):raise ValueError('Finite splice stations required')
    if stations!=sorted(set(stations)) or any(s<=0 or s>=length for s in stations):raise ValueError('Splice stations must increase inside the member')
    cuts=[0,*stations,length];ids=[]
    existing=member.get('analysis_segments',[])
    claims=[dict(analysis_id=a,start=0.,end=1.) for a in member.get('analysis_ids',[])]+existing
    if len(claims)>1:raise ValueError('Split a member with one analysis interval at a time; supply explicit mapping for a chained physical member')
    for i,(a,b) in enumerate(zip(cuts,cuts[1:]),1):
        part=copy.deepcopy(member);part['id']=member['id']+':piece:'+str(i)
        part['axis']=[[member['axis'][0][k]+s/length*direction[k] for k in range(3)] for s in (a,b)]
        off=member.get('end_offsets',[[0,0,0],[0,0,0]])
        part['end_offsets']=[off[0] if i==1 else [0,0,0],off[1] if i==len(cuts)-1 else [0,0,0]]
        part['analysis_ids']=[];part['analysis_segments']=[]
        for claim in claims:
            start,end=claim['start'],claim['end']
            part['analysis_segments'].append(dict(analysis_id=claim['analysis_id'],start=start+(end-start)*a/length,end=start+(end-start)*b/length))
        part['physical_piece_of']=member['id'];part['cut_stations_in']=[a,b]
        ids.append(model.record('members',part))
    return ids


def repeat_system(model,prefix,members,stations,direction,*,source=None):
    """Place every selected member at every explicitly supplied frame station."""
    if len(direction)!=3 or abs(sum(v*v for v in direction)-1)>1e-6:raise ValueError('Unit repetition direction required')
    if len(stations)!=len(set(stations)) or any(type(s) not in (int,float) or not math.isfinite(s) for s in stations):raise ValueError('Unique finite frame stations required')
    result=[]
    for i,station in enumerate(stations,1):
        for member in members:
            row=copy.deepcopy(member);row['id']=prefix+str(i)+':'+member['id']
            row['axis']=[[p[k]+station*direction[k] for k in range(3)] for p in row['axis']]
            # Analysis mapping is occurrence-specific. A seed's analysis IDs do
            # not cover repeated frames that the analysis did not explicitly model.
            if row.get('analysis_ids') or row.get('analysis_segments'):raise ValueError('Provide occurrence analysis mapping after repetition')
            result.append(model.record('members',row,source=source))
    return result


def wall(model,prefix,baseline,height,stud_stations,*,stud,track,normal,source=None):
    """Record a supplied straight wall layout: actual studs and two tracks.

    Stations are explicit inches, including the selected end/chord studs. No
    automatic spacing, chord-pack selection, opening framing or capacity design.
    """
    length,delta=_length(baseline)
    if abs(delta[2])>1e-8 or not math.isfinite(height) or height<=0:raise ValueError('Horizontal wall baseline and positive height required')
    if len(normal)!=3 or abs(sum(v*v for v in normal)-1)>1e-6 or abs(normal[2])>1e-8 or abs(sum(a*b for a,b in zip(delta,normal)))>1e-6:raise ValueError('Wall normal must be horizontal and perpendicular to baseline')
    if len(stud_stations)!=len(set(stud_stations)) or any(type(s) not in (int,float) or not math.isfinite(s) or not 0<=s<=length for s in stud_stations):raise ValueError('Unique stud stations must lie within the wall')
    ids=[]
    for i,s in enumerate(stud_stations,1):
        p=[baseline[0][k]+s/length*delta[k] for k in range(3)]
        ids.append(model.member(prefix+':stud:'+str(i),'stud',[p,[p[0],p[1],p[2]+height]],local_y=[normal[1],-normal[0],0],source=source,**stud))
    for i,z in enumerate((0,height)):
        axis=[[p[0],p[1],p[2]+z] for p in baseline]
        ids.append(model.member(prefix+':track:'+str(i),'track',axis,local_y=[0,0,1 if i==0 else -1],source=source,**track))
    return ids


def reservation(model,id,vertices,*,host_id=None,request_id=None,responsible_party=None,source=None,**data):
    """Keep an architectural opening at its supplied location; never invent a host."""
    if host_id:return model.opening(id,host_id,vertices,source=source,**data)
    if not request_id or not responsible_party:raise ValueError('Unhosted reservation requires its information request and responsible party')
    return model.record('interfaces',dict(id=id,scope='delegated',responsible_party=responsible_party,
        representation='Unhosted architectural reservation; coordinates retained; not a cut void',
        vertices=vertices,request_id=request_id,detail_ref=dict(id=request_id,revision='1'),**data),source=source)
