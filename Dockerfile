FROM python:3.12-slim

WORKDIR /app

# Скопировать зависимости
COPY requirements.txt .

# Установить зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Скопировать весь код
COPY . .

# Экспонируем порт, на котором Cloud Run ждёт Health-check
EXPOSE 8080

# Запускаем бот (Flask стартует автоматически из keep_alive.py)
CMD ["python", "Botparsing.py"]