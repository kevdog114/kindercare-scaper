FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY config.sample.json ./

RUN mkdir -p /app/media

ENV CONFIG_PATH=/app/config.local.json
ENV AUTH_TOKEN=changethis

EXPOSE 8080

CMD ["python", "server.py"]
