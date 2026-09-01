# Use a Python image with uv pre-installed
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*

# Setup a non-root user
RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --create-home nonroot

# Install the project into `/code`
WORKDIR /code

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Omit development dependencies
ENV UV_NO_DEV=1

# Ensure installed tools can be executed out of the box
ENV UV_TOOL_BIN_DIR=/usr/local/bin

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Then, add the rest of the project source code and install it
# Installing separately from its dependencies allows optimal layer caching
COPY . /code
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

ENTRYPOINT []

# Place executables in the environment at the front of the path
ENV PATH="/code/.venv/bin:$PATH"
# SECRET_KEY is scoped to this command only — never bake it into the image,
# or a deploy missing the real secret would boot with a publicly known key.
RUN SECRET_KEY="non-secret-key-for-building-purposes" ./manage.py collectstatic --noinput --ignore tailwindcss

EXPOSE 8000
RUN chmod +x entry.sh

ENTRYPOINT ["/code/entry.sh"]
