# Agent image: the LDC D compiler and the counter's directory. The block itself is not here; it
# lives in the synthesis server's own image (Dockerfile.synth) and is only reachable over its
# export port. Nothing from tests/ or solution/ is copied in.

# LDC 1.41.0 from the project's release tarball, checked against its published SHA-256 before
# it is unpacked. std.numeric is taken out of the Phobos import tree (and out of std/package.d),
# so a program that imports it does not compile. Only ldc2 and ldmd2 are kept from bin/.
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb AS ldc
RUN python3 -c "import hashlib, urllib.request, tarfile; \
url = 'https://github.com/ldc-developers/ldc/releases/download/v1.41.0/ldc2-1.41.0-linux-x86_64.tar.xz'; \
urllib.request.urlretrieve(url, '/tmp/ldc.tar.xz'); \
h = hashlib.sha256(open('/tmp/ldc.tar.xz', 'rb').read()).hexdigest(); \
assert h == '4a439457f0fe59e69d02fd6b57549fc3c87ad0f55ad9fb9e42507b6f8e327c8f', h; \
tarfile.open('/tmp/ldc.tar.xz').extractall('/opt')" \
    && mv /opt/ldc2-1.41.0-linux-x86_64 /opt/ldc \
    && rm -f /tmp/ldc.tar.xz /opt/ldc/import/std/numeric.d \
    && sed -i '/^ std\.numeric,$/d' /opt/ldc/import/std/package.d \
    && find /opt/ldc/bin -type f ! -name ldc2 ! -name ldmd2 -delete \
    && rm -rf /opt/ldc/lib32 \
    && chmod -R go-w /opt/ldc

FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

# gcc and libc6-dev are what ldc2 links through.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        asciinema \
        ca-certificates \
        gcc \
        libc6-dev \
        tmux \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ldc /opt/ldc /opt/ldc
ENV PATH=/opt/ldc/bin:$PATH \
    SYNTH_HOST=127.0.0.1 \
    SYNTH_PORT=7300

WORKDIR /app/bdd
COPY app/bdd/ /app/bdd/
