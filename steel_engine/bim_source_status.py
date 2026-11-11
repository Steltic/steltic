"""Evidence checks shared by HR packaging and report presentation."""
from pathlib import Path

try:
    from .bim_json import file_digest
except ImportError:
    from bim_json import file_digest


def consistency_errors(root, stamp, package_file="calc_package.json"):
    """A successful recorded check must agree with its counters and inputs."""
    root = Path(root)
    if not isinstance(stamp, dict):
        return ["Consistency result is not an object"]
    errors = []
    if stamp.get("result") != "PASS":
        errors.append("Recorded consistency result is not PASS")
    if type(stamp.get("n_issues")) is not int or stamp["n_issues"] != 0 or stamp.get("issues") != []:
        errors.append("Recorded consistency result requires n_issues=0 and issues=[]")
    for relative, key in (("cfg.py", "cfg_sha256"), ("design/" + package_file, "calc_package_sha256")):
        path = root / relative
        if not path.is_file() or stamp.get(key) != file_digest(path):
            errors.append("Missing or stale " + key + " for " + relative)
    return errors
