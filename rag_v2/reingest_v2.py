"""
reingest_v2.py -- re-chunk the engineering-standards markdown with the clause-aware chunker
(chunk_v2.py) and re-embed + upsert into Qdrant, REPLACING the engineering_standards_* collections.

Run with the RAG deployment's venv (it has sentence-transformers + qdrant_client). From the live
deployment dir (the one with venv/, models/, qdrant on :6333, and where you've placed chunk_v2.py +
md/ next to this file):

    cd ~/rag
    venv/bin/python reingest_v2.py             # all standards in ./md
    venv/bin/python reingest_v2.py A360 A341   # just these

It reads ./md/<STD>.md, recreates each collection fresh, and upserts the new chunks. The agent keeps
querying the same collection names (engineering_standards_A360, ...) and gets the better chunks.

Embeddings are computed WITHOUT a prefix, to match how local_api.py embeds queries
(`model.encode(query)`). Switching BOTH sides to Nomic's "search_document:" / "search_query:" prefixes
would improve retrieval further; left off here so the live query path stays consistent.
"""
import os, sys, uuid, hashlib, gc, re
HERE = os.path.dirname(os.path.abspath(__file__))
_REPEAT = re.compile(r'(\S)\1{3,}')   # 4+ repeats of a non-space char (e.g. long table-separator dashes)
def _embed_text(t):
    """Sanitized, length-capped text used ONLY for the embedding vector (full text stays in the payload).
    Collapses pathological repeated-char runs that can crash/explode the tokenizer."""
    return _REPEAT.sub(r'\1\1\1', t)[:3500]
sys.path.insert(0, HERE)
import chunk_v2
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

MODEL_NAME = "nomic-ai/nomic-embed-text-v1"
MD_DIR     = os.path.join(HERE, "md")
MODEL_CACHE = os.environ.get("MODEL_CACHE", os.path.join(HERE, "models"))   # set MODEL_CACHE=/path/to/existing/models to reuse a prior Nomic download
EMBED_BATCH = int(os.environ.get("EMBED_BATCH", "8"))   # override up on a big box, e.g. EMBED_BATCH=64 on 32 GB (8 is safe for ~4 GB)
DISPLAY = {
    "A303": "AISC 303-22", "A341": "AISC 341-22", "A342": "AISC 342-22", "A358": "AISC 358-22",
    "A360": "AISC 360-22", "A370": "AISC 370-22", "AS100": "AISI S100", "AS202": "AISI S202",
    "AS220": "AISI S220", "AS230": "AISI S230", "AS240": "AISI S240", "AS250": "AISI S250",
    "AS400": "AISI S400",
}


def main(only=None):
    print("loading embedding model ...", flush=True)
    model = SentenceTransformer(MODEL_NAME, trust_remote_code=True, cache_folder=MODEL_CACHE)
    model.max_seq_length = min(getattr(model, "max_seq_length", 2048) or 2048, 2048)  # cap activation size
    client = QdrantClient(host=os.environ.get("QDRANT_HOST", "localhost"),
                          port=int(os.environ.get("QDRANT_PORT", "6333")))
    if only:                                  # preserve the order given on the command line
        codes = [c for c in only if os.path.exists(os.path.join(MD_DIR, c + ".md"))]
    else:
        codes = sorted(f[:-3] for f in os.listdir(MD_DIR) if f.endswith(".md"))
    grand = 0
    for code in codes:
        coll = "engineering_standards_" + code
        md = open(os.path.join(MD_DIR, code + ".md"), encoding="utf-8").read()
        chunks = chunk_v2.chunk_markdown(md, code, DISPLAY.get(code, code))
        print(f"[{code}] {len(chunks)} chunks -> embedding ...", flush=True)
        vecs = model.encode([_embed_text(c["text"]) for c in chunks], show_progress_bar=False, batch_size=EMBED_BATCH)
        if client.collection_exists(coll):
            client.delete_collection(coll)
        client.create_collection(coll, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
        points = [
            PointStruct(id=str(uuid.UUID(hex=hashlib.md5(f"{code}-v2-{i}".encode()).hexdigest())),
                        vector=v.tolist(), payload=c)
            for i, (c, v) in enumerate(zip(chunks, vecs))
        ]
        for j in range(0, len(points), 256):
            client.upsert(coll, points=points[j:j + 256])
        grand += len(points)
        print(f"[{code}] upserted {len(points)} points -> {coll}", flush=True)
        del vecs, points, chunks, md; gc.collect()
    print(f"done. {grand} chunks across {len(codes)} collections.", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or None)
