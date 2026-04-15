import os
import re
import time
import random
import requests
from urllib.parse import urlparse
from ddgs import DDGS

# ─────────────────────────────────────────────
#  User-Agent пул для имитации браузера
# ─────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

def _get_headers(referer: str = None, extra: dict = None) -> dict:
    """Формирует заголовки с рандомным UA для обхода простых блокировок."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    }
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    if extra:
        headers.update(extra)
    return headers


# ─────────────────────────────────────────────
#  Парсер продвинутых операторов в запросе
# ─────────────────────────────────────────────

def _parse_query(query: str) -> dict:
    """
    Разбирает запрос на части:
      - site:example.com   → ограничить поиск сайтом
      - filetype:pdf       → искать конкретный тип файла
      - download:https://… → прямая загрузка файла по ссылке
      - остальное          → обычный поисковый запрос

    Примеры запросов (как ИИ должен их передавать):
      "ubuntu server guide filetype:pdf"
      "nginx config site:nginx.org"
      "download:https://example.com/report.pdf"
      "python asyncio tutorial"
    """
    result = {
        "raw": query,
        "terms": query,
        "site": None,
        "filetype": None,
        "download_url": None,
    }

    # Команда прямого скачивания
    dl_match = re.search(r'download:(https?://\S+)', query, re.IGNORECASE)
    if dl_match:
        result["download_url"] = dl_match.group(1)
        return result

    # site:
    site_match = re.search(r'\bsite:(\S+)', query, re.IGNORECASE)
    if site_match:
        result["site"] = site_match.group(1)

    # filetype:
    ft_match = re.search(r'\bfiletype:(\w+)', query, re.IGNORECASE)
    if ft_match:
        result["filetype"] = ft_match.group(1).lower()

    # Чистый текст запроса (без наших операторов — они уйдут в DDGS напрямую)
    result["terms"] = query

    return result


# ─────────────────────────────────────────────
#  Загрузчик файлов по прямой ссылке
# ─────────────────────────────────────────────

DOWNLOAD_DIR = "/app/downloads"

def download_file(url: str, save_dir: str = DOWNLOAD_DIR, timeout: int = 60) -> str:
    """
    Скачивает файл по прямой ссылке с нормальными заголовками и UA.
    Возвращает путь к сохранённому файлу или строку с ошибкой.
    """
    try:
        os.makedirs(save_dir, exist_ok=True)

        # Определяем имя файла из URL
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path) or "downloaded_file"
        # Убираем опасные символы
        filename = re.sub(r'[^\w\.\-]', '_', filename)
        if not filename or filename == "_":
            filename = f"file_{int(time.time())}"

        save_path = os.path.join(save_dir, filename)

        referer = f"{parsed.scheme}://{parsed.netloc}/"
        headers = _get_headers(referer=referer)

        print(f"[Downloader] Качаю: {url}")
        session = requests.Session()
        # Первый запрос — HEAD чтобы получить заголовки и куки
        try:
            head = session.head(url, headers=headers, timeout=15, allow_redirects=True)
            content_type = head.headers.get("Content-Type", "")
            content_length = head.headers.get("Content-Length", "?")
        except Exception:
            content_type = ""
            content_length = "?"

        # Основной GET запрос со стримингом
        response = session.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
        response.raise_for_status()

        # Уточняем имя из Content-Disposition если есть
        cd = response.headers.get("Content-Disposition", "")
        cd_match = re.search(r'filename[^;=\n]*=([\'"]?)([^\'";\n]+)\1', cd)
        if cd_match:
            cd_name = re.sub(r'[^\w\.\-]', '_', cd_match.group(2).strip())
            if cd_name:
                filename = cd_name
                save_path = os.path.join(save_dir, filename)

        total = int(response.headers.get("Content-Length", 0))
        downloaded = 0

        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

        size_kb = os.path.getsize(save_path) / 1024
        return (
            f"✅ Файл успешно скачан!\n"
            f"📁 Путь: {save_path}\n"
            f"📦 Размер: {size_kb:.1f} KB\n"
            f"🔗 Источник: {url}"
        )

    except requests.exceptions.HTTPError as e:
        return f"❌ HTTP ошибка при скачивании: {e.response.status_code} {e.response.reason}\nURL: {url}"
    except requests.exceptions.ConnectionError:
        return f"❌ Ошибка соединения. Проверь URL или доступность сайта:\n{url}"
    except requests.exceptions.Timeout:
        return f"❌ Таймаут при скачивании (>{timeout}s):\n{url}"
    except Exception as e:
        return f"❌ Ошибка загрузки: {str(e)}\nURL: {url}"


# ─────────────────────────────────────────────
#  Основная функция поиска
# ─────────────────────────────────────────────

def search_web(query: str) -> str:
    """
    Продвинутый веб-поиск с поддержкой операторов:
      • Обычный поиск:        "что такое nginx"
      • Поиск по сайту:       "документация nginx site:nginx.org"
      • Поиск файла:          "linux guide filetype:pdf"
      • Комбо:                "nginx config filetype:pdf site:nginx.org"
      • Прямая загрузка:      "download:https://example.com/file.pdf"

    ИИ может использовать любые комбинации операторов в запросе.
    """
    print(f"[WebSearch] Запрос: {query}")
    parsed = _parse_query(query)

    # ── Режим прямой загрузки файла ──
    if parsed["download_url"]:
        return download_file(parsed["download_url"])

    # ── Обычный поиск через DDGS (операторы site: и filetype: DDGS понимает нативно) ──
    try:
        search_query = parsed["terms"]  # Передаём запрос как есть, включая операторы

        results = DDGS().text(search_query, max_results=7)

        if not results:
            # Если с операторами не нашлось — пробуем без filetype
            if parsed["filetype"] and parsed["site"]:
                fallback = re.sub(r'\bfiletype:\w+\s*', '', search_query).strip()
                results = DDGS().text(fallback, max_results=5)
            if not results:
                return f"По запросу «{search_query}» ничего не найдено."

        # Формируем ответ
        info_parts = []
        if parsed["site"]:
            info_parts.append(f"сайт: {parsed['site']}")
        if parsed["filetype"]:
            info_parts.append(f"тип: .{parsed['filetype']}")
        filter_info = f" [{', '.join(info_parts)}]" if info_parts else ""

        formatted = f"Результаты поиска{filter_info} по запросу «{search_query}»:\n\n"

        for i, res in enumerate(results):
            title = res.get("title", "Без заголовка")
            body  = res.get("body", "").strip()
            href  = res.get("href", "")
            formatted += f"[{i+1}] {title}\n{body}\nСсылка: {href}\n\n"

        # Добавляем подсказку если нашли файлы — ИИ может скачать
        if parsed["filetype"] and results:
            formatted += (
                f"💡 Подсказка: чтобы скачать найденный файл, используй search_web_tool с запросом "
                f"\"download:ССЫЛКА_НА_ФАЙЛ\"\n"
            )

        return formatted[:5000]

    except Exception as e:
        return f"Ошибка при выполнении веб-поиска: {str(e)}"


# ─────────────────────────────────────────────
#  Вспомогательная функция для прямого вызова
#  загрузчика из агентских инструментов бота
# ─────────────────────────────────────────────

def download_file_tool(url: str, save_dir: str = DOWNLOAD_DIR) -> str:
    """Инструмент для ИИ: скачать файл по прямой ссылке с правильными заголовками."""
    return download_file(url, save_dir)
    
