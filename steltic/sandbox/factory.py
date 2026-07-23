"""Pick the executor from config.EXECUTOR (auto|docker|subprocess)."""
from .. import config
from .docker_exec import DockerExecutor
from .subprocess_exec import SubprocessExecutor


def _docker():
    return DockerExecutor(config.SANDBOX_IMAGE, config.SANDBOX_MEM, config.SANDBOX_CPUS, config.SANDBOX_PIDS)

def _sub(log=print):
    # The subprocess executor runs the engine in THIS interpreter's environment -- verify the
    # engine deps are importable NOW so a broken install fails loudly at boot, not mid-design.
    import importlib.util
    missing = [m for m in ("openseespy", "numpy", "scipy", "matplotlib")
               if importlib.util.find_spec(m) is None]
    if missing:
        log(f"[sandbox] WARNING: missing engine packages: {', '.join(missing)} -- runs WILL fail. "
            f"Fix: uv tool install --force steltic   (or: pip install {' '.join(missing)})")
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
