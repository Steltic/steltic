"""Pick the executor from config.EXECUTOR (auto|docker|subprocess)."""
from .. import config
from .docker_exec import DockerExecutor
from .subprocess_exec import SubprocessExecutor


def _docker():
    return DockerExecutor(config.SANDBOX_IMAGE, config.SANDBOX_MEM, config.SANDBOX_CPUS, config.SANDBOX_PIDS)

def _sub(log=print):
    # The subprocess executor runs the engine in THIS interpreter's environment -- verify the
    # engine deps actually IMPORT now (present is not enough: openseespy's compiled module can
    # fail to load on an unsupported interpreter), so a broken install fails loudly at boot.
    problems = []
    for m in ("openseespy.opensees", "numpy", "matplotlib"):
        try:
            __import__(m)
        except Exception as e:
            problems.append(f"{m} ({type(e).__name__}: {str(e)[:90]})")
    if problems:
        log("[sandbox] WARNING: engine packages broken or missing -- runs WILL fail:")
        for p in problems:
            log(f"[sandbox]   - {p}")
        log("[sandbox] Fix: uv tool uninstall steltic && uv tool install --python 3.12 steltic")
    return SubprocessExecutor(config.STEEL_ENGINE)


def make_executor(log=print):
    mode = (config.EXECUTOR or "auto").lower()
    if mode == "docker":
        return _docker()
    if mode == "subprocess":
        return _sub(log)
    # auto: prefer Docker if it's actually usable, else fall back to the subprocess executor
    # (no container isolation -- fine for a local single-user install running your own designs).
    d = _docker()
    ok, why = d.healthcheck()
    if ok:
        log(f"[sandbox] using DockerExecutor (image={config.SANDBOX_IMAGE})")
        return d
    s = _sub(log)
    log(f"[sandbox] Docker unavailable ({why}); using SubprocessExecutor -- no container "
        f"isolation (fine for local single-user use).")
    return s
