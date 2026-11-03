"""Physical-data export v1. Identical in HR and CFS; adapters own engine details.

The parent reads/writes JSON only. Trusted job Python executes in a fresh child,
in a temporary directory, with analysis entry points disabled. This is process
isolation, not a security sandbox for untrusted Python.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile

ENGINE = Path(__file__).resolve().parent
if __package__:
    from .bim_contract import VERSION, COLLECTIONS, COVERAGE, issue, validate
else:
    from bim_contract import VERSION, COLLECTIONS, COVERAGE, issue, validate


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evidence_json(value):
    """Keep native metadata with non-string map keys without losing key types."""
    if isinstance(value,dict):
        if all(isinstance(k,str) for k in value): return {k:evidence_json(v) for k,v in value.items()}
        return {"entries":[{"key":evidence_json(k),"value":evidence_json(v)} for k,v in value.items()]}
    if isinstance(value,(list,tuple)): return [evidence_json(v) for v in value]
    if isinstance(value,set): return [evidence_json(v) for v in sorted(value,key=str)]
    return value


def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    fd, temp = tempfile.mkstemp(prefix=".bim-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def assemble(root, cfg, pkg, snapshot, provenance):
    """Join structured declarations and captured geometry; never parse prose as facts."""
    model = snapshot.get("physical", {})
    physical = {key:list(model.get(key, [])) for key in COLLECTIONS}
    physical.update(analysis_only=list(model.get("analysis_only", [])), record_dispositions=[], joint_dispositions=[], coverage={})
    findings = list(snapshot.get("findings", []))
    reference_system=None
    for origin, declaration in (("cfg.py#/bim", cfg.get("bim", {})), ("calculation package#/bim", pkg.get("bim", {}))):
        if not isinstance(declaration, dict): raise ValueError(origin+" must be an object")
        if declaration and declaration.get("schema_version") != VERSION: raise ValueError(origin+" requires schema_version "+VERSION)
        if 'reference_system' in declaration:
            value=declaration['reference_system']
            if reference_system is not None and reference_system!=value:raise ValueError('Conflicting physical reference systems')
            reference_system=value
        for key in COLLECTIONS:
            supplied = declaration.get(key, [])
            if not isinstance(supplied, list): raise ValueError(origin+" "+key+" must be a list")
            target = {row["id"]:row for row in physical[key]}
            seen = set()
            for row in supplied:
                if not isinstance(row, dict) or not row.get("id") or row["id"] in seen: raise ValueError(origin+" duplicate/invalid "+key+" ID")
                seen.add(row["id"])
                if row["id"] in target and target[row["id"]].get("status") == "declared": raise ValueError(origin+" conflicting declaration "+row["id"])
                target[row["id"]] = dict(row, status="declared", declaration_source=origin)
            physical[key] = list(target.values())
        for key in ("analysis_only", "record_dispositions", "joint_dispositions"):
            physical[key].extend(declaration.get(key, []))
        for key,value in declaration.get("coverage", {}).items():
            if key in physical["coverage"] and physical["coverage"][key] != value: raise ValueError("Conflicting coverage: "+key)
            physical["coverage"][key] = value
    # Explicit physical members replace recovered candidates representing those elements.
    claimed = {a for m in physical["members"] if m.get("status") == "declared" for a in m.get("analysis_ids", [])}
    claimed.update(s["analysis_id"] for m in physical["members"] if m.get("status") == "declared" for s in m.get("analysis_segments", []))
    excluded = {a["analysis_id"] for a in physical["analysis_only"]}
    physical["members"] = [m for m in physical["members"] if m.get("status") == "declared" or not (set(m.get("analysis_ids", [])) & (claimed | excluded))]
    records = []
    context = {}
    for table,value in pkg.items():
        if table == "bim": continue
        if not isinstance(value, list) or any(not isinstance(row,dict) for row in value): context[table]=value
        if not isinstance(value, list): continue
        for i,row in enumerate(value):
            if not isinstance(row, dict): continue
            rid = table+"/"+str(row.get("id", "@"+str(i)))
            if any(r["id"]==rid for r in records): raise ValueError("Duplicate design record ID: "+rid)
            records.append({"id":rid, "table":table, "source_pointer":"/"+table.replace("~","~0").replace("/","~1")+"/"+str(i), "waived":bool(row.get("waived")), "data":row})
    source_files = provenance["source_files"]
    consistency = root/"design"/"consistency_result.json"
    if consistency.is_file():
        result = read_json(consistency)
        cp = root/"design"/provenance["package_file"]
        expected = result.get("calc_package_sha256") or result.get("package_sha1")
        actual = digest(cp) if result.get("calc_package_sha256") else hashlib.sha1(cp.read_bytes()).hexdigest()
        cfg_ok = not result.get("cfg_sha256") or result["cfg_sha256"] == digest(root/"cfg.py")
        if result.get("result") != "PASS" or not expected or expected != actual or not cfg_ok:
            findings.append(issue("design_provenance", "Recorded design consistency is failing or stale"))
    else:
        findings.append(issue("design_provenance", "No recorded design consistency verdict"))
    result = {"schema":"steltic.bim_package", "schema_version":VERSION, "building_id":root.name,
              "module":provenance["module"], "units":{"length":"in","force":"kip","stress":"ksi"},
              "provenance":provenance, "analysis":snapshot.get("analysis", {"nodes":[],"elements":[]}),
              "physical":physical, "design_records":records, "design_context":context,
              "layout_evidence":snapshot.get("layout_evidence", {}), "source_findings":findings,
              "required":bool(cfg.get("bim", {}).get("required") or ((root/"design/bim_request.json").is_file() and read_json(root/"design/bim_request.json").get("required"))), "readiness":{}}
    request=root/'design/bim_request.json'
    requirements=read_json(request) if request.is_file() else {}
    if requirements.get('require_connection_parts') or cfg.get('bim',{}).get('require_connection_parts'):
        result['require_connection_parts']=True
    if requirements.get('architectural_input_sha256'):
        source=root/'architectural_input.json'
        handoff=root/'architectural_handoff.txt';handoff_hash=requirements.get('architectural_handoff_sha256')
        handoff_changed=bool(handoff_hash and (not handoff.is_file() or digest(handoff)!=handoff_hash))
        if not source.is_file() or digest(source)!=requirements['architectural_input_sha256'] or handoff_changed:
            findings.append(issue('architectural_input_changed','The saved architectural input is missing or changed; restore the original handoff through the design interface'))
        else:
            result['architectural_input']=read_json(source)
            if reference_system is None:reference_system=copy.deepcopy(result['architectural_input']['reference_system'])
            else:reference_system=copy.deepcopy(reference_system)
            # Identity is copied from the original input, never retyped by the model.
            reference_system['source_ifc_sha256']=result['architectural_input']['source_ifc_sha256']
    if reference_system is not None:result['reference_system']=reference_system
    found = validate(result)
    result["readiness"] = {"status":"invalid" if any(i["severity"]=="error" for i in found) else ("needs_input" if found else "ready"),
                           "findings":found, "counts":{"analysis_elements":len(result["analysis"]["elements"]), "members":len(physical["members"]), "panels":len(physical["panels"]), "design_records":len(records)}}
    return result


def tracked_sources(root):
    """Fingerprint evidence consumed by replay, including local config helper modules."""
    ignored = {".git", ".venv", "__pycache__", ".run", "rag", "figs", "design", "bim", "exports", "outputs"}
    volatile = {"conversation.json", "activity_log.jsonl", "run_log.jsonl", "hub_runs.jsonl"}
    # Configs may read data (e.g. schedule.json), not just import Python. Track
    # local input files outside generated-output folders, including nested data.
    # Explicit additional inputs inside excluded folders can be listed by the
    # job in bim_inputs.json. Never interpret paths outside the job directory.
    files = {p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".py", ".json", ".csv", ".tsv", ".txt", ".yaml", ".yml", ".toml", ".xlsx")
             and p.relative_to(root).as_posix() not in volatile
             and not any(part in ignored or part.startswith(".bim-work-") for part in p.relative_to(root).parts)}
    files.update(p for p in root.rglob("*.py") if not any(part in (".git", ".venv", "__pycache__", ".run", "rag", "figs") or part.startswith(".bim-work-") for part in p.relative_to(root).parts))
    manifest = root / "bim_inputs.json"
    if manifest.is_file():
        extra = read_json(manifest)
        if not isinstance(extra, list) or any(not isinstance(v, str) for v in extra):
            raise ValueError("bim_inputs.json must be a list of job-relative input paths")
        for relative in extra:
            p = (root / relative).resolve()
            if not p.is_relative_to(root.resolve()) or not p.is_file():
                raise ValueError("BIM input must be an existing file inside the job: " + relative)
            files.add(p)
    for rel in ("design/calc_package.json", "design/calc_package_cfs.json", "design/consistency_result.json", "design/member_schedule.csv", "design/bim_request.json", "viewer_3d.html"):
        p=root/rel
        if p.is_file(): files.add(p)
    return {p.relative_to(root).as_posix():digest(p) for p in sorted(files)}


def run_worker(command, cwd, env, timeout):
    """Kill the complete owned child tree on timeout (Windows venvs use a launcher)."""
    options={"creationflags":subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name=="nt" else {"start_new_session":True}
    with subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True,encoding="utf-8",errors="replace",**options) as proc:
        try:
            stdout,stderr=proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name=="nt":
                subprocess.run(["taskkill","/PID",str(proc.pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            proc.kill()
            proc.communicate(timeout=10)
            raise
        return subprocess.CompletedProcess(command,proc.returncode,stdout,stderr)


def replay(job, output=None, timeout=120):
    """Export a completed job in isolation. Existing design artifacts are read only."""
    root = Path(job).resolve()
    if not (root/"cfg.py").is_file(): raise ValueError("Job needs cfg.py")
    output = Path(output).resolve() if output else root/"design"/"bim_package.json"
    before = tracked_sources(root)
    if output == root or output in [root/p for p in before]: raise ValueError("Export cannot overwrite source evidence")
    if output.name != "bim_package.json": raise ValueError("Output filename must be bim_package.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bim-work-", dir=output.parent, ignore_cleanup_errors=True) as temp:
        temp = Path(temp)
        if not temp.resolve().is_relative_to(output.parent): raise ValueError("Replay scratch directory escapes output directory")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE":"1", "PYTHONUTF8":"1", "PYTHONIOENCODING":"utf-8", "STEEL_BUILDER_JOBS":str(temp)}
        env["PYTHONPATH"] = os.pathsep.join((str(ENGINE), str(ENGINE.parent)))
        try:
            proc = run_worker([sys.executable, str(Path(__file__).resolve()), "_worker", str(root), str(temp/"result.json")], temp, env, timeout)
        finally:
            after = tracked_sources(root)
            if before != after: raise RuntimeError("Source evidence changed during replay; no BIM package published")
        if proc.returncode or not (temp/"result.json").is_file():
            raise RuntimeError("Model-only replay failed:\n"+(proc.stdout+proc.stderr)[-5000:])
        result = read_json(temp/"result.json")
        if result["provenance"]["source_files"] != before: raise RuntimeError("Source snapshot differs from worker input")
    atomic_json(output, result)
    return result


def worker(job, output):
    import runpy
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ENGINE))
    import bim_adapter as adapter
    root=Path(job).resolve()
    source_files=tracked_sources(root)
    adapter.disable_analysis()
    # Execute a staged job tree. A mere sys.path entry cannot resolve ordinary
    # relative data reads or runpy.run_path('helper.py'), and __file__-relative
    # writes must not land in the completed source job.
    staged=Path(output).resolve().parent/'inputs'/root.name
    staged.mkdir(parents=True)
    for relative,expected in source_files.items():
        source=(root/relative).resolve();target=(staged/relative).resolve()
        if not source.is_relative_to(root) or not target.is_relative_to(staged):
            raise ValueError('Replay input must stay inside the job: '+relative)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)
        if digest(target)!=expected:raise RuntimeError('Replay input changed while staging: '+relative)
    os.chdir(staged)
    os.environ['STEEL_BUILDER_JOBS']=str(staged.parent)
    sys.path.insert(1, str(staged))
    package_file=adapter.PACKAGE
    pkg=read_json(root/"design"/package_file)
    try:
        cfg=runpy.run_path(str(staged/"cfg.py"))["cfg"]
    except ModuleNotFoundError as ex:
        if not hasattr(adapter,"capture_saved"): raise
        cfg,snapshot=adapter.capture_saved(staged,pkg,str(ex))
    else:
        snapshot=adapter.capture(cfg, pkg, staged)
    snapshot["layout_evidence"]=evidence_json(snapshot.get("layout_evidence",{}))
    provenance={"module":adapter.MODULE,"package_file":package_file,"source_files":source_files,
                "exporter_revision":os.environ.get("STELTIC_BIM_REVISION"),"producer_revision":None,
                "engine_files":{p.name:digest(p) for p in sorted(ENGINE.iterdir()) if p.is_file() and p.suffix in (".py",".csv",".json")},
                "engine_version":snapshot.pop("engine_version",None),"mode":"model_only_replay", "analysis_executed":False}
    result=assemble(root,cfg,pkg,snapshot,provenance)
    atomic_json(output,result)


def after_consistency(root, cfg, design_issues):
    """New declared jobs export automatically. Legacy replay is explicit and read-only.

    BIM readiness stays separate from the recorded structural consistency verdict.
    An opted-in job returns BIM findings to its completion check without changing
    the original package or reporting those as structural calculation failures.
    """
    request=Path(root)/"design/bim_request.json"
    required=request.is_file() and read_json(request).get("required") is True
    if not isinstance(cfg, dict) or not isinstance(cfg.get("bim"), dict):
        return ["BIM declaration missing: new jobs require cfg['bim']; see contract/BIM_EXPORT.md"] if required else []
    if cfg["bim"].get("required"):
        marker=read_json(request) if request.is_file() else {}
        marker.update(required=True,cfg_sha256=digest(Path(root)/"cfg.py"))
        if cfg['bim'].get('require_connection_parts'):marker['require_connection_parts']=True
        atomic_json(request,marker)
    if design_issues: return []
    try:
        exported=replay(root)
        if (required or cfg["bim"].get("required")) and exported["readiness"]["status"] != "ready":
            return ["BIM "+f["code"]+": "+f["message"] for f in exported["readiness"]["findings"]]
    except Exception as ex:
        return ["BIM export failed: "+str(ex)]
    return []


def completion_issues(job):
    """Engine-free freshness/readiness gate for jobs opting into the BIM contract."""
    root=Path(job)
    path=root/"design"/"bim_package.json"
    request=root/"design"/"bim_request.json"
    required=request.is_file() and read_json(request).get("required") is True
    if not path.is_file(): return ["Required BIM export is missing; run consistency.check or BIM replay"] if required else []
    try:
        pkg=read_json(path)
        if not pkg.get("required") and not required: return []
        if required and not pkg.get("required"): return ["Required BIM export cannot be disabled by changing package metadata"]
        if pkg["provenance"]["source_files"] != tracked_sources(root): return ["BIM export is stale; rerun consistency.check or BIM replay"]
        found=validate(pkg)
        return ["BIM "+f["code"]+": "+f["message"] for f in found]
    except Exception as ex:
        return ["BIM package cannot be validated: "+str(ex)]


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    sub=ap.add_subparsers(dest="command",required=True)
    one=sub.add_parser("replay",help="Build geometry and join completed design data without analysis")
    one.add_argument("job",type=Path); one.add_argument("--output",type=Path)
    one.add_argument("--timeout",type=float,default=120); one.add_argument("--require-ready",action="store_true")
    batch=sub.add_parser("batch",help="Replay every immediate job directory independently")
    batch.add_argument("jobs_root",type=Path); batch.add_argument("--output-root",type=Path,required=True)
    batch.add_argument("--timeout",type=float,default=120); batch.add_argument("--require-ready",action="store_true")
    check=sub.add_parser("validate"); check.add_argument("package",type=Path)
    w=sub.add_parser("_worker"); w.add_argument("job"); w.add_argument("output")
    args=ap.parse_args(argv)
    if args.command=="_worker": worker(args.job,args.output); return 0
    if args.command=="validate":
        findings=validate(read_json(args.package)); print(json.dumps(findings,indent=2)); return 2 if findings else 0
    if args.command=="replay":
        p=replay(args.job,args.output,args.timeout)
        print(json.dumps({"building":p["building_id"],"status":p["readiness"]["status"],"counts":p["readiness"]["counts"],"finding_codes":sorted({f["code"] for f in p["readiness"]["findings"]})},indent=2))
        return 2 if p["readiness"]["status"]=="invalid" or (args.require_ready and p["readiness"]["status"]!="ready") else 0
    rows=[]
    jobs=sorted(p for p in args.jobs_root.iterdir() if p.is_dir() and (p/"cfg.py").is_file())
    if not jobs: raise ValueError("No jobs found")
    for job in jobs:
        try:
            p=replay(job,args.output_root/job.name/"bim_package.json",args.timeout)
            row={"building":job.name, "status":p["readiness"]["status"], "counts":p["readiness"]["counts"], "finding_codes":sorted({f["code"] for f in p["readiness"]["findings"]})}
        except Exception as ex:
            row={"building":job.name,"status":"failed","error":str(ex)}
        rows.append(row); print(json.dumps(row),flush=True)
    atomic_json(args.output_root/"replay_summary.json",rows)
    return 1 if any(r["status"] in ("failed","invalid") or (args.require_ready and r["status"]!="ready") for r in rows) else 0


if __name__=="__main__":
    raise SystemExit(main())
