FROM python:3.12-slim

# Where code lives inside the container
WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir fastapi uvicorn python-multipart

# Copy backend code
COPY app /app/app

# Start the server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
