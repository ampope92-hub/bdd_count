# Verifier image. Runs in its own container, sees only the paths declared in `artifacts`, and
# holds everything the agent never sees: both blocks, their exact counts and the export server
# that serves them. It compiles the collected D itself and runs the counter against servers it
# starts.

# The counter is started through tests/launch.c, which lets it start no other program. Built
# statically in a throwaway stage, so no C compiler from here reaches the final image.
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb AS launcher
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY launch.c /tmp/launch.c
RUN gcc -O2 -static -Wall -o /tool_launch /tmp/launch.c

# The same LDC the agent's box carries, from the same release tarball, checked against the same
# SHA-256, with the same std.numeric removal.
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

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# gcc and libc6-dev are what ldc2 links through.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ldc /opt/ldc /opt/ldc

# Verifier tooling from a hash-pinned lockfile. Nothing is installed at trial time.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.txt && rm -f /tmp/requirements.txt

COPY --from=launcher /tool_launch /usr/local/bin/tool_launch
RUN useradd --create-home --uid 10001 --shell /bin/bash runner

# Landing directory for the declared artifact.
RUN mkdir -p /app/bdd && chown -R runner:runner /app && chmod 1755 /app

# The blocks, their counts and the server. Root only, and never in any container the agent
# reaches.
COPY data /opt/truth
COPY golden /opt/truth/golden
COPY golden_ref /opt/truth/golden_ref
COPY synth /opt/synth
RUN chmod 700 /opt/truth /opt/synth

# Separate mode does not upload tests/ into the image, so the image owns /tests itself.
COPY . /tests/

WORKDIR /app/bdd
