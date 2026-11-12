FROM python:3.13-slim

WORKDIR /app
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt
COPY server /app/server

ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec uvicorn server.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
