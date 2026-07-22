"""Executor interface + shared helpers. An executor takes model-written Python and runs it in
isolation, with ONLY the session's jobs/ directory writable and NO network / NO credentials."""
from dataclasses import dataclass
from pathlib import Path

# Refused outright (mirrors the desktop tool): the sandbox image is preconfigured; no installs / DLL loads.
BLOCKED_TOKENS = ("pip install", "pip3 install", "conda install", "ensurepip",
                  "add_dll_directory", "ctypes.cdll", "ctypes.windll")
_GUARD = "# ---- agent code (sandboxed: no network, no credentials, jobs/ is the only writable mount) ----\n"


def is_blocked(code: str) -> str | None:
    low = (code or "").lower()
    for t in BLOCKED_TOKENS:
        if t in low:
            return ("Package installation and manual DLL loading are DISABLED. The sandbox is preconfigured "
                    "(openseespy, numpy, matplotlib). If an import fails, state the exact error and stop.")
    return None


def prepare_script(code: str) -> str:
    return _GUARD + (code or "")


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False
    error: str = ""

    def to_tool_result(self, cap_out: int = 12000, cap_err: int = 4000) -> dict:
        if self.error and self.returncode == 0:
            return {"error": self.error}
        out = {"stdout": (self.stdout or "")[-cap_out:], "stderr": (self.stderr or "")[-cap_err:],
               "returncode": self.returncode}
        if self.timed_out:
            out["timed_out"] = True
        if self.error:
            out["error"] = self.error
        return out


class Executor:
    name = "base"

    def healthcheck(self) -> tuple[bool, str]:
        return True, ""

    def run(self, code: str, jobs_dir: Path, building: str, timeout: int) -> ExecResult:
        raise NotImplementedError
