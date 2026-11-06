"""Exact, exhaustive applications of explicitly labelled source clauses.

A schedule may specify different hardware at the base, upper floors or
exterior-column/interior-HSS bases. These labels also exist in the package
schema; the located member proof below remains mandatory. The
original record remains immutable. An occurrence can select an exact clause,
provided every clause has a located application and each selection is valid.
Unlabelled or partially parsed records cannot use this mechanism.
"""
import math,re


def clauses(text):
    if not isinstance(text,str):return {}
    hits=list(re.finditer(r'(?:^|;)\s*(base|floors|top|exterior bases|interior hss[\d.x/]+ columns):\s*',text,re.I))
    if len(hits)!=2 or hits[0].start()!=0:return {}
    labels={h[1].lower() for h in hits}
    portal='exterior bases' in labels and any(re.fullmatch(r'interior hss[\d.x/]+ columns',label) for label in labels)
    if labels not in ({'base','floors'},{'base','top'}) and not portal:return {}
    return {hit[1].lower():text[hit.end():hits[i+1].start() if i+1<len(hits) else len(text)].strip()
            for i,hit in enumerate(hits)}


def applications(package):
    records={r['id']:r for r in package['design_records']};chosen={};used={};errors=[]
    members={m['id']:m for m in package['physical']['members']}
    joints={j['id']:j for j in package['physical'].get('joints',[])}
    def point(value):
        return isinstance(value,(list,tuple)) and len(value)==3 and all(type(v) in (int,float) and math.isfinite(v) for v in value)
    for c in package['physical']['connections']:
        seen=set()
        entries=c.get('source_record_selections',[])
        if not isinstance(entries,list):
            errors.append((c['id'],'','Source record selections must be a list'));continue
        for entry in entries:
            if not isinstance(entry,dict) or not all(isinstance(entry.get(k),str) for k in ('record_id','field','clause','text','basis')) or type(entry.get('level')) is not int:
                errors.append((c['id'],'','Source clause selection has invalid fields'));continue
            rid=entry.get('record_id');field=entry.get('field');label=entry.get('clause');text=entry.get('text');key=(c['id'],rid)
            source=records.get(rid,{}).get('data',{});options=clauses(source.get(field)) if field in ('selection','components') else {}
            expected=options.get(label)
            if rid in seen or rid not in c.get('design_record_ids',[]) or not expected or text!=expected or not entry.get('basis'):
                errors.append((c['id'],str(rid),'Select an exact labelled clause from the linked immutable source record, with its applicability basis'));continue
            seen.add(rid)
            level=entry.get('level')
            if 'floors' in options:
                tracks=[members[mid] for mid in c.get('member_ids',[]) if mid in members and members[mid]['kind'] in ('bottom_track','ordinary_track','tier_track')]
                if not any(m.get('level')==level for m in tracks) or (label=='base')!=(level==1):
                    errors.append((c['id'],str(rid),'Base/floors clause needs its actual receiving bottom track and matching storey'));continue
            elif 'exterior bases' in options:
                interior=next(v for v in options if v!='exterior bases')
                profile=interior.removeprefix('interior ').removesuffix(' columns').upper()
                q=c.get('point',joints.get(c.get('joint_id'),{}).get('point'));located=[]
                for m in members.values():
                    axis=m.get('joint_workpoints',m.get('axis'))
                    if m.get('level')!=level or m.get('kind') not in ('column','prop'):continue
                    if not isinstance(axis,list) or len(axis)!=2 or not all(point(v) for v in axis):continue
                    if any(abs(axis[1][i]-axis[0][i])>.001 for i in (0,1)) or abs(axis[1][2]-axis[0][2])<1:continue
                    located.append((m,min(axis,key=lambda v:v[2])))
                props=[(m,p) for m,p in located if m.get('kind')=='prop' and str(m.get('profile_id','')).upper()==profile]
                columns=[(m,p) for m,p in located if m.get('kind')=='column']
                matched=False
                if point(q):
                    for prop,pp in props:
                        for left,lp in columns:
                            for right,rp in columns:
                                for along in (0,1):
                                    across=1-along
                                    if not lp[along]+.001<pp[along]<rp[along]-.001:continue
                                    if any(abs(v[i]-pp[i])>.001 for v in (lp,rp) for i in (across,2)):continue
                                    targets=[(prop,pp)] if label==interior else [(left,lp),(right,rp)]
                                    if any(m['id'] in c.get('member_ids',[]) and math.dist(p,q)<.001 for m,p in targets):matched=True
                if not matched:
                    errors.append((c['id'],str(rid),'Exterior/interior base clause needs its actual base joint, source HSS prop section and two flanking frame columns'));continue
            else:
                # The source's separate post head/base clauses apply only at
                # the actual ends of a bound endwall post, not a claimed label.
                q=c.get('point',joints.get(c.get('joint_id'),{}).get('point'));matched=False
                for mid in c.get('member_ids',[]):
                    m=members.get(mid,{})
                    if m.get('kind')!='endwall_post' or m.get('level')!=level:continue
                    axis=m.get('joint_workpoints',m.get('axis'))
                    if not point(q) or not isinstance(axis,list) or len(axis)!=2 or not all(point(v) for v in axis):continue
                    if abs(axis[0][2]-axis[1][2])<1:continue
                    end=(min if label=='base' else max)(axis,key=lambda v:v[2])
                    if math.dist(q,end)<.001:matched=True;break
                if not matched:
                    errors.append((c['id'],str(rid),'Base/top clause needs its actual endwall post and matching physical joint workpoint'));continue
            chosen[key]=text;used.setdefault((rid,field),set()).add(label)
    for (rid,field),labels in used.items():
        missing=set(clauses(records[rid]['data'][field]))-labels
        if missing:errors.append((rid,field,'No located application for source clauses: '+', '.join(sorted(missing))))
    return chosen,errors
