FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. Copy ONLY the requirements file first (this caches the install)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. Copy the rest of the code
COPY . .

CMD ["python", "main.py"]
