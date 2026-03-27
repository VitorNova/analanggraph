FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
EXPOSE 3200
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "3200"]
