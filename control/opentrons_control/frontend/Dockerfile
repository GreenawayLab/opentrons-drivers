FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx jinja2 python-multipart

# Mirror the import path inside the container so imports read
# `opentrons_control.frontend.app.*` everywhere.
RUN mkdir -p /app/opentrons_control/frontend \
 && touch /app/opentrons_control/__init__.py \
          /app/opentrons_control/frontend/__init__.py

COPY app /app/opentrons_control/frontend/app

ENV PYTHONPATH=/app

CMD ["uvicorn", "opentrons_control.frontend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
