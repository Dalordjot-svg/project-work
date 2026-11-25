FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY bot.py calendar_web.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
