# Используем легковесный образ Python
FROM python:3.12-slim

# Указываем рабочую директорию внутри контейнера
WORKDIR /app

# Устанавливаем переменные окружения для корректной работы Python
# PYTHONDONTWRITEBYTECODE - не создавать .pyc файлы
# PYTHONUNBUFFERED - выводить логи в консоль без задержек
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Устанавливаем часовой пояс (Киев)
ENV TZ=Europe/Kiev

# Устанавливаем системные зависимости:
# - iputils-ping, curl, wget, net-tools, htop: для диагностики сети
# - docker.io: если нужно управлять докером изнутри (через проброшенный сокет)
# - ffmpeg: для конвертации аудио (голосовые сообщения)
# - p7zip-full: для работы инструмента поиска в архивах
# - tzdata: для корректной работы со временем
RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    curl \
    wget \
    nano \
    net-tools \
    htop \
    docker.io \
    ffmpeg \
    p7zip-full \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем Python библиотеки из requirements.txt
# (Все нужные пакеты, включая mcp, aiogram/telebot, ddgs и edge-tts уже должны быть там)
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все файлы проекта в контейнер
COPY . .

# Создаем папку для загрузок, если её нет, чтобы избежать ошибок прав доступа
RUN mkdir -p /app/downloads

# Команда для запуска бота
CMD ["python", "bot.py"]
