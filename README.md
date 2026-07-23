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

## Install & run

With [uv](https://docs.astral.sh/uv/) (recommended — no Python setup needed):

```bash
uv tool install steltic
steltic                      # starts on http://127.0.0.1:8000 and opens your browser
```

Or with pipx: `pipx install steltic`. Steltic needs **Python 3.10–3.12** (openseespy's compiled
binaries don't support newer interpreters yet) — uv picks a compatible one automatically; with
pipx/pip, point them at a 3.12 interpreter. Or from a checkout:

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

Three options:

1. **None (default — not recommended).** Leave `RAG_API_URL` empty — the agent relies on its own
   cited AISC knowledge. Good models do respectably; grounded runs are better.
2. **Bring your own server.** Run any server exposing the small API described in
   [rag_v2/README.md](rag_v2/README.md): load the downloadable OpenSees databases, and build the
   standards collections from your own licensed copies with the `rag_v2/` chunking + ingestion
   starter kit. Then put the connection in the environment (or a `.env` next to where you launch):
   `RAG_API_URL=http://your-server:8080/query` and, if your server enforces one, `RAG_API_TOKEN=...`.
3. **Hosted Steltic RAG.** The maintained, pre-built full set (specs + design examples + OpenSees).
   To get access, use the contact details at [stelticai.com](https://stelticai.com), then set the
   provided `RAG_API_URL` + `RAG_API_TOKEN` in your environment.

## Repo map

`steltic/` FastAPI app + agent loop + sandbox executors · `steel_engine/` OpenSees modelling,
design pipeline, consistency checks, report + 3D viewer · `contract/` the agent's working contract
and references · `frontend/` vanilla JS UI · `sandbox_image/` Docker sandbox image ·
`test_buildings/` 50+ example briefs (steel + CFS) with assessment rubrics · `rag_v2/`, `rag_update/`
build-your-own-RAG starter kit.

## License

MIT — see [LICENSE](LICENSE), [NOTICE](NOTICE) (third-party data notes) and
[DISCLAIMER.md](DISCLAIMER.md) (engineering disclaimer; also shown in-app at `/terms`).
