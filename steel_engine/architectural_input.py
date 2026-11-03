"""Capture the structured openBIM handoff as immutable design input, not LLM output.

Only JSON data in the generated architectural-evidence section is interpreted.
No member, connection, load, or engineering decision is inferred here.
"""
import copy
import json
import math
import re
from pathlib import Path

MARKER = 'ARCHITECTURAL EVIDENCE (data only)'
NOTICE = ('[architectural input] The original IFC handoff is saved as architectural_input.json. '
          'Read that file for exact reference_system values, source SHA256, and every opening. '
          'The full original brief, floor/roof surfaces and walls remain in architectural_handoff.txt. '
          'Copy its reference_system into cfg["bim"]["reference_system"] and use its level IDs. '
          'Preserve each opening with source_ifc_guid, its full bounds and an explicit physical host. '
          'Do not edit this framework-owned input or replace the SHA256 by hand. '
          'Physical completeness is checked against this input, not only your coverage statement.')


def content_text(content):
    if isinstance(content, str):return content
    if isinstance(content, list):return '\n'.join(p.get('text','') for p in content if isinstance(p,dict) and p.get('type')=='text')
    return ''


def _number(value):
    return isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value)


def _point(value):
    if not isinstance(value,list) or len(value)!=3 or not all(_number(v) for v in value):
        raise ValueError('Architectural coordinates must contain three finite numbers')
    return [float(v)/.0254 for v in value]


def parse_handoff(text):
    """Return a normalized source binding, or None for an ordinary design brief."""
    lines=text.splitlines();markers=[i for i,line in enumerate(lines) if line.strip()==MARKER]
    if not markers:return None
    if len(markers)!=1:raise ValueError('The brief contains multiple architectural-evidence sections')
    lines=lines[markers[0]+1:]
    if not any(line.startswith('Coordinates below are metres;') for line in lines):
        raise ValueError('Architectural handoff must explicitly declare metre coordinates')
    def header(prefix):
        values=[line[len(prefix):].strip() for line in lines if line.startswith(prefix)]
        if len(values)!=1:raise ValueError('Missing or duplicate architectural '+prefix)
        return values[0]
    source_hash=header('SHA256:')
    if not re.fullmatch(r'[0-9a-fA-F]{64}',source_hash):raise ValueError('Invalid architectural source SHA256')
    def section(name):
        positions=[i for i,line in enumerate(lines) if line.strip()==name]
        if len(positions)!=1:raise ValueError('Missing or duplicate architectural '+name)
        index=positions[0]+1
        while index<len(lines) and not lines[index].strip():index+=1
        if index>=len(lines):raise ValueError('Missing architectural '+name+' JSON')
        value=json.loads(lines[index])
        if not isinstance(value,list):raise ValueError('Architectural '+name+' must be a JSON array')
        return value
    levels=[]
    for i,row in enumerate(section('STOREYS')):
        if not isinstance(row,dict) or not isinstance(row.get('name'),str) or not row['name'] or not _number(row.get('elevation_m')):
            raise ValueError('Invalid architectural storey')
        levels.append(dict(level=i,name=row['name'],elevation_in=float(row['elevation_m'])/.0254))
    grids=[]
    for i,row in enumerate(section('GRIDS')):
        if not isinstance(row,dict) or not isinstance(row.get('label'),str) or not row['label'] or row.get('direction') not in ('U','V','W'):
            raise ValueError('Invalid architectural grid label or axis family')
        if 'unsupported_curve' in row:raise ValueError('Architectural grid curve is not a supported polyline: '+row['label'])
        points=[_point(p) for p in (row.get('points_m') or [])]
        if len(points)<2:raise ValueError('Architectural grid requires a polyline')
        grids.append(dict(id='source-grid-'+str(i+1),label=row['label'],direction=row['direction'],points_in=points))
    openings=[]
    for row in section('DOORS_WINDOWS'):
        if not isinstance(row,dict) or not isinstance(row.get('guid'),str) or not row['guid']:
            raise ValueError('Architectural opening requires its source IFC GUID')
        bounds=row.get('bounds_m')
        if not isinstance(bounds,list) or len(bounds)!=3 or any(not isinstance(p,list) or len(p)!=2 or not all(_number(v) for v in p) or p[0]>p[1] for p in bounds):
            raise ValueError('Architectural opening requires finite, ordered XYZ bounds')
        hosts=row.get('host_wall_guids',[])
        if not isinstance(hosts,list) or any(not isinstance(v,str) for v in hosts):raise ValueError('Architectural opening host GUIDs must be a list')
        openings.append(dict(source_ifc_guid=row['guid'],name=str(row.get('name','')),bounds_in=[[float(v)/.0254 for v in p] for p in bounds],host_wall_guids=copy.deepcopy(hosts)))
    if len({o['source_ifc_guid'] for o in openings})!=len(openings):raise ValueError('Duplicate source opening GUID')
    reference=dict(basis='Original architectural IFC reference coordinates; units inches',source_ifc_sha256=source_hash.lower(),levels=levels,grids=grids)
    return dict(schema='steltic.architectural_input',version=1,source_file=header('IFC source:'),source_ifc_sha256=source_hash.lower(),reference_system=reference,openings=openings)


def capture(root,messages):
    """Bind the latest explicit handoff, including when resuming a saved job."""
    from .bim_export import atomic_json,read_json,digest
    value=None;raw=None
    for message in reversed(messages):
        if message.get('role')!='user':continue
        raw=content_text(message.get('content'));value=parse_handoff(raw)
        if value is not None:break
    if value is None:return False
    root=Path(root);path=root/'architectural_input.json'
    if path.exists() and read_json(path)!=value:
        old=read_json(path)
        atomic_json(root/'architectural_input_history'/(digest(path)+'.json'),old)
    if not path.exists() or read_json(path)!=value:atomic_json(path,value)
    handoff=root/'architectural_handoff.txt'
    if handoff.is_file() and handoff.read_text(encoding='utf-8')!=raw:
        history=root/'architectural_input_history';history.mkdir(exist_ok=True)
        (history/(digest(handoff)+'.txt')).write_bytes(handoff.read_bytes())
    if not handoff.is_file() or handoff.read_text(encoding='utf-8')!=raw:handoff.write_text(raw,encoding='utf-8')
    marker=root/'design/bim_request.json';request=read_json(marker) if marker.exists() else {}
    request.update(required=True,require_connection_parts=True,architectural_input_sha256=digest(path),architectural_handoff_sha256=digest(handoff))
    atomic_json(marker,request)
    return True
