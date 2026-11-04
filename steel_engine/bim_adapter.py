"""HR geometry capture. Builds the actual model, without running an analysis."""
import ast
import csv
import json
import math
import re

MODULE = "HR"
PACKAGE = "calc_package.json"


def disabled(*args, **kwargs):
    raise RuntimeError("Analysis/design execution is disabled during BIM replay")


def disable_analysis():
    import openseespy.opensees as ops
    for name in ("analyze", "eigen", "responseSpectrumAnalysis"):
        if hasattr(ops, name): setattr(ops, name, disabled)
    import design_pipeline, pipeline
    design_pipeline.design = disabled
    pipeline.design_and_report = disabled


def cross(a,b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def normal(v):
    length=math.sqrt(sum(x*x for x in v))
    return [x/length for x in v] if length > 1e-10 else None


def equivalent(a,b):
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):
        return math.isclose(a,b,rel_tol=1e-7,abs_tol=1e-7)
    if isinstance(a,(list,tuple)) and isinstance(b,(list,tuple)):
        return len(a)==len(b) and all(equivalent(x,y) for x,y in zip(a,b))
    return a==b


def capture(cfg, pkg, root):
    import openseespy.opensees as ops
    import engine3d as E
    import sections as S
    from bim_export import issue
    calls={}; transforms={}; supports={}
    original={k:getattr(ops,k) for k in ("wipe","element","geomTransf","fix")}
    def wipe(*args):
        calls.clear(); transforms.clear(); supports.clear()
        return original["wipe"](*args)
    def element(*args):
        result=original["element"](*args)
        calls[str(args[1])]=list(args)
        return result
    def transform(*args):
        result=original["geomTransf"](*args)
        transforms[str(args[1])]=list(args)
        return result
    def fix(*args):
        result=original["fix"](*args)
        supports[str(args[0])]=list(args[1:])
        return result
    ops.wipe=wipe; ops.element=element; ops.geomTransf=transform; ops.fix=fix
    try:
        info=E.build(cfg, "PDelta")
        coords={str(tag):[float(x) for x in ops.nodeCoord(tag)] for tag in ops.getNodeTags()}
        connectivity={str(tag):[str(n) for n in ops.eleNodes(tag)] for tag in ops.getEleTags()}
    finally:
        for name,fn in original.items(): setattr(ops,name,fn)
    metadata={str(r[0]):r for r in info.get("ele", [])}
    schedules={}
    schedule=root/"design"/"member_schedule.csv"
    if schedule.is_file():
        with schedule.open(encoding="utf-8-sig",newline="") as stream:
            schedules={str(row["ele_tag"]):row for row in csv.DictReader(stream)}
    elements=[]; members=[]; profiles={}; excluded=[]; findings=[]
    for tag,nodes in sorted(connectivity.items(),key=lambda x:int(x[0])):
        row=metadata.get(tag)
        kind=str(row[1]) if row else "unclassified"
        sec=str(row[2]) if row else None
        args=calls.get(tag, [])
        eid="hr:element:"+tag
        transform_id=None; local_y=None
        if args and args[0]=="elasticBeamColumn" and len(args)>10:
            transform_id=str(args[10])
            vector=transforms.get(transform_id, [])[2:5]
            if len(nodes)==2 and len(vector)==3:
                local_x=normal([b-a for a,b in zip(coords[nodes[0]],coords[nodes[1]])])
                if local_x: local_y=normal(cross(vector,local_x))
        elements.append({"id":eid,"tag":tag,"node_ids":["hr:node:"+n for n in nodes],"kind":kind,"profile_id":sec,
                         "element_type":str(args[0]) if args else None,"transform_id":transform_id,
                         "local_y":local_y,"end_releases":info.get("beam_rel",{}).get(int(tag)),
                         "source_schedule":schedules.get(tag),"builder_arguments":args})
        if sec and sec not in profiles:
            try:
                props={k:float(v) for k,v in S.props(sec).items() if isinstance(v,(float,int)) and math.isfinite(v)}
            except (KeyError,ValueError,TypeError,AttributeError): props={}
            profiles[sec]={"id":sec,"status":"recovered","family":"source_section","dimensions":{k:props[k] for k in ("d","bf","tf","tw") if props.get(k,0)>0},"properties":props,"basis":"sections.props in replay engine; confirm physical/custom assembly"}
        if len(nodes)!=2 or coords[nodes[0]]==coords[nodes[1]]:
            excluded.append({"analysis_id":eid,"reason":"Non-line or zero-length analysis entity; physical assembly must be declared separately"})
            continue
        # These source roles/names explicitly describe nonphysical idealizations.
        if kind.lower() in ("rigid", "rigid_link", "rigid-zone") or (sec or "").startswith("RZ-"):
            excluded.append({"analysis_id":eid,"reason":"Rigid analysis idealization"}); continue
        from physical_frame import width_axis
        axis=[coords[nodes[0]],coords[nodes[1]]]
        physical_y=width_axis(axis,local_y,args,profiles.get(sec,{}).get('properties',{})) if (sec or '').startswith(('W','S','M','HP')) else local_y
        members.append({"id":eid,"kind":kind,"status":"candidate","axis":axis,
                        "local_y":physical_y,"analysis_local_y":local_y,
                        "orientation_basis":"Recorded element inertias mapped to profile width/depth axes" if physical_y else "Explicit physical section orientation required",
                        "profile_id":sec,"material_id":None,"reference_line":None,
                        "end_offsets":None,"analysis_ids":[eid],"geometry_basis":"analysis_axis"})
    # Compare literal saved geometry; never execute the saved script's modal self-check.
    gold=root/"model_opensees.py"
    if gold.is_file():
        gold_nodes={}; gold_elements={}; gold_calls={}; gold_transforms={}; unparsed=[]
        for node in ast.walk(ast.parse(gold.read_text(encoding="utf-8-sig"))):
            if not (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=="ops" and node.func.attr in ("node","element","geomTransf")): continue
            try: args=[ast.literal_eval(a) for a in node.args]
            except (ValueError,TypeError): unparsed.append(node.lineno); continue
            if node.func.attr=="node": gold_nodes[str(args[0])]=[float(x) for x in args[1:4]]
            elif node.func.attr=="geomTransf": gold_transforms[str(args[1])]=args
            else:
                gold_elements[str(args[1])]=[str(x) for x in args[2:2+len(connectivity.get(str(args[1]),[]))]]
                gold_calls[str(args[1])]=args
        differences=[]
        if set(gold_elements)!=set(connectivity): differences.append("element tag set")
        for tag,nodes in connectivity.items():
            if gold_elements.get(tag)!=nodes: differences.append("connectivity "+tag)
            if not equivalent(gold_calls.get(tag),calls.get(tag)): differences.append("element properties/options "+tag)
            for n in nodes:
                if n not in gold_nodes or any(abs(a-b)>1e-7 for a,b in zip(coords[n],gold_nodes[n])): differences.append("coordinates "+n)
        if set(gold_transforms)!=set(transforms): differences.append("transformation tag set")
        for tag,args in transforms.items():
            if not equivalent(gold_transforms.get(tag),args): differences.append("orientation/offsets "+tag)
        if differences or unparsed:
            findings.append(issue("legacy_geometry_mismatch","Replayed geometry differs from saved model or contains nonliteral source calls",differences+["line "+str(n) for n in unparsed],"error"))
        comparison={"status":"match" if not differences and not unparsed else "mismatch","source":"model_opensees.py","elements_compared":len(connectivity),"tolerance_in":1e-7,"checks":["element tags","connectivity","coordinates","element properties/options","transformations/orientations/offsets"],"properties_relative_tolerance":1e-7}
    else:
        comparison={"status":"not_available","source":"model_opensees.py"}
    missing_metadata=[e["id"] for e in elements if e["kind"]=="unclassified"]
    if missing_metadata: findings.append(issue("member_metadata","Builder did not label all analysis elements",missing_metadata))
    # Geometry declarations remain explicit, including wall/strip replacements.
    if not (cfg.get("bim",{}).get("panels") or pkg.get("bim",{}).get("panels")) and any("SPSW" in str(v).upper() or "CCPSW" in str(v).upper() for v in (cfg.get("arch"),cfg.get("system"))):
        findings.append(issue("physical_idealization","Wall analysis idealizations require explicit physical panels/member mapping"))
    return {"engine_version":ops.version(), "analysis":{"nodes":[{"id":"hr:node:"+n,"point":p,"restraints":supports.get(n)} for n,p in sorted(coords.items(),key=lambda x:int(x[0]))],
              "elements":elements,"transforms":transforms,"comparison":comparison},
            "physical":{"members":members,"profiles":list(profiles.values()),"analysis_only":excluded},"findings":findings,
            "layout_evidence":{"levels_in":info.get("z",[]),"grid_x_in":cfg.get("xcoords"),"grid_y_in":cfg.get("ycoords"),
                               "materials":cfg.get("materials"),
                               "openings":cfg.get("openings") if not callable(cfg.get("openings")) else "declared by cfg callable; see source",
                               "physical_note":"Analysis axes do not establish member cardinal points, cutbacks, plate placement or physical wall assemblies."}}


def capture_saved(root,pkg,reason):
    """Replay only literal modeling commands; do not execute a saved Python file."""
    import openseespy.opensees as ops
    from bim_export import issue
    path=root/"model_opensees.py"
    if not path.is_file(): raise ValueError("Configuration import failed and no saved model is available: "+reason)
    allowed={"wipe","model","node","fix","mass","geomTransf","uniaxialMaterial","element","rigidDiaphragm","equalDOF","rigidLink"}
    ignored={"getNodeTags","getEleTags","eigen"}  # saved script's self-check is never run
    commands=[]
    tree=ast.parse(path.read_text(encoding="utf-8-sig"))
    direct={id(node.value) for node in tree.body if isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)}
    for node in ast.walk(tree):
        if not (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=="ops"): continue
        fn=node.func.attr
        if fn in ignored: continue
        if fn not in allowed or node.keywords or id(node) not in direct: raise ValueError("Unsupported or nested saved-model command at line "+str(node.lineno))
        try: args=[ast.literal_eval(a) for a in node.args]
        except (TypeError,ValueError) as ex: raise ValueError("Nonliteral saved-model command at line "+str(node.lineno)) from ex
        commands.append((node.lineno,fn,args))
    if not commands: raise ValueError("Saved model contains no literal model commands")
    commands.sort()
    text=(root/"viewer_3d.html").read_text(encoding="utf-8-sig")
    marker=re.search(r"\b(?:const|let|var)\s+DATA\s*=\s*",text)
    view=json.JSONDecoder().raw_decode(text,marker.end())[0]
    rows=[(r["tag"],r["type"],r["sec"],*r["n"]) for r in view["elements"]]
    def builder(cfg,transf):
        for _,name,args in commands: getattr(ops,name)(*args)
        return {"ele":rows,"z":view["meta"].get("levels",[]),"present":{},"cm":{},"NF":len(view["meta"].get("levels",[]))-1}
    cfg={"custom_build":builder,"present":{}}
    # Recover only literal BIM declarations; never pretend the full cfg was loaded.
    for node in ast.parse((root/"cfg.py").read_text(encoding="utf-8-sig")).body:
        if isinstance(node,ast.Assign) and len(node.targets)==1:
            target=node.targets[0]
            if isinstance(target,ast.Subscript) and isinstance(target.value,ast.Name) and target.value.id=="cfg":
                try:
                    key=ast.literal_eval(target.slice)
                    if key in ("bim","materials"): cfg[key]=ast.literal_eval(node.value)
                except (ValueError,TypeError): pass
    snapshot=capture(cfg,pkg,root)
    snapshot["analysis"]["comparison"]["status"]="replayed_saved_model"
    snapshot["findings"].append(issue("legacy_configuration_dependency","Used saved literal model because configuration dependencies are unavailable: "+reason))
    return cfg,snapshot
