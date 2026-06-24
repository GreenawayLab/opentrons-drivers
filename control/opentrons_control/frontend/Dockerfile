FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx jinja2 python-multipart

COPY app /app/app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
