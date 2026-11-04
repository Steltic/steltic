"""Explicit physical pieces spanning a chain of analysis members.

Selections and splice stations are inputs. This utility never selects sections,
changes capacities or treats an analysis node as a required physical joint.
"""
import copy
import math


def joined_piece(id,left,right):
    """Join two explicitly selected equal-section pieces at a continuous node."""
    for field in ('profile_id','material_id'):
        if left.get(field)!=right.get(field):raise ValueError('A continuous piece needs the same selected section and material')
    common=[(i,j) for i,a in enumerate(left['axis']) for j,b in enumerate(right['axis']) if math.dist(a,b)<1e-6]
    if len(common)!=1:raise ValueError('Pieces must share exactly one reference endpoint')
    i,j=common[0];joint=left['axis'][i];a=left['axis'][1-i];b=right['axis'][1-j]
    u=[v-q for v,q in zip(a,joint)];v=[v-q for v,q in zip(b,joint)]
    if sum(x*y for x,y in zip(u,v))>=0 or abs(sum(x*y for x,y in zip(u,v))+math.dist(a,joint)*math.dist(b,joint))>1e-5:
        raise ValueError('Continuous pieces must be collinear and oppose each other')
    lo=left.get('end_offsets',[[0,0,0],[0,0,0]]);ro=right.get('end_offsets',[[0,0,0],[0,0,0]])
    if math.dist(lo[i],ro[j])>1e-6:raise ValueError('Physical end offsets leave a gap at the continuous node')
    piece=copy.deepcopy(left);piece.update(id=id,axis=[copy.deepcopy(a),copy.deepcopy(b)],end_offsets=[lo[1-i],ro[1-j]],analysis_ids=[],analysis_segments=[])
    for m in (left,right):
        piece['analysis_segments'].extend(dict(analysis_id=a,start=0.,end=1.) for a in m.get('analysis_ids',[]))
        piece['analysis_segments'].extend(copy.deepcopy(m.get('analysis_segments',[])))
    piece['design_record_ids']=list(dict.fromkeys(left.get('design_record_ids',[])+right.get('design_record_ids',[])))
    piece['source_member_ids']=list(dict.fromkeys(left.get('source_member_ids',[left['id']])+right.get('source_member_ids',[right['id']])))
    piece['physical_selection_from']=left['id']
    return piece


def stacked_pieces(prefix,segments,intervals):
    """Return vertical pieces for explicit (bottom, top, selected member) intervals.

    Every input segment must identify its analysis element or interval. The
    returned pieces partition that mapping exactly, including a splice located
    between analysis levels. The selected member supplies the physical profile,
    material, orientation and checked record references for each real piece.
    """
    if not segments or not intervals:raise ValueError('A stack needs analysis segments and explicit physical selections')
    segments=sorted(segments,key=lambda m:m['axis'][0][2])
    xy=segments[0]['axis'][0][:2];start=segments[0]['axis'][0][2];end=segments[-1]['axis'][1][2]
    prior=start
    for m in segments:
        a,b=m['axis']
        if any(abs(p[k]-xy[k])>1e-6 for p in (a,b) for k in (0,1)) or b[2]<=a[2] or abs(a[2]-prior)>1e-6:
            raise ValueError('Stack segments must form a continuous upward vertical chain')
        claims=m.get('analysis_ids',[]);partial=m.get('analysis_segments',[])
        if len(claims)+len(partial)!=1:raise ValueError('Each stack segment needs one explicit analysis element interval')
        prior=b[2]
    prior=start;pieces=[]
    for i,(lo,hi,selected) in enumerate(intervals,1):
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (lo,hi)) or hi<=lo or abs(lo-prior)>1e-6 or hi>end+1e-6:
            raise ValueError('Physical intervals must continuously partition the complete stack')
        part=copy.deepcopy(selected);part.update(id=prefix+':piece:'+str(i),axis=[[*xy,lo],[*xy,hi]],
            analysis_ids=[],analysis_segments=[],end_offsets=[[0,0,0],[0,0,0]],
            physical_selection_from=selected['id'],source_member_ids=[])
        part['selected_design_record_ids']=copy.deepcopy(selected.get('design_record_ids',[]))
        part['analysis_design_evidence']=[]
        part.setdefault('design_record_ids',[])
        for m in segments:
            a,b=m['axis'];lower=max(lo,a[2]);upper=min(hi,b[2])
            if upper-lower<=1e-8:continue
            claim=copy.deepcopy(m.get('analysis_segments',[{}])[0]) if m.get('analysis_segments') else dict(analysis_id=m['analysis_ids'][0],start=0.,end=1.)
            f0,f1=claim['start'],claim['end'];length=b[2]-a[2]
            part['analysis_segments'].append(dict(analysis_id=claim['analysis_id'],start=f0+(f1-f0)*(lower-a[2])/length,end=f0+(f1-f0)*(upper-a[2])/length))
            part['source_member_ids'].append(m['id'])
            part['analysis_design_evidence'].append(dict(source_member_id=m['id'],profile_id=m['profile_id'],
                design_record_ids=copy.deepcopy(m.get('design_record_ids',[])),source_forces=copy.deepcopy(m.get('source_forces',{})),
                analysis_segment=copy.deepcopy(part['analysis_segments'][-1]),
                basis='Original analysis-segment design evidence; physical selection is separately identified'))
            if m['profile_id']==part['profile_id']:
                part['design_record_ids']=list(dict.fromkeys(part['design_record_ids']+m.get('design_record_ids',[])))
        if i==1:part['end_offsets'][0]=copy.deepcopy(segments[0].get('end_offsets',[[0,0,0],[0,0,0]])[0])
        if i==len(intervals):part['end_offsets'][1]=copy.deepcopy(segments[-1].get('end_offsets',[[0,0,0],[0,0,0]])[1])
        part.pop('joint_workpoints',None)
        if any(m.get('joint_workpoints') for m in segments):
            part['joint_workpoints']=copy.deepcopy(part['axis'])
            if i==1:part['joint_workpoints'][0]=copy.deepcopy(segments[0].get('joint_workpoints',segments[0]['axis'])[0])
            if i==len(intervals):part['joint_workpoints'][1]=copy.deepcopy(segments[-1].get('joint_workpoints',segments[-1]['axis'])[1])
        pieces.append(part);prior=hi
    if abs(prior-end)>1e-6:raise ValueError('Physical intervals leave part of the stack unrepresented')
    return pieces
