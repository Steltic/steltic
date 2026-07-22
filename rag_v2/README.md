# Build-your-own engineering-standards RAG

Steltic's agent calls one small HTTP API for standards grounding. Anything that speaks this API
works — these scripts are the starter kit used to build the hosted Steltic RAG (embedding +
[Qdrant](https://qdrant.tech/) vector store), for use with **your own licensed copies** of the
standards. AISC/ASCE specification text is copyrighted: do not redistribute your chunk stores.

## The API Steltic expects

`POST $RAG_API_URL` (e.g. `http://127.0.0.1:8080/query`), JSON body:

```json
{"query": "F2 flexural strength compact section",
 "collection": "engineering_standards_A360",
 "top_k": 5,
 "clause": "F2",        // optional exact-clause filter
 "chapter": "F"}        // optional chapter filter
```

Optional `Authorization: Bearer $RAG_API_TOKEN`. Response: a JSON object with a `results` list
(each result's text is shown to the agent; include the clause code and text in each chunk).

Collections the agent queries: `engineering_standards_A360` (primary spec),
`engineering_standards_A341` (seismic), `engineering_standards_A358` (connections),
`engineering_standards_A303`, `steel_design_examples` (worked examples), plus OpenSees docs
(`opensees_docs`-style collections) and `opensees_buildings_3d` (validated reference builds).

## Downloadable OpenSees databases

The five OpenSees collections (`opensees_buildings_3d`, `opensees_examples`,
`opensees_building_templates`, `openseespy_documentation`, `opensees_documentation`) are published
as `.jsonl.gz` dumps on the repo's [Releases page](https://github.com/Steltic/steltic/releases).
Each dump carries its vectors AS-IS (embedded with **nomic-embed-text**, 768-dim) — nothing is
re-embedded on load, but your query server must embed queries with the same model for the vectors
to match. Load into a local Qdrant with:

    venv/bin/python load_opensees.py osdump/*.jsonl.gz

## Scripts

- `chunk_v2.py` — clause-aware chunking of specification text (keeps equations with their clause,
  tags chunks with clause/chapter for the pinpoint filters).
- `chunk_examples.py` / `reingest_examples.py` — chunk + ingest worked design examples.
- `reingest_v2.py` — embed + upsert spec chunks into the vector store.
- `dump_opensees.py` / `load_opensees.py` — export/import the OpenSees docs collection.
- `../rag_get_shim.py` — tiny GET→POST relay, handy for tools that can only make GET requests.
- `../rag_update/*.jsonl` — validated 3D reference-building summaries (original content, one JSON
  object per line: `id` + `text`) for an `opensees_buildings_3d` collection; embed + upsert each
  line like any other chunk.
