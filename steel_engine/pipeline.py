"""
pipeline.py  --  the design entry point the agent runs. ONE straight-through call (NO user-review
pause). Do NOT hand-write report.html or your own model/design scripts.

    import pipeline
    name = "<the building name the user gave you>"
    res = pipeline.design_and_report(name, cfg)    # sanity -> DEMAND envelope -> figures -> report.html
    print(res)                                     # {model_valid, demands_written, figures, report_html, design_dir, root}

You DETERMINE the joints / base fixity explicitly and STATE them in the report, but you do NOT pause
to ask the user to approve the model. (build_and_preview(name, cfg) remains available as an OPTIONAL
self-review that builds just the 3 figures -- it is not a required hold.)

The framework computes the model, the ASCE 7 loads, the P-Delta analysis, and the per-member
DEMANDS + the report scaffold. It computes NO AISC 360 capacity: YOU query the RAG, derive every
capacity/D-C yourself, and write them into calc_package.json. All outputs go to the building's
solution folder: steel_builder/<name>/ (design/, figs/, report.html).

Unusual geometry / non-rigid joints: the parametric builder makes a rectangular grid of rigid
elasticBeamColumn members. To model anything else (custom nodes, sloped roofs, per-member moment
releases, etc.), set cfg["custom_build"] = a function custom_build(cfg, transf) that builds the
OpenSees model and returns the standard info dict {cm, present, z, NF, ele:[(tag,kind,sec,n1,n2)]}
using engine3d.ntag(i,j,k)/mtag(k); the whole pipeline then runs on your model unchanged.
"""
import os, sys, subprocess, copy

_HERE = os.path.dirname(os.path.abspath(__file__))     # .../engine
_REPO = os.path.dirname(_HERE)                          # repo root (steel_builder)
for _p in (_HERE, _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import engine3d as E


def _root(name):
    """Per-building solution folder -- ALL outputs (cfg, design, figs, report) land here.
    Routed to the removable jobs folder via STEEL_BUILDER_JOBS (set by the MCP server) so a fresh
    agent cannot see prior jobs; falls back to the repo root for standalone/dev use."""
    base = os.environ.get("STEEL_BUILDER_JOBS") or _REPO
    return os.path.join(base, name)


def build_and_preview(name, cfg=None):
    """OPTIONAL self-review: build the OpenSees model + the 3 reviewer figures and return the figure
    paths + modelling assumptions. NOT a required user hold -- you may inspect the figures yourself,
    but proceed straight to design_and_report (just STATE the joints explicitly in the report)."""
    if cfg is not None:
        E.CFG[name] = copy.deepcopy(cfg)   # freeze the analysed model: the report renders exactly this cfg
    if name not in E.CFG:
        raise SystemExit("cfg '%s' is not registered -- pass cfg=<your building dict>." % name)
    c = E.CFG[name]
    root = _root(name); os.makedirs(root, exist_ok=True)
    try:
        import plot_model as PM
        figs = PM.figures(name, os.path.join(root, "figs"))
    except Exception as ex:
        figs = "figures failed: %s" % ex
    base = c.get("base", "fixed")
    has_releases = bool(c.get("releases"))
    if c.get("custom_build"):
        joints = ("CUSTOM model -- you defined the nodes/elements and any joint releases yourself in "
                  "custom_build(cfg, transf).")
    elif has_releases:
        joints = ("Beam-to-column and brace joints are RIGID / continuous EXCEPT the per-member moment "
                  "releases you set in cfg['releases'] (pinned / simple-shear connections at those "
                  "members). Interior gravity framing leans only if cfg['lean_gravity'] is set.")
    else:
        joints = ("ALL beam-to-column and brace joints are RIGID / continuous (elasticBeamColumn) -- "
                  "there are NO per-member moment releases. Interior gravity framing leans only if "
                  "cfg['lean_gravity'] is set. (Use cfg['releases'] for pinned / simple-shear joints.)")
    return {"name": name, "root": root, "figures": figs,
            "base_fixity": base, "joint_assumption": joints, "joints_have_releases": has_releases,
            "NOTE": ("Joint fixity is a KEY modelling decision -- state it EXPLICITLY in the report. "
                     "Record exactly what you made the base joints (%s) and the internal member "
                     "connections (see joint_assumption) and whether each came from the user's information "
                     "or is a default you chose. You do NOT pause for the user -- set "
                     "cfg['releases'] / cfg['base'] / cfg['lean_gravity'] or a custom_build as needed and "
                     "call design_and_report." % base)}


def design_and_report(name, cfg=None, do_report=True):
    """Run the full design (no user-review pause): register cfg, run sanity -> DEMAND envelope ->
    figures -> HTML report, all in process, writing to the solution folder steel_builder/<name>.
    Computes NO AISC 360 capacity; the agent derives those from the RAG and fills calc_package.json."""
    if cfg is not None:
        E.CFG[name] = copy.deepcopy(cfg)   # freeze the analysed model: the report renders exactly this cfg
    if name not in E.CFG:
        raise SystemExit("cfg '%s' is not registered -- pass cfg=<your building dict>." % name)

    root = _root(name); os.makedirs(root, exist_ok=True)
    out = {"name": name, "root": root}
    # R22 preflight: cheap cfg lint BEFORE any solve (units, factors vs system, drift limit vs Ie,
    # height limits, model/diaphragm declarations). Findings print and return; ERRORs mean the cfg
    # is mis-declared -- fix them first rather than debugging analysis output.
    try:
        import preflight as _PF
        _pf = _PF.check(E.CFG.get(name))
        out["preflight"] = _pf
        print(_PF.render(_pf))
    except Exception as _pfe:
        out["preflight"] = [("WARN", "preflight failed: %s" % _pfe)]
    E.clear_caches()   # fresh per-run modal/elf memo so design + report share this run's solves

    # 1) engineering sanity-check suite
    r = E.report(name)
    out["model_valid"] = bool(r.get("allp"))
    import consistency as _CC                                   # early units/geometry heads-up (e.g. story heights in ft)
    out["geometry_warnings"] = _CC._geometry_issues(E.CFG.get(name))

    # 2) ASCE 7-22 LRFD combinations + per-member DEMAND envelope (NO capacities -- agent/RAG)
    import design_pipeline as DP
    out["demands_written"] = bool(DP.design(name, outdir=os.path.join(root, "design")))
    out["design_dir"] = os.path.join(root, "design")

    # 3) reviewer-grade figures (geometry / orientation / deformed)
    try:
        import plot_model as PM
        out["figures"] = PM.figures(name, os.path.join(root, "figs"))
    except Exception as ex:
        out["figures"] = "skipped (%s)" % ex

    # 3b) standalone OpenSees model files for the user to check independently:
    #     model_opensees.py (DYNAMIC -- mass/period/seismic) and model_static.py (STATIC -- gravity force diagrams)
    try:
        out["model_files"] = E.export_model(cfg, root, name=name)
    except Exception as ex:
        out["model_files"] = "skipped (%s)" % ex
    try:
        import static_model as SM
        out["model_static"] = SM.export_static_model(cfg, root, name=name)
    except Exception as ex:
        out["model_static"] = "skipped (%s)" % ex

    # 4) the HTML report (10-section + appendices) -> steel_builder/<name>/report.html
    if do_report:
        import report as RPT
        out["report_html"] = RPT.build_report(name, root=root)

    # End-of-run reminder the agent sees in the tool output, right before it replies to the user.
    out["NEXT_STEP"] = ("MANDATORY before you finish: (a) the model must be COMPLETE (model_complete must PASS -- every "
                        "element modelled, no missing floor beams) and you must run consistency.check(name) and reconcile "
                        "every flag (including a missing cfg.py); ALSO CONFIRM report Figure 2 (member orientation, "
                        "web/depth ticks) rendered -- if it reads 'not available', run plot_model.figures(name) and "
                        "re-render report.build_report before finishing; then (b) END YOUR REPLY with this closing note "
                        "to the user, VERBATIM (do not reword it and do not add other offers):\n"
                        "\"Several figures are OFF-by-default, to reduce the time to render the report. Once the design "
                        "is completed, ask me to generate these items and add them to the report (may take several "
                        "minutes to render). Want to try different lateral restraint locations or systems? Want to do "
                        "an optimization run to reduce member sizes? Tell me what you would like to change in the "
                        "building and I'm on it.\"\n"
                        "(For your own reference, NOT to be listed to the user unless they ask: the OFF-by-default "
                        "figures are cfg['force_diagrams'] (~20-30 s), cfg['force_summary'] (~20-30 s), "
                        "cfg['mode_figures'] (~12 s), cfg['deformed_shape_figure'], cfg['section_color_figure'] and "
                        "cfg['appendix_case_figures'] -- set the flag(s) and re-render report.build_report.) "
                        "(c) The report is ALREADY on the user\u2019s computer at jobs/<name>/report.html "
                        "(== C:\\...\\jobs\\<name>\\report.html); just give them that path -- do NOT copy it "
                        "anywhere or look for an \u2018outputs folder\u2019; the engine path IS their C: drive.")
    print("\n" + "=" * 72 + "\n>> NEXT STEP (do not skip): " + out["NEXT_STEP"] + "\n" + "=" * 72)
    return out


if __name__ == "__main__":
    for nm in (sys.argv[1:] or ["B02"]):
        print(design_and_report(nm))
