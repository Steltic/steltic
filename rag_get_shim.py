# rag_get_shim.py - GET -> POST relay so Claude's web-fetch (GET-only) can query the RAG.
# Run on the laptop alongside the IAP tunnel (RAG on localhost:8080), then point
# cloudflared at this shim:  cloudflared tunnel --url http://localhost:8081
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, os, urllib.request, urllib.parse

RAG = "http://localhost:8080/query"
# The IAP tunnel arrives at the VM from Google's range (not localhost), so when the VM enforces
# RAG_API_TOKEN the shim must send it:  set RAG_API_TOKEN=...  before launching.
TOKEN = os.environ.get("RAG_API_TOKEN", "")

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        payload = {
            "query": q.get("query", [""])[0],
            "collection": q.get("collection", ["engineering_standards_A360"])[0],
            "top_k": int(q.get("top_k", ["5"])[0]),
        }
        for k in ("clause", "chapter"):
            if k in q:
                payload[k] = q[k][0]
        hdrs = {"Content-Type": "application/json"}
        if TOKEN:
            hdrs["Authorization"] = "Bearer " + TOKEN
        req = urllib.request.Request(RAG, data=json.dumps(payload).encode(), headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            code = 200
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            code = 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass

if __name__ == "__main__":
    print("GET->POST RAG shim on http://127.0.0.1:8081  (relaying to %s)" % RAG)
    HTTPServer(("127.0.0.1", 8081), H).serve_forever()
