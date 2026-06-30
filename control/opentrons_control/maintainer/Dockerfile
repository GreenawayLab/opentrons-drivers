FROM python:3.12-slim

WORKDIR /app

# The source is fetched as an HTTPS tarball and extracted with the
# stdlib. build: produce the drivers wheel. fastapi/uvicorn/httpx/pydantic: the
# service surface, the archive download, and the backend client.
RUN pip install --no-cache-dir fastapi uvicorn httpx pydantic build

# Mirror the import path inside the container, the same way the backend does,
# so imports read `opentrons_control.maintainer.app.*` everywhere.
RUN mkdir -p /app/opentrons_control/maintainer \
 && touch /app/opentrons_control/__init__.py \
          /app/opentrons_control/maintainer/__init__.py

COPY app /app/opentrons_control/maintainer/app

ENV PYTHONPATH=/app

CMD ["uvicorn", "opentrons_control.maintainer.app.main:app", "--host", "0.0.0.0", "--port", "8000"]