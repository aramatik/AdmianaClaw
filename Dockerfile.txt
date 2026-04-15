FROM python:3.12-slim

WORKDIR /app

# Устанавливаем часовой пояс прямо в систему контейнера
ENV TZ=Europe/Kiev

RUN apt-get update && apt-get install -y \
    iputils-ping curl wget nano net-tools htop docker.io ffmpeg p7zip-full tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt ddgs edge-tts

COPY . .

CMD ["python", "bot.py"]
