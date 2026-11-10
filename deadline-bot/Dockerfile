FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py logic.py ./
ENV DB_PATH=/data/deadlines.db PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "bot.py"]
