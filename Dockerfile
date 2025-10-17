FROM python:3.11-slim

WORKDIR /app

# Установим зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

ENV PYTHONUNBUFFERED=1

# Не копируем .env (секреты)
CMD ["python", "-m", "app.main"]