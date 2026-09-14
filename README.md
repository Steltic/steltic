# Steltic

A free, open-source AISC 360/341 steel-design agent that runs locally. Paste a design brief and
watch the agent build an OpenSees model + a stamped-style HTML report + an interactive 3D viewer —
**bringing your own LLM** (your API base-url + key, held in memory only, never written to disk).

```
browser ──▶ FastAPI app (localhost) ──▶ your LLM (key in app memory, never stored)
                 │  parses tool calls
                 ▼
           sandbox executor (run_python only) — Docker when available
                 │
           steel engine + OpenSees ─▶ report.html + viewer_3d.html
```

> **Not for construction.** Every output is produced by your own AI model, may be incomplete or
> incorrect, and must be independently checked and sealed by a licensed professional engineer before
> any use for design, construction, or permitting. See [DISCLAIMER.md](DISCLAIMER.md).

## Try the hosted version of Steltic for free using our servers [stelticai.com](https://stelticai.com)

## Install & run

With [uv](https://docs.astral.sh/uv/) (recommended — no Python setup needed):

```bash
uv tool install --python 3.12 steltic
steltic                      # starts on http://127.0.0.1:8000 and opens your browser
```

The `--python 3.12` matters: openseespy (the analysis engine) ships compiled binaries for
**Python 3.10–3.12 only**, and uv ignores the package's upper Python bound — without the flag it
may install onto a newer interpreter where the engine can't load (Steltic will tell you and refuse
to start). uv downloads 3.12 automatically if you don't have it. Or with pipx (which respects the
bound): `pipx install --python 3.12 steltic`. The install includes the full engine
(openseespy/numpy/scipy/matplotlib, ~400MB) so designs run out of the box without Docker. Or from
a checkout:

```bash
git clone <this repo> && cd steltic
python -m venv .venv && . .venv/bin/activate
pip install -e .
./run_local.sh               # http://localhost:8000
```

First run: open **Settings**, enter your provider's **API base URL** (any OpenAI-compatible
endpoint — OpenRouter, vLLM, Together, Fireworks, OpenAI, Anthropic…), your **API key**, and a
**model** id. Then paste a brief (stories, bays & spacings, loads, seismic, system) — or pick one of
the 36 built-in example briefs — and click **Design building**.

Offline smoke test: set Model to `MOCK` — it drives the whole pipeline (sandbox, engine, OpenSees,
report) with no LLM.

**Windows note:** if launching fails with *"Application Control policy has blocked this file"* or
*"os error 4551"*, Windows **Smart App Control** is blocking uv's downloaded (unsigned) Python.
Fix: install the signed python.org build and point uv at it —

```powershell
winget install Python.Python.3.12       # then open a new terminal
uv tool uninstall steltic
$env:UV_PYTHON_PREFERENCE = "only-system"
uv tool install --python 3.12 steltic
```

## Sandbox

The agent's Python only ever executes in a sandbox. `EXECUTOR` (env / `.env`) picks the mode:

| value        | isolation | notes |
|--------------|-----------|-------|
| `auto`       | Docker if available, else subprocess | default |
| `docker`     | container per run: no network, read-only fs, non-root, cpu/mem caps | build once: `./sandbox_image/build.sh` |
| `subprocess` | none (child process with rlimits) | needs `pip install openseespy numpy scipy matplotlib`; fine for local single-user use |

Steltic binds to 127.0.0.1 and has **no authentication** — don't expose the port. Designs are saved
under your OS user-data dir (e.g. `%LOCALAPPDATA%\Steltic`, `~/.local/share/Steltic`); override
with `DATA_DIR`.

## Engineering-standards, OpenSees & design-examples RAG (highly recommended)

The hosted version of Steltic at [stelticai.com](https://stelticai.com) grounds the agent with RAG
vector databases of AISC 360, 341 and 358 (2022), the AISC Steel Construction Manual Design
Examples (V16.0, 168 worked examples), an examples database of validated OpenSees models, and two
databases of the OpenSees documentation. The specifications and design examples ground the LLM in
the current procedures; the OpenSees databases help it build the model — and when OpenSees throws
errors, resolve them. Steltic's performance has been validated **with** these databases and will
likely reduce without them.

For copyright reasons the vector databases of AISC 360, 341 and 358 are **not** open-sourced in
this project. The rest are freely downloadable from this repo's
[Releases page](https://github.com/Steltic/steltic/releases) as portable `.jsonl.gz` dumps
(pre-embedded; see [rag_v2/README.md](rag_v2/README.md) for loading them into your own Qdrant):
`steel_design_examples` (168 original worked Q&A covering the AISC Design Examples scope),
`opensees_buildings_3d` (40 validated 3D building models), `opensees_building_templates`, and the
two OpenSees documentation sets.

Two options:

1. **None (default — not recommended).** Leave `RAG_API_URL` empty — the agent relies on its own
   cited AISC knowledge. Good models do respectably; grounded runs are better.
2. **Bring your own server.** Run any server exposing the small API described in
   [rag_v2/README.md](rag_v2/README.md): load the downloadable OpenSees databases, and build the
   standards collections from your own licensed copies with the `rag_v2/` chunking + ingestion
   starter kit. Then put the connection in the environment (or a `.env` next to where you launch):
   `RAG_API_URL=http://your-server:8080/query` and, if your server enforces one, `RAG_API_TOKEN=...`.

## Feedback from the Nonlinear module (SNL)

The Nonlinear module can hand a re-design back through *Continue* once its Chapter 16 / pushover / DDM run is
complete (see `steltic_nonlinear/docs/README_feedback.md`). Three things in this repository exist for that:

- `cfg['drift_relief_16_1_2']` — the ASCE 7-22 §16.1.2 relief block SNL writes when a Chapter 16 analysis lets the
  §12.12.1 drift limits be relaxed (Risk Category I–III only). `steel_engine/preflight.py` and `consistency.py` accept
  a relaxed `drift_limit` only with a complete block, never for Risk Category IV, and never above the Chapter 16
  limit; Chapter 8 of the report states the relief and that the design is provisional until Chapter 16 is re-run.
- `POST /api/restore/<building>?archive=1` — moves an existing job aside as `<building>__<timestamp>` before the
  restore (what a fresh run already does), so a promoted candidate never silently overwrites a design of record;
  `viewer_3d.html` is now part of the restore whitelist.
- `design/design_of_record.json` — written by SNL when the user promotes a verified candidate; the report's Chapter 1
  basis table shows it. The agent contract (`contract/AGENT_START.md`, *Feedback from the Nonlinear module*) says how
  the three loop briefs must be applied (`capacity_design.SCWB.by_story`, `capacity_design.panel_zone.by_joint`).

`tests/test_drift_relief.py` covers the rules (`python -m pytest tests -q`).

## Repo map

`steltic/` FastAPI app + agent loop + sandbox executors · `steel_engine/` OpenSees modelling,
design pipeline, consistency checks, report + 3D viewer · `contract/` the agent's working contract
and references · `frontend/` vanilla JS UI · `sandbox_image/` Docker sandbox image ·
`test_buildings/` 50+ example briefs (steel + CFS) with assessment rubrics · `rag_v2/`, `rag_update/`
build-your-own-RAG starter kit.

## License

MIT — see [LICENSE](LICENSE), [NOTICE](NOTICE) (third-party data notes) and
[DISCLAIMER.md](DISCLAIMER.md) (engineering disclaimer; also shown in-app at `/terms`).
