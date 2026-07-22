"""
dump_opensees.py -- export OpenSees RAG collections from a working Qdrant to portable .jsonl.gz files,
so they can be point-re-upserted into another Qdrant (the server) WITHOUT Qdrant snapshots
(snapshots of these were corrupting on restore with a "Wal error").

Run on the box whose Qdrant already HAS the collections (your local box), with the RAG venv:

    cd ~/rag                                      # wherever qdrant_client is importable
    venv/bin/python dump_opensees.py              # default: the 3 small building collections
    venv/bin/python dump_opensees.py --with-docs  # also the 2 big *_documentation collections
    venv/bin/python dump_opensees.py opensees_buildings_3d   # explicit list

Each collection -> ./osdump/<collection>.jsonl.gz . The first line is metadata
{"_meta": {collection,size,distance,vector_name,points}}, then one JSON point per line
{"id","vector","payload"}. Vectors are copied AS-IS (already Nomic-768), so NOTHING is re-embedded
and there is no model/snapshot dependency.
"""
import os, sys, json, gzip
from qdrant_client import QdrantClient

SMALL = ["opensees_buildings_3d", "opensees_examples", "opensees_building_templates"]
DOCS  = ["openseespy_documentation", "opensees_documentation"]
OUT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "osdump")


def _dist_str(d):
    return getattr(d, "value", None) or str(d)


def _vec_params(info):
    """Return (size, distance_str, vector_name|None). Handles default (unnamed) and named vectors."""
    v = info.config.params.vectors
    if isinstance(v, dict):                         # named vectors -> take the single/default one
        name = next(iter(v))
        return v[name].size, _dist_str(v[name].distance), name
    return v.size, _dist_str(v.distance), None      # default (unnamed) vector


def main(args):
    explicit = [a for a in args if not a.startswith("--")]
    colls = explicit if explicit else SMALL + (DOCS if "--with-docs" in args else [])
    os.makedirs(OUT, exist_ok=True)
    c = QdrantClient(host=os.environ.get("QDRANT_HOST", "localhost"),
                     port=int(os.environ.get("QDRANT_PORT", "6333")))
    for coll in colls:
        if not c.collection_exists(coll):
            print(f"[skip] {coll} -- not found on this Qdrant"); continue
        size, dist, vname = _vec_params(c.get_collection(coll))
        path = os.path.join(OUT, coll + ".jsonl.gz")
        n, off = 0, None
        with gzip.open(path, "wt") as f:
            f.write(json.dumps({"_meta": {"collection": coll, "size": size,
                                          "distance": dist, "vector_name": vname}}) + "\n")
            while True:
                pts, off = c.scroll(coll, limit=256, with_payload=True, with_vectors=True, offset=off)
                for p in pts:
                    f.write(json.dumps({"id": p.id, "vector": p.vector, "payload": p.payload}) + "\n")
                    n += 1
                if off is None:
                    break
        mb = os.path.getsize(path) / 1e6
        print(f"[ok] {coll}: {n} points, {size}-dim {dist} -> {path} ({mb:.1f} MB)")
    print("\ndone. scp the ./osdump/*.jsonl.gz you want + load_opensees.py to the server, then run load there.")


if __name__ == "__main__":
    main(sys.argv[1:])
