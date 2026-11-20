ARG BASE_IMAGE=python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3
FROM ${BASE_IMAGE}

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis \
    FIN_AUDIT_REQUIRE_SECURE=1

# Dependency preparation happens at build time. Runtime never installs packages.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libseccomp2=2.5.4-1+deb12u1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock /app/
COPY docker/build_sandbox.py /app/docker/build_sandbox.py
RUN python /app/docker/build_sandbox.py --install-test-dependencies

COPY . /app/
RUN python /app/docker/build_sandbox.py \
    --root /sandbox \
    --worker /app/src/fin_audit/agent_worker.py

CMD ["python", "-m", "fin_audit.cli", "verify", "--stage", "5", "--profile", "secure", "--output", "/tmp/fin-audit-release"]
