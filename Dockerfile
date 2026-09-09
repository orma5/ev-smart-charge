# Ships uv alongside the interpreter, so the lockfile can be installed without
# a separate uv download step in the build.
FROM ghcr.io/astral-sh/uv:python3.14-alpine

# Set environment variable for timezone
ENV TZ=Europe/Stockholm

# Python block-buffers stdout when it is a pipe rather than a terminal, which
# under Kubernetes means `kubectl logs` shows nothing until 8KB has built up.
# The CronJob never noticed: the container exited after every run and exit
# flushed the buffer. A process that never exits never flushes, so the logs of
# a long-running scheduler were invisible exactly when they were wanted.
ENV PYTHONUNBUFFERED=1

# Install dependencies, including tzdata for timezone support
RUN apk add --no-cache tzdata

# Set timezone
RUN cp /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone

# Set working directory
WORKDIR /app

# Install Python dependencies. --frozen fails the build if uv.lock has drifted
# from pyproject.toml, rather than silently resolving something else; --no-dev
# keeps pytest out of the runtime image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Run out of the synced venv directly. `uv run` would work too, but it re-checks
# the lockfile on every start for no benefit here.
ENV PATH="/app/.venv/bin:$PATH"

# The application. Listed file by file rather than `COPY . .` so that a stray
# .env - which exists in local development and holds the API key - cannot be
# baked into an image that gets pushed to the registry.
COPY main.py db.py savings.py web.py app.py schema.sql ./
COPY templates ./templates

# Served by waitress from inside app.py, alongside the scheduler thread.
EXPOSE 8000

ENTRYPOINT ["python", "app.py"]
