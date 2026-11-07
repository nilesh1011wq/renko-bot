FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8765

COPY . /app

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "import os, urllib.request; p=os.environ.get('PORT','8765'); urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=4)" || exit 1

CMD ["python3", "server.py"]
