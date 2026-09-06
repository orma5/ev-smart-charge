# Ships uv alongside the interpreter, so the lockfile can be installed without
# a separate uv download step in the build.
FROM ghcr.io/astral-sh/uv:python3.14-alpine

# Set environment variable for timezone
ENV TZ=Europe/Stockholm

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

# Copy your script
COPY main.py .

# Run script by default when container starts
ENTRYPOINT ["python", "main.py"]
