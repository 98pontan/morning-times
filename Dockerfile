FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/app/config.yaml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py config.yaml seed.yaml alembic.ini ./
COPY migrations ./migrations
COPY src ./src

USER 1000:1000
EXPOSE 8080
CMD ["python", "main.py"]
