FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watch_imax.py .

ENV STATE_FILE=/data/state.json
ENV LOOP=1
ENV CHECK_INTERVAL_SECONDS=300
ENV PYTHONUNBUFFERED=1

VOLUME /data

CMD ["python", "-u", "watch_imax.py", "--loop"]
