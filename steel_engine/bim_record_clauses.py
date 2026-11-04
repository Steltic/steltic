"""Exact, exhaustive applications of explicitly labelled source clauses.

A schedule may specify different hardware at the base and upper floors. The
original record remains immutable. An occurrence can select an exact clause,
provided every clause has a located application and each selection is valid.
Unlabelled or partially parsed records cannot use this mechanism.
"""
import re


def clauses(text):
    if not isinstance(text,str):return {}
    hits=list(re.finditer(r'(?:^|;)\s*(base|floors):\s*',text,re.I))
    if len(hits)!=2 or hits[0].start()!=0 or {h[1].lower() for h in hits}!={'base','floors'}:return {}
    return {hit[1].lower():text[hit.end():hits[i+1].start() if i+1<len(hits) else len(text)].strip()
            for i,hit in enumerate(hits)}


def applications(package):
    records={r['id']:r for r in package['design_records']};chosen={};used={};errors=[]
    members={m['id']:m for m in package['physical']['members']}
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
            # These supported source labels concern wall sill anchorage, not
            # rods, unrelated beams or a generic representative connection.
            tracks=[members[mid] for mid in c.get('member_ids',[]) if mid in members and members[mid]['kind'] in ('bottom_track','ordinary_track','tier_track')]
            level=entry.get('level')
            if level is None or not any(m.get('level')==level for m in tracks) or (label=='base')!=(level==1):
                errors.append((c['id'],str(rid),'Base/floors clause needs its actual receiving bottom track and matching storey'));continue
            chosen[key]=text;used.setdefault((rid,field),set()).add(label)
    for (rid,field),labels in used.items():
        missing=set(clauses(records[rid]['data'][field]))-labels
        if missing:errors.append((rid,field,'No located application for source clauses: '+', '.join(sorted(missing))))
    return chosen,errors
