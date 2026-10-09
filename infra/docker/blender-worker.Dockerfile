# Kinesis Blender worker image (Phase 6). One container runs one worker command for one job.
#
# Build with the worker directory as the context (it holds only main.py and the contract):
#   docker build -f infra/docker/blender-worker.Dockerfile -t kinesis-worker:4.5.14 blender/worker
#
# Run (what ContainerJobRunner does): the job directory is the only writable mount.
#   docker run --rm --network none --read-only --tmpfs /tmp -v <job_dir>:/work \
#     kinesis-worker:4.5.14 --command extract --spec /work/work/extract_scope.spec.json
FROM ubuntu:24.04

ARG BLENDER_VERSION=4.5.14
ARG BLENDER_SHA256=9ba871ff2ecd36526b77432745980b7e6664ecd0c7ca11c48849073dcfe06da3

# Headless Workbench rendering uses Mesa's software EGL/GL (llvmpipe): no GPU is needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils \
      libegl1 libegl-mesa0 libgl1 libgl1-mesa-dri libglx-mesa0 libgles2 \
      libxi6 libxkbcommon0 libxrender1 libxxf86vm1 libsm6 libx11-6 libxfixes3 libxext6 \
 && rm -rf /var/lib/apt/lists/*

# The pinned LTS, checksum-verified (ADR 0010). download.blender.org rejects curl's default UA.
RUN curl -fsSL -A "kinesis-build/1.0" \
      "https://download.blender.org/release/Blender4.5/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
      -o /tmp/blender.tar.xz \
 && echo "${BLENDER_SHA256}  /tmp/blender.tar.xz" | sha256sum -c - \
 && mkdir -p /opt/blender \
 && tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
 && rm /tmp/blender.tar.xz

COPY main.py /opt/kinesis/worker/main.py

# Unprivileged; the root filesystem is mounted read-only at run time, so Blender's user config
# goes to the /tmp tmpfs.
RUN useradd --uid 10001 --create-home --home-dir /home/worker worker
USER worker
ENV HOME=/tmp \
    XDG_CONFIG_HOME=/tmp/.config \
    XDG_CACHE_HOME=/tmp/.cache
WORKDIR /work

# Fixed argv (blender/worker/io_contract.md). Only `--command <enum> --spec <path>` varies.
ENTRYPOINT ["/opt/blender/blender", "--background", "--factory-startup", "-noaudio", \
            "/work/input/scene.blend", "--python-exit-code", "3", \
            "--python", "/opt/kinesis/worker/main.py", "--"]
CMD ["--command", "inspect", "--spec", "/work/work/inspect.spec.json"]
