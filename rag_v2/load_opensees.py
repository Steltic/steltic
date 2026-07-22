"""
load_opensees.py -- import the .jsonl.gz dumps produced by dump_opensees.py into THIS box's Qdrant
(a point re-upsert). Recreates each collection fresh and upserts the points, copying vectors AS-IS.
This bypasses Qdrant snapshots entirely (the path that was failing with a "Wal error").

Run on the SERVER, with the RAG venv, after scp-ing the osdump/ files here:

    venv/bin/python load_opensees.py osdump/*.jsonl.gz
    venv/bin/python load_opensees.py osdump/opensees_buildings_3d.jsonl.gz   # one at a time

Idempotent: an existing same-name collection is dropped and rebuilt. Targets localhost:6333.
"""
import sys, json, gzip, glob
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

DIST = {"cosine": Distance.COSINE, "euclid": Distance.EUCLID,
        "dot": Distance.DOT, "manhattan": Distance.MANHATTAN}
BATCH = 256


def load(path, c):
    with gzip.open(path, "rt") as f:
        meta = json.loads(f.readline())["_meta"]
        coll = meta["collection"]
        size = int(meta["size"])
        dist = DIST[meta["distance"].lower()]
        vname = meta.get("vector_name")
        vcfg = {vname: VectorParams(size=size, distance=dist)} if vname else VectorParams(size=size, distance=dist)
        if c.collection_exists(coll):
            c.delete_collection(coll)
        c.create_collection(coll, vectors_config=vcfg)
        batch, n = [], 0
        for line in f:
            o = json.loads(line)
            batch.append(PointStruct(id=o["id"], vector=o["vector"], payload=o["payload"]))
            if len(batch) >= BATCH:
                c.upsert(coll, points=batch); n += len(batch); batch = []
        if batch:
            c.upsert(coll, points=batch); n += len(batch)
    got = c.get_collection(coll).points_count
    print(f"[ok] {coll}: upserted {n} points, collection now reports {got} ({size}-dim {meta['distance']})")


def main(args):
    paths = []
    for a in args:
        paths += glob.glob(a)
    if not paths:
        print("usage: load_opensees.py osdump/*.jsonl.gz"); return
    c = QdrantClient(host="localhost", port=6333)
    for p in sorted(set(paths)):
        load(p, c)
    print("\ndone. verify via the query server, e.g.:\n"
          "  curl -s -X POST localhost:8080/query -H 'Content-Type: application/json' \\\n"
          "    -d '{\"query\":\"3 storey special moment frame\",\"collection\":\"opensees_buildings_3d\",\"top_k\":3}'")


if __name__ == "__main__":
    main(sys.argv[1:])
