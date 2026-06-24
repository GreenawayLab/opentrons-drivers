FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        fastapi \
        uvicorn \
        httpx \
        pydantic \
        pydantic-settings \
        sqlalchemy \
        "psycopg[binary]" \
        cryptography \
        pyjwt \
        jinja2 \
        python-multipart
        
# Mirror the import path inside the container
RUN mkdir -p /app/opentrons_control/backend \
 && touch /app/opentrons_control/__init__.py \
          /app/opentrons_control/backend/__init__.py

COPY app /app/opentrons_control/backend/app

ENV PYTHONPATH=/app

CMD ["python", "-m", "opentrons_control.backend.app.main"]
