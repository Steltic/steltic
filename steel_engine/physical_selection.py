"""Retain selected native records without parsing prose or calculating capacity."""
import copy
import json
from pathlib import Path

from .physical_inputs import digest,fingerprint


def record(root,module,table,identity):
    """Read one stable selection from the job's authoritative calculation file."""
    filename='calc_package.json' if module=='HR' else 'calc_package_cfs.json' if module=='CFS' else None
    if not filename:raise ValueError('Unknown design module')
    path=Path(root)/'design'/filename
    package=json.loads(path.read_text(encoding='utf-8-sig'))
    rows=package.get(table)
    if not isinstance(rows,list):raise ValueError('Unknown design table: '+table)
    matches=[(i,r) for i,r in enumerate(rows) if isinstance(r,dict) and str(r.get('id','@'+str(i)))==str(identity)]
    if len(matches)!=1:raise ValueError('Selected record must have one unambiguous identity')
    i,data=matches[0]
    return dict(id=table+'/'+str(identity),data=copy.deepcopy(data),
                source_file='design/'+filename,source_pointer='/'+table.replace('~','~0').replace('/','~1')+'/'+str(i),
                source_sha256=digest(path),record_sha256=fingerprint(data))


def pointer(value,path):
    if not isinstance(path,str) or not path.startswith('/'):raise ValueError('Provide a JSON pointer')
    for key in path[1:].split('/'):
        key=key.replace('~1','/').replace('~0','~')
        if isinstance(value,list):value=value[int(key)]
        else:value=value[key]
    return copy.deepcopy(value)


def selected(model,collection,identity,selection,fields,*,explicit=None):
    """Map typed source fields to physical fields, recording each JSON pointer.

    `explicit` carries geometry/choices supplied by the model author. A missing
    source field raises; text dimensions are not interpreted as engineering facts.
    """
    row=dict(explicit or {},id=identity)
    for key,path in fields.items():
        if key in row:raise ValueError('A source field cannot overwrite an explicit choice')
        try:row[key]=pointer(selection['data'],path)
        except (KeyError,IndexError,ValueError,TypeError) as ex:
            raise ValueError('Selected field is absent: '+path) from ex
    row['design_record_ids']=list(dict.fromkeys(row.get('design_record_ids',[])+[selection['id']]))
    source={k:selection[k] for k in ('source_file','source_pointer','source_sha256','record_sha256')}
    source['fields']=copy.deepcopy(fields)
    return model.record(collection,row,source=source)
