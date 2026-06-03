FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cached until requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source — .dockerignore excludes .env, data/, __pycache__
COPY . .

CMD ["python", "run_pipeline.py"]
