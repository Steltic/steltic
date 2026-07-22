"""`steltic` console entry point: start the local server and open the browser.

Usage:  steltic [--host 127.0.0.1] [--port 8000] [--no-browser]

All app settings come from environment variables (see .env.example in the repo). A .env file in
the current directory is loaded automatically (simple KEY=VALUE lines; no dependency needed).
"""
import argparse, os, pathlib, threading, webbrowser


def _load_dotenv(path: pathlib.Path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.split("#", 1)[0].strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def main():
    ap = argparse.ArgumentParser(prog="steltic", description="Steltic -- local AISC steel-design agent")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; do NOT expose publicly)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()

    _load_dotenv(pathlib.Path.cwd() / ".env")
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"[warn] binding to {args.host}: Steltic has NO authentication -- anyone who can reach "
              "this port can use your LLM key and see your designs. Keep it on 127.0.0.1 or put an "
              "authenticating reverse proxy in front.")

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"[steltic] serving on {url}")

    import uvicorn
    uvicorn.run("steltic.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
