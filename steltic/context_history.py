"""Lossless storage for old, large tool outputs in long design conversations."""
import hashlib
import json
import os
from pathlib import Path
import uuid


def archive_tool_results(messages, job_dir, keep=16, min_chars=4000):
    """Keep recent outputs inline; retain older large results in the same job.

    User instructions, assistant calls and their IDs are never changed. A failed
    archive leaves its original output in context. No model/provider settings
    or engineering artifacts are modified.
    """
    if keep < 1:
        return 0
    eligible = [i for i, m in enumerate(messages)
                if m.get("role") == "tool"
                and m.get("name") in ("read_file", "run_python", "list_files", "activity_summary")]
    root = Path(job_dir).resolve()
    archived = 0
    for i in eligible[:-keep]:
        message = messages[i]
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= min_chars:
            continue
        raw = content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        relative = "context_tool_results/" + digest + ".txt"
        destination = root / relative
        temporary = None
        try:
            if not destination.resolve().is_relative_to(root):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != raw:
                    continue  # preserve a changed archive; never point at wrong data
            else:
                temporary = destination.parent / ("tmp-" + uuid.uuid4().hex[:12])
                temporary.write_bytes(raw)
                os.replace(temporary, destination)
            if destination.read_bytes() != raw:
                continue
        except OSError:
            continue
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        pointer = dict(archived_tool_result=True, path=relative, sha256=digest,
                       characters=len(content),
                       hint="Historical tool output retained verbatim in this job. Read current job files for current state. If needed, use run_python to load this UTF-8 file and print only the relevant portion.")
        try:
            value = json.loads(content)
            if isinstance(value, dict) and type(value.get("returncode")) is int:
                pointer["returncode"] = value["returncode"]
        except (ValueError, TypeError):
            pass
        messages[i] = dict(message, content=json.dumps(pointer, ensure_ascii=True))
        archived += 1
    return archived

