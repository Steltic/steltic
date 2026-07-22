"""Steltic -- a local web app that runs an AISC 360/341 steel-design agent.

Frontend (vanilla) -> FastAPI backend (agent loop) -> sandboxed run_python (Docker if available).
You bring your own LLM (base-url + API key, kept in memory only). Optional engineering-standards
RAG via RAG_API_URL. The steel engine lives in ../steel_engine and only ever runs in the sandbox.
"""
__version__ = "0.1.1"
