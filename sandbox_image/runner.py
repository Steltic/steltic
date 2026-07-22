"""Entry point INSIDE the sandbox container. Runs one model-written script with the steel engine
importable, an internal wall-clock alarm (defence in depth on top of the host timeout), and Agg
matplotlib. Whatever the script writes under cwd (=/jobs/<building>) is the result the host reads back.
"""
import os, sys, runpy, signal

TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "900"))

def _bail(signum, frame):
    print(f"\n[sandbox] hard timeout after {TIMEOUT}s", file=sys.stderr)
    os._exit(124)

def main():
    if len(sys.argv) < 2:
        print("usage: runner.py <script.py>", file=sys.stderr); sys.exit(2)
    script = sys.argv[1]
    os.environ.setdefault("MPLBACKEND", "Agg")
    try:
        signal.signal(signal.SIGALRM, _bail)
        signal.alarm(TIMEOUT)
    except Exception:
        pass
    runpy.run_path(script, run_name="__main__")

if __name__ == "__main__":
    main()
