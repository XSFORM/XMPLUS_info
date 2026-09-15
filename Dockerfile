FROM python:3.11-slim

WORKDIR /app

# Git (для git pull из бота) + Docker CLI + Compose plugin
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
RUN mkdir -p /usr/local/lib/docker/cli-plugins
COPY --from=docker/compose-bin:v2.32.4 /docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

# Установим зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.main"]
