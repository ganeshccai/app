# Use Python official image
FROM python:3.12-slim

WORKDIR /app

# Copy files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run uses PORT env variable
CMD ["python", "main.py"]
