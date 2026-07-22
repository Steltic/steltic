"""
reingest_examples.py -- rebuild the `steel_design_examples` collection from the per-example markdown
(steel_egs/) using chunk_examples.py, then re-embed + upsert into Qdrant.

Run on the RAG VM's venv (has sentence-transformers + qdrant_client), with the steel_egs md placed
next to this file (or point EGS_DIR at it):

    cd ~/rag
    EGS_DIR=~/rag/steel_egs venv/bin/python reingest_examples.py

Parity with reingest_v2.py:
  * same model (nomic-embed-text-v1, 768-dim, cosine), embedded WITHOUT a prefix so it matches
    local_api.py's `model.encode(query)`;
  * same collection NAME (`steel_design_examples`) -> the app + agent contract are unchanged.

New here: keyword payload indexes on example_id / example_family / chapter / clauses, so a query can be
FILTERED to the right example family (near-deterministic retrieval) once local_api.py forwards a filter.
"""
import os, sys, uuid, hashlib, gc, re
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chunk_examples
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, PayloadSchemaType

MODEL_NAME  = "nomic-ai/nomic-embed-text-v1"
COLLECTION  = "steel_design_examples"
EGS_DIR     = os.environ.get("EGS_DIR", os.path.join(HERE, "steel_egs"))
MODEL_CACHE = os.environ.get("MODEL_CACHE", os.path.join(HERE, "models"))   # reuse the existing Nomic download
EMBED_BATCH = int(os.environ.get("EMBED_BATCH", "8"))                        # bump on a big box (e.g. 64)

_REPEAT = re.compile(r'(\S)\1{3,}')
def _embed_text(t):
    """Only the vector text: collapse pathological repeated-char runs, cap length (full text stays in payload)."""
    return _REPEAT.sub(r'\1\1\1', t)[:3500]


def main():
    print(f"reading examples from {EGS_DIR} ...", flush=True)
    chunks = chunk_examples.build_examples(EGS_DIR)
    if not chunks:
        print("no chunks -- check EGS_DIR points at the steel_egs *_solution.md files"); return
    exs = len(set(c["example_id"] for c in chunks))

    # Connect + FAIL FAST if Qdrant is down, BEFORE the (slow) embed -- so a down/OOM'd DB never wastes it.
    client = QdrantClient(host=os.environ.get("QDRANT_HOST", "localhost"),
                          port=int(os.environ.get("QDRANT_PORT", "6333")), timeout=120)
    try:
        client.get_collections()
    except Exception as e:
        print(f"ERROR: cannot reach Qdrant on :6333 ({e}).\n"
              f"Start/restart Qdrant (and stop local_api.py to free RAM on the 8 GB box), then re-run.")
        return

    print(f"{exs} examples -> {len(chunks)} chunks; loading embedding model ...", flush=True)
    model = SentenceTransformer(MODEL_NAME, trust_remote_code=True, cache_folder=MODEL_CACHE)
    model.max_seq_length = min(getattr(model, "max_seq_length", 2048) or 2048, 2048)
    vecs = model.encode([_embed_text(c["text"]) for c in chunks], show_progress_bar=True, batch_size=EMBED_BATCH)

    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(COLLECTION, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
    for field in ("example_id", "example_family", "chapter"):
        client.create_payload_index(COLLECTION, field, PayloadSchemaType.KEYWORD)
    client.create_payload_index(COLLECTION, "clauses", PayloadSchemaType.KEYWORD)   # list-of-keyword filter

    points = [
        PointStruct(id=str(uuid.UUID(hex=hashlib.md5(f"egs-{c['example_id']}-{c['part']}".encode()).hexdigest())),
                    vector=v.tolist(), payload=c)
        for c, v in zip(chunks, vecs)
    ]
    for j in range(0, len(points), 256):
        client.upsert(COLLECTION, points=points[j:j + 256])
    print(f"done. upserted {len(points)} chunks into {COLLECTION} (+ keyword indexes on "
          f"example_id/example_family/chapter/clauses).", flush=True)
    del vecs, points, chunks; gc.collect()


if __name__ == "__main__":
    main()
