"""HUB-01: the server the hub starts must refuse cross-site and DNS-rebound requests.

Before the guard, a page on any site could send a "simple" cross-site POST (text/plain body, no CORS
preflight) to 127.0.0.1:<port>/api/creds -- `await request.json()` ignores the Content-Type -- and
re-point the LLM endpoint at its own server; /api/run would then execute that "model's" run_python
on the user's PC. A hostname resolving to 127.0.0.1 (DNS rebinding) could even read the replies.
"""
import json, os, sys, tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="steltic-guard-test-"))
os.environ.setdefault("EXECUTOR", "subprocess")

from fastapi.testclient import TestClient   # noqa: E402
from steltic import main, session           # noqa: E402

EVIL_CREDS = json.dumps({"base_url": "https://attacker.example/v1", "api_key": "k", "model": "m"})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("STELTIC_HUB_TOKEN", raising=False)
    monkeypatch.delenv("STELTIC_ALLOWED_HOSTS", raising=False)
    return TestClient(main.app)


def _base_url():
    return (session.get_creds() or {}).get("base_url")


def test_cross_site_post_cannot_repoint_the_llm_endpoint(client):
    before = _base_url()
    for h in ({"origin": "https://evil.example"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"}):
        r = client.post("/api/creds", content=EVIL_CREDS, headers={"content-type": "text/plain", **h})
        assert r.status_code == 403, h
        assert _base_url() == before
    # /api/run and /api/stop are refused the same way, before the body is read
    r = client.post("/api/run", content="{}", headers={"content-type": "text/plain", "origin": "https://evil.example"})
    assert r.status_code == 403 and "cross-site" in r.json()["detail"]
    assert client.post("/api/stop", content="{}", headers={"origin": "https://evil.example"}).status_code == 403


def test_dns_rebinding_host_is_refused_even_for_reads(client):
    assert client.get("/api/me", headers={"host": "attacker.example"}).status_code == 403
    assert client.get("/api/me", headers={"host": "attacker.example:8410"}).status_code == 403
    assert client.get("/healthz", headers={"host": "127.0.0.1:8410"}).status_code == 200
    assert client.get("/healthz", headers={"host": "localhost:8410"}).status_code == 200
    assert client.get("/healthz", headers={"host": "[::1]:8410"}).status_code == 200


def test_own_page_hub_proxy_and_local_clients_still_work(client):
    good = json.dumps({"base_url": "http://127.0.0.1:9/v1", "api_key": "k", "model": "MOCK"})
    # the module's own page (same origin), the hub's page through its /m/ proxy, curl / another module
    for h in ({"origin": "http://127.0.0.1:8410", "sec-fetch-site": "same-origin"},
              {"origin": "http://localhost:8300", "sec-fetch-site": "same-origin"},
              {"origin": "http://[::1]:8410"},
              {}):
        r = client.post("/api/creds", content=good, headers={"content-type": "application/json", **h})
        assert r.status_code < 400, (h, r.text)
    # reads from another site stay readable (the browser hides the reply; nothing changes)
    assert client.get("/api/me", headers={"origin": "https://evil.example"}).status_code == 200


def test_hub_token_is_checked_when_sent(client, monkeypatch):
    monkeypatch.setenv("STELTIC_HUB_TOKEN", "s3cret")
    good = json.dumps({"base_url": "http://127.0.0.1:9/v1", "api_key": "k", "model": "MOCK"})
    r = client.post("/api/creds", content=good, headers={"x-steltic-hub-token": "s3cret"})
    assert r.status_code < 400
    r = client.post("/api/creds", content=good, headers={"x-steltic-hub-token": "guess"})
    assert r.status_code == 403 and "token" in r.json()["detail"]
    # a token never lifts the Host check
    assert client.get("/api/me", headers={"x-steltic-hub-token": "s3cret", "host": "attacker.example"}).status_code == 403
    # a server the hub did not start has no token: any token sent is wrong
    monkeypatch.delenv("STELTIC_HUB_TOKEN")
    assert client.post("/api/creds", content=good, headers={"x-steltic-hub-token": "s3cret"}).status_code == 403


def test_allowed_hosts_extend_the_loopback_names(client, monkeypatch):
    monkeypatch.setenv("STELTIC_ALLOWED_HOSTS", "workstation.lan, 192.168.1.5")
    assert client.get("/healthz", headers={"host": "workstation.lan:8000"}).status_code == 200
    assert client.get("/healthz", headers={"host": "192.168.1.5:8000"}).status_code == 200
    assert client.get("/healthz", headers={"host": "attacker.example"}).status_code == 403
