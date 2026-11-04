"""Portable, literal HR model input for immutable checked-design replay.

No Python source is executed while creating or reading this snapshot. Replay
executes only the explicitly permitted native modeling commands, without a solve.
"""
from __future__ import annotations
import ast
import json
from pathlib import Path
import re

try:
    from .physical_inputs import atomic_json,design_files,digest
except ImportError:
    from physical_inputs import atomic_json,design_files,digest

NAME='portable_model.json'
ALLOWED={'wipe','model','node','fix','mass','geomTransf','uniaxialMaterial','element','rigidDiaphragm','equalDOF','rigidLink'}
IGNORED={'getNodeTags','getEleTags','eigen'}


def freeze(root):
    root=Path(root).resolve();source=root/'model_opensees.py'
    if not source.is_file():raise ValueError('Portable HR replay requires the saved model_opensees.py')
    tree=ast.parse(source.read_text(encoding='utf-8-sig'));calls=[]
    direct={id(n.value) for n in tree.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)}
    for node in ast.walk(tree):
        if not (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=='ops'):continue
        command=node.func.attr
        if command in IGNORED:continue
        if command not in ALLOWED or node.keywords or id(node) not in direct:raise ValueError('Unsupported or nested saved-model command at line '+str(node.lineno))
        try:arguments=[ast.literal_eval(a) for a in node.args]
        except (ValueError,TypeError):raise ValueError('Nonliteral saved-model argument at line '+str(node.lineno)) from None
        calls.append(dict(line=node.lineno,command=command,arguments=arguments))
    calls.sort(key=lambda c:c['line'])
    if not calls or not any(c['command']=='node' for c in calls) or not any(c['command']=='element' for c in calls):
        raise ValueError('Saved model must contain explicit nodes and elements')
    viewer=(root/'viewer_3d.html').read_text(encoding='utf-8-sig')
    marker=re.search(r'\b(?:const|let|var)\s+DATA\s*=\s*',viewer)
    if marker is None:raise ValueError('Saved model metadata missing')
    data=json.JSONDecoder().raw_decode(viewer,marker.end())[0]
    rows=[(r['tag'],r['type'],r['sec'],*r['n']) for r in data['elements']]
    # A snapshot preserves a checked design; it cannot certify an unverified job.
    stamp=json.loads((root/'design/consistency_result.json').read_text(encoding='utf-8-sig'))
    if stamp.get('result')!='PASS' or stamp.get('calc_package_sha256')!=digest(root/'design/calc_package.json'):
        raise ValueError('Portable model needs matching checked gold calculation data')
    if not stamp.get('cfg_sha256') or stamp['cfg_sha256']!=digest(root/'cfg.py'):
        raise ValueError('Portable model needs a matching checked configuration hash')
    value=dict(schema='steltic.portable_model',version=1,module='HR',units=dict(length='in',force='kip',stress='ksi'),
        source_files=design_files(root),commands=calls,
        metadata=dict(ele=rows,z=data.get('meta',{}).get('levels',[]),present={},cm={}),
        verification=dict(basis='Recorded checked design; saved literal model retained. This is not an independent cfg rebuild.',
                          consistency_sha256=digest(root/'design/consistency_result.json'),analysis_executed=False))
    json.dumps(value,allow_nan=False)
    atomic_json(root/NAME,value)
    return value


def read(root):
    root=Path(root).resolve();path=root/NAME
    if not path.is_file():return None
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    if value.get('schema')!='steltic.portable_model' or value.get('version')!=1 or value.get('module')!='HR':
        raise ValueError('Unsupported portable model')
    if value.get('units')!={'length':'in','force':'kip','stress':'ksi'}:raise ValueError('Portable model units changed')
    expected=value.get('source_files',{})
    if not expected or expected!=design_files(root):raise ValueError('Portable model design sources are stale; rebuild from the checked source model')
    for row in value.get('commands',[]):
        if row.get('command') not in ALLOWED or not isinstance(row.get('arguments'),list):raise ValueError('Unsafe portable model command')
    if not value.get('commands'):raise ValueError('Portable model commands are missing')
    # Reconstruct from the still-bound source and compare the command records.
    # A modified snapshot must not silently change physical analysis geometry.
    tree=ast.parse((root/'model_opensees.py').read_text(encoding='utf-8-sig'))
    expected_calls=[]
    for node in tree.body:
        call=node.value if isinstance(node,ast.Expr) else None
        if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute) and isinstance(call.func.value,ast.Name) and call.func.value.id=='ops' and call.func.attr in ALLOWED:
            expected_calls.append(dict(line=call.lineno,command=call.func.attr,arguments=[ast.literal_eval(a) for a in call.args]))
    # JSON converts tuples to arrays; canonical JSON compares the persisted form.
    if json.dumps(expected_calls,sort_keys=True)!=json.dumps(value['commands'],sort_keys=True):raise ValueError('Portable commands differ from the bound checked source model')
    text=(root/'viewer_3d.html').read_text(encoding='utf-8-sig')
    marker=re.search(r'\b(?:const|let|var)\s+DATA\s*=\s*',text)
    if marker is None:raise ValueError('Portable model metadata source is missing')
    view=json.JSONDecoder().raw_decode(text,marker.end())[0]
    meta=dict(ele=[[r['tag'],r['type'],r['sec'],*r['n']] for r in view['elements']],
              z=view.get('meta',{}).get('levels',[]),present={},cm={})
    if value.get('metadata')!=meta:raise ValueError('Portable metadata differs from the bound checked model')
    return value


def capture(root,pkg,selected=None):
    try:from . import bim_adapter
    except ImportError:import bim_adapter
    import openseespy.opensees as ops
    value=read(root)
    if value is None:raise ValueError('Portable model not found')
    if bim_adapter.MODULE!='HR':raise ValueError('Portable OpenSees input is specific to HR')
    meta=value['metadata']
    def build(cfg,transf):
        for row in value['commands']:getattr(ops,row['command'])(*row['arguments'])
        return dict(ele=meta['ele'],z=meta['z'],present=meta.get('present',{}),cm=meta.get('cm',{}),NF=max(0,len(meta['z'])-1))
    cfg=dict(custom_build=build,present={})
    if selected:cfg['bim']={'panels':selected['declarations'].get('panels',[])}
    snapshot=bim_adapter.capture(cfg,pkg,Path(root))
    if snapshot['analysis'].get('comparison',{}).get('status')!='match':raise ValueError('Portable model does not match its checked native source')
    return {},snapshot,dict(kind='portable_checked_model',path=NAME,sha256=digest(Path(root)/NAME),
        independent_configuration_rebuild=False,basis=value['verification']['basis'])
