"""Versioned physical selections and engineer decisions, independent of cfg execution.

This module records decisions; it does not choose products, infer capacities or
change engineering calculation files. Identical in HR and CFS.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

NAME = 'physical_inputs.json'
SCHEMA = 'steltic.physical_inputs'
COLLECTIONS = ('members','profiles','materials','panels','openings','joints','connections','interfaces')
STATES = ('proposed','accepted_proposal','confirmed','accepted_test','delegated')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    content=json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n'
    fd,temp=tempfile.mkstemp(prefix='.physical-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:stream.write(content)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def empty(module):
    if module not in ('HR','CFS'):raise ValueError('Physical input module must be HR or CFS')
    return dict(schema=SCHEMA,version=1,module=module,revision=1,
        units=dict(length='in',force='kip',stress='ksi'),
        declarations=dict(schema_version='1.0',required=True,require_connection_parts=True,
                          **{k:[] for k in COLLECTIONS}),
        layout={},requests=[],decisions=[],design_sources={},events=[])


def validate_input(value, module=None):
    json.dumps(value,allow_nan=False)
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('version')!=1:
        raise ValueError('Unsupported physical input schema/version')
    if value.get('module') not in ('HR','CFS') or (module and value['module']!=module):
        raise ValueError('Physical input module mismatch')
    if value.get('units')!={'length':'in','force':'kip','stress':'ksi'}:raise ValueError('Physical inputs use inches, kips and ksi')
    if type(value.get('revision')) is not int or value['revision']<1:raise ValueError('Invalid physical input revision')
    declaration=value.get('declarations')
    if not isinstance(declaration,dict) or declaration.get('schema_version')!='1.0':raise ValueError('Physical declarations require schema_version 1.0')
    for key in COLLECTIONS:
        rows=declaration.get(key,[])
        if not isinstance(rows,list) or any(not isinstance(r,dict) or not isinstance(r.get('id'),str) or not r['id'] for r in rows):
            raise ValueError('Invalid physical collection: '+key)
        if len({r['id'] for r in rows})!=len(rows):raise ValueError('Duplicate physical IDs: '+key)
    ids=set()
    for request in value.get('requests',[]):
        if not isinstance(request,dict) or not request.get('id') or request['id'] in ids:raise ValueError('Duplicate/invalid information request')
        ids.add(request['id'])
        if request.get('status') not in STATES:raise ValueError('Invalid request status')
        if not request.get('question') or not request.get('proposal'):raise ValueError('Requests need a question and explicit proposal')
        if request.get('status')=='accepted_test' and request.get('fictional') is not True:raise ValueError('Test decisions must remain explicitly fictional')
    for event in value.get('decisions',[]):
        if not event.get('request_id') or event['request_id'] not in ids or not event.get('author'):
            raise ValueError('Decisions require a known request and author')
    return value


def read(root, module=None):
    path=Path(root)/NAME
    if not path.is_file():return None
    return validate_input(json.loads(path.read_text(encoding='utf-8-sig')),module)


def design_files(root):
    """Exact design dependencies, excluding separately versioned physical inputs."""
    root=Path(root)
    try:from .bim_export import tracked_sources
    except ImportError:from bim_export import tracked_sources
    ignored={NAME,'portable_model.json','bim_inputs.json','design/bim_request.json'}
    # Never exclude an arbitrary directory: configurations may consume files in
    # it. The physical selections live in this one explicitly versioned file.
    return {n:h for n,h in tracked_sources(root).items() if n not in ignored}


def binding_findings(root, value):
    try:from .bim_contract import issue
    except ImportError:from bim_contract import issue
    files=value.get('design_sources',{})
    if not files:return [issue('physical_design_binding','Bind physical selections to the checked design sources before final completion')]
    root=Path(root).resolve();changed=[]
    for name,expected in files.items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or digest(path)!=expected:changed.append(name)
    # Added design dependencies must also invalidate a previously complete binding.
    changed.extend(sorted(set(design_files(root))-set(files)))
    result=[issue('physical_design_binding','Physical selections refer to changed or missing design inputs',changed)] if changed else []
    if value.get('pending_design_reviews'):
        result.append(issue('physical_input_design_review','Confirmed physical changes still need their affected engineering checks',
                            [r['request_id'] for r in value['pending_design_reviews']]))
    return result


def _checked_design_sources(root,value):
    stamp_path=root/'design/consistency_result.json'
    if not stamp_path.is_file():raise ValueError('A recorded structural consistency verdict is required')
    stamp=json.loads(stamp_path.read_text(encoding='utf-8-sig'))
    package=root/'design'/('calc_package.json' if value['module']=='HR' else 'calc_package_cfs.json')
    expected=stamp.get('calc_package_sha256') or stamp.get('package_sha1')
    actual=digest(package) if stamp.get('calc_package_sha256') else hashlib.sha1(package.read_bytes()).hexdigest()
    if stamp.get('result')!='PASS' or not expected or actual!=expected or (stamp.get('cfg_sha256') and stamp['cfg_sha256']!=digest(root/'cfg.py')):
        raise ValueError('Cannot bind physical inputs to a failing or stale structural consistency verdict')
    return design_files(root)


def bind_checked_design(root):
    """Call only after actual structural consistency; never manufacture a PASS."""
    from filelock import FileLock
    root=Path(root)
    with FileLock(str(root/'.physical-inputs.lock'),timeout=10):
        return _bind_checked_design(root)


def _bind_checked_design(root):
    value=read(root)
    if value is None:return None
    hashes=_checked_design_sources(root,value)
    if value.get('design_sources')!=hashes:
        value['design_sources']=hashes;value['revision']+=1
        value['events'].append(dict(action='bind_checked_design',revision=value['revision'],sources_sha256=fingerprint(hashes)))
        atomic_json(root/NAME,value)
    return value


def record_design_review(root, *, request_id, author, note, source_pointers,
                         expected_revision, expected_sha256):
    """Close a pending engineering review against explicit checked records.

    Called by the engineering workflow after it reviews the affected selection,
    not by the drawing answer UI. A general PASS alone never closes a review.
    The retained record pointers and physical revision make the sign-off auditable.
    """
    from filelock import FileLock
    root=Path(root)
    if not isinstance(author,str) or not author.strip() or not isinstance(note,str) or not note.strip():
        raise ValueError('Engineering review requires its author and decision basis')
    if not isinstance(source_pointers,list) or not source_pointers or any(not isinstance(p,str) or not p.startswith('/') for p in source_pointers):
        raise ValueError('Review requires the affected checked calculation record pointers')
    with FileLock(str(root/'.physical-inputs.lock'),timeout=10):
        value=read(root)
        if value is None or value['revision']!=expected_revision or fingerprint(value)!=expected_sha256:
            raise ValueError('Physical inputs changed; refresh before reviewing')
        pending=[p for p in value.get('pending_design_reviews',[]) if p['request_id']==request_id]
        if not pending:raise ValueError('This request has no pending engineering review')
        # This verifies the actual consistency stamp and its input hashes. It
        # does not alter the calculation or its verdict, or resolve the request.
        sources=_checked_design_sources(root,value)
        calc=root/'design'/('calc_package.json' if value['module']=='HR' else 'calc_package_cfs.json')
        package=json.loads(calc.read_text(encoding='utf-8-sig'));records=[]
        for pointer in source_pointers:
            record=package
            try:
                for token in pointer[1:].split('/'):
                    token=token.replace('~1','/').replace('~0','~')
                    record=record[int(token)] if isinstance(record,list) else record[token]
            except (ValueError,KeyError,TypeError,IndexError):raise ValueError('Unknown checked calculation record: '+pointer) from None
            if not isinstance(record,dict) or not record.get('id'):raise ValueError('Review pointer must identify a checked record')
            records.append(dict(source_pointer=pointer,record_sha256=fingerprint(record)))
        value['revision']+=1;value['design_sources']=sources
        review=dict(request_id=request_id,author=author.strip(),note=note.strip(),revision=value['revision'],
                    reviewed_changes=pending,records=records,calc_sha256=digest(calc),
                    physical_declarations_sha256=fingerprint(value['declarations']),design_sources=sources)
        value.setdefault('design_reviews',[]).append(review)
        value['pending_design_reviews']=[p for p in value['pending_design_reviews'] if p['request_id']!=request_id]
        value['events'].append(dict(action='engineering_review_recorded',request_id=request_id,revision=value['revision'],review_sha256=fingerprint(review)))
        atomic_json(root/NAME,value)
        return dict(revision=value['revision'],sha256=fingerprint(value),review=review,export_stale=True)


class PhysicalModel:
    """Small recording API used while choosing real products and occurrences.

    Methods accept explicit selections and retain their provenance. Repetition
    utilities expand supplied geometry; they never derive an engineering choice
    from an analysis stiffness or a free-text capacity result.
    """
    def __init__(self,module,value=None):
        self.value=copy.deepcopy(value) if value else empty(module)
        self._loaded_sha=fingerprint(value) if value else None
        validate_input(self.value,module)
        self._ids={k:{r['id'] for r in self.value['declarations'].get(k,[])} for k in COLLECTIONS}

    def record(self,collection,entity,*,source=None):
        if collection not in COLLECTIONS:raise ValueError('Unknown physical collection')
        row=copy.deepcopy(entity)
        if not row.get('id'):raise ValueError('A stable entity ID is required')
        if source is not None:row['selection_source']=copy.deepcopy(source)
        target=self.value['declarations'].setdefault(collection,[])
        if row['id'] in self._ids[collection]:raise ValueError('Duplicate '+collection+' ID: '+row['id'])
        self._ids[collection].add(row['id'])
        target.append(row);return row['id']

    def material(self,id,specification,*,source=None,**properties):
        if not specification:raise ValueError('Select a material specification')
        return self.record('materials',dict(id=id,specification=specification,**properties),source=source)

    def profile(self,id,family,dimensions,*,source=None,**properties):
        return self.record('profiles',dict(id=id,family=family,dimensions=dimensions,**properties),source=source)

    def member(self,id,kind,axis,profile_id,material_id,local_y,*,reference_line,end_offsets,analysis_ids=(),source=None,**properties):
        return self.record('members',dict(id=id,kind=kind,axis=axis,profile_id=profile_id,material_id=material_id,
            local_y=local_y,reference_line=reference_line,end_offsets=end_offsets,analysis_ids=list(analysis_ids),**properties),source=source)

    def repeated_members(self,prefix,kind,axis,stations,direction,**selection):
        if len(direction)!=3 or abs(sum(float(v)**2 for v in direction)-1)>1e-6:raise ValueError('Repetition direction must be a unit vector')
        ids=[]
        for i,station in enumerate(stations,1):
            if isinstance(station,bool) or not isinstance(station,(int,float)) or not math.isfinite(station):raise ValueError('Stations must be finite inches')
            located=[[float(p[j])+station*direction[j] for j in range(3)] for p in axis]
            ids.append(self.member(prefix+str(i),kind,located,**selection))
        return ids

    def panel(self,id,kind,vertices,thickness,material_id,*,source=None,**properties):
        return self.record('panels',dict(id=id,kind=kind,vertices=vertices,thickness=thickness,material_id=material_id,**properties),source=source)

    def opening(self,id,host_id,vertices,*,source=None,**properties):
        return self.record('openings',dict(id=id,host_id=host_id,vertices=vertices,**properties),source=source)

    def request(self,id,question,proposal,*,entity_ids=(),fictional=False,**properties):
        if any(r['id']==id for r in self.value['requests']):raise ValueError('Duplicate request ID')
        self.value['requests'].append(dict(id=id,question=question,proposal=proposal,entity_ids=list(entity_ids),
            status='accepted_proposal',quantitatively_resolved=False,fictional=fictional,**properties))

    def save(self,root):
        from filelock import FileLock
        root=Path(root);root.mkdir(parents=True,exist_ok=True)
        with FileLock(str(root/'.physical-inputs.lock'),timeout=10):
            current=read(root)
            if current is not None:
                if self._loaded_sha!=fingerprint(current):raise ValueError('Physical inputs changed; reload before recording selections')
                if fingerprint(self.value)!=self._loaded_sha:self.value['revision']=current['revision']+1
            validate_input(self.value)
            atomic_json(root/NAME,self.value)
            self._loaded_sha=fingerprint(self.value)
        return dict(revision=self.value['revision'],sha256=fingerprint(self.value),path=NAME)


def apply_decision(root, *, request_id, author, note, action, expected_revision, expected_sha256, changes=(), decision_id=None):
    """Atomically update selected physical fields; checked calculations are protected."""
    from filelock import FileLock
    root=Path(root)
    with FileLock(str(root/'.physical-inputs.lock'),timeout=10):
        value=read(root)
        if value is None:raise ValueError('This job has no physical input register')
        payload=dict(request_id=request_id,author=author,note=note,action=action,changes=list(changes))
        if decision_id is not None:
            if not isinstance(decision_id,str) or not decision_id.strip() or len(decision_id)>160:raise ValueError('Invalid decision ID')
            prior=next((d for d in value['decisions'] if d.get('decision_id')==decision_id),None)
            if prior:
                if prior.get('payload_sha256')!=fingerprint(payload):raise ValueError('Decision ID already belongs to a different answer')
                return dict(revision=value['revision'],sha256=fingerprint(value),duplicate=True,
                            decision_revision=prior['revision'],export_stale=True)
        if value['revision']!=expected_revision or fingerprint(value)!=expected_sha256:raise ValueError('Physical inputs changed; refresh before answering')
        request=next((r for r in value['requests'] if r['id']==request_id),None)
        if request is None:raise ValueError('Unknown information request')
        if action not in ('accept_proposal','confirm','accept_test','reopen','delegate'):raise ValueError('Unknown decision action')
        if not isinstance(author,str) or not author.strip() or len(author)>160 or not isinstance(note,str) or not note.strip() or len(note)>12000:
            raise ValueError('A named author and concise decision/source note are required')
        if action=='accept_test' and request.get('fictional') is not True:raise ValueError('Test acceptance is only available for fictional requests')
        if changes and action not in ('confirm','accept_test'):raise ValueError('Quantitative changes require explicit confirmation')
        allowed={k:set() for k in COLLECTIONS}
        for binding in request.get('bindings',[]):
            if binding.get('collection') in allowed:allowed[binding['collection']].add((binding.get('entity_id'),binding.get('field')))
        for change in changes:
            collection=change.get('collection');identity=change.get('entity_id');field=change.get('field')
            if collection not in allowed or (identity,field) not in allowed[collection]:raise ValueError('Change is outside this request\'s declared field bindings')
            if field in ('id','status','declaration_source','design_record_ids') or not isinstance(field,str) or field.startswith('_'):
                raise ValueError('Identity and engineering evidence cannot be rewritten by an information answer')
            entity=next((e for e in value['declarations'].get(collection,[]) if e['id']==identity),None)
            if entity is None:raise ValueError('Request refers to an unknown physical entity')
            entity[field]=copy.deepcopy(change['value'])
        state={'accept_proposal':'accepted_proposal','confirm':'confirmed','accept_test':'accepted_test','reopen':'proposed','delegate':'delegated'}[action]
        request.update(status=state,quantitatively_resolved=action in ('confirm','accept_test'),answer=note.strip(),author=author.strip())
        value['revision']+=1
        event=dict(request_id=request_id,author=author.strip(),note=note.strip(),action=action,revision=value['revision'],changes=copy.deepcopy(list(changes)))
        if decision_id is not None:event.update(decision_id=decision_id,payload_sha256=fingerprint(payload))
        if changes and action!='accept_test':
            value.setdefault('pending_design_reviews',[]).append(dict(request_id=request_id,revision=value['revision'],changes_sha256=fingerprint(list(changes))))
        value['decisions'].append(event);validate_input(value);atomic_json(root/NAME,value)
        return dict(revision=value['revision'],sha256=fingerprint(value),request=copy.deepcopy(request),
            structural_review_required=bool(changes),export_stale=True)


def request_register(root):
    value=read(root)
    if value is None:return None
    requests=copy.deepcopy(value['requests'])
    indexes={k:{e['id']:e for e in value['declarations'].get(k,[])} for k in COLLECTIONS}
    for request in requests:
        for binding in request.get('bindings',[]):
            entity=indexes.get(binding.get('collection'),{}).get(binding.get('entity_id'),{})
            binding['value']=copy.deepcopy(entity.get(binding.get('field')))
    return dict(schema=SCHEMA,version=1,module=value['module'],revision=value['revision'],sha256=fingerprint(value),
        requests=requests,decisions=value['decisions'],can_generate_provisional=True,units=value['units'],
        pending_design_reviews=value.get('pending_design_reviews',[]))


def apply_layout(cfg, value):
    """Apply separately recorded physical CFS positions without altering stiffness."""
    if not value or value['module']!='CFS':return cfg
    layout=value.get('layout',{});positions=layout.get('wall_segments_ft',{})
    cfg=copy.deepcopy(cfg)
    if layout.get('line_plan_pos_ft'):
        cfg['line_plan_pos_ft']={**cfg.get('line_plan_pos_ft',{}),**layout['line_plan_pos_ft']}
    for direction,key in (('X','lines_x'),('Y','lines_y')):
        for line in cfg.get(key,[]):
            saved=positions.get(direction+'-'+str(line.name),{})
            located=dict(getattr(line,'segment_positions_ft',None) or {})
            for story,ranges in saved.items():
                segments=line.segments.get(int(story),line.segments.get(str(story)))
                groups=layout.get('wall_segment_groups',{}).get(direction+'-'+str(line.name),{}).get(str(story))
                if groups is not None:
                    # A checked equivalent wall may contain several contiguous
                    # physical braced bays. This copy is for geometry capture;
                    # the original stiffness configuration is never rewritten.
                    if segments is None or len(groups)!=len(segments) or [i for g in groups for i in g]!=list(range(1,len(ranges)+1)):
                        raise ValueError('Physical wall groups must partition every bay in native segment order')
                    expanded=[]
                    for group,segment in zip(groups,segments):
                        selected=[ranges[i-1] for i in group]
                        if not selected or any(len(r)!=2 or not r[1]>r[0] for r in selected) or any(abs(a[1]-b[0])>1e-6 for a,b in zip(selected,selected[1:])) or abs(selected[-1][1]-selected[0][0]-float(segment[0]))>1e-6:
                            raise ValueError('Physical bay group must be continuous and equal its checked segment length')
                        expanded.extend([(r[1]-r[0],segment[1]) for r in selected])
                    if not hasattr(line,'bim_native_segments_ft'):line.bim_native_segments_ft={}
                    line.bim_native_segments_ft[int(story)]=copy.deepcopy(segments)
                    line.segments[int(story)]=expanded
                    segments=expanded
                if segments is None or len(ranges)!=len(segments) or any(len(r)!=2 or not r[1]>r[0] or abs((r[1]-r[0])-float(s[0]))>1e-6 for r,s in zip(ranges,segments)):
                    raise ValueError('Accepted physical wall ranges must match native story/count/segment lengths: '+str(line.name)+'/'+str(story))
                located[int(story)]=ranges
            line.segment_positions_ft=located
    return cfg
