# Sandbox image: the steel engine + OpenSees, run as a NON-root user with no network at runtime.
# Host launches one throwaway container per run_python call (steltic/sandbox/docker_exec.py).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 libgl1 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir openseespy numpy scipy matplotlib

# Engine + report code (read-only at runtime) and the entry point.
COPY steel_engine/ /sandbox/
COPY sandbox_image/runner.py /sandbox/runner.py
ENV PYTHONPATH=/sandbox \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mpl \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

# Non-root. The host also passes -u <uid>:<gid> so artifacts are owned by the app user.
RUN useradd -m -u 65532 sandbox
USER 65532
WORKDIR /jobs
# Docker mode overrides this with: runner.py <script>.
ENTRYPOINT ["python", "/sandbox/runner.py"]
