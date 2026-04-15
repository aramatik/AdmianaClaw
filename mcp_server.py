import os
import subprocess
import time
from mcp.server.fastmcp import FastMCP

# Импортируем твои существующие рабочие модули
import web_search
import task

# Инициализация MCP сервера. 
# При запуске через stdio клиент будет общаться с ним через стандартный ввод/вывод.
mcp = FastMCP("BotPlugins")

@mcp.tool()
def execute_bash(command: str) -> str:
    """
    Выполняет bash команду в системе и возвращает результат.
    Использовать для системного администрирования, работы с файлами и скриптами.
    """
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout if result.stdout else result.stderr
        return output[:2500]
    except Exception as e:
        return f"Ошибка выполнения: {str(e)}"

@mcp.tool()
def search_web_tool(query: str) -> str:
    """
    Ищет актуальную информацию в интернете.
    """
    return web_search.search_web(query)

@mcp.tool()
def download_file_tool(url: str) -> str:
    """
    Скачивает файл по URL. Возвращает локальный путь к скачанному файлу.
    """
    return web_search.download_file_tool(url)

@mcp.tool()
def send_file_to_telegram(filepath: str) -> str:
    """
    Подготавливает локальный файл для отправки пользователю в чат.
    """
    if not os.path.exists(filepath):
        return f"Ошибка: Файл {filepath} не найден на сервере."
    
    # Сервер не имеет доступа к Telegram API, поэтому возвращаем триггер.
    # Главный клиент бота перехватит этот маркер и вызовет bot.send_document.
    return f"__MCP_SEND_FILE_TRIGGER__:{filepath}"

@mcp.tool()
def delete_scheduled_task_tool(chat_id: int, task_id: str) -> str:
    """
    Удаляет фоновую задачу по её ID. Требует передачи ID текущего чата.
    """
    if task.delete_task(chat_id, task_id, deleted_by="AI_AGENT"):
        return f"Успех: Задача {task_id} удалена из расписания."
    return f"Ошибка: Задача {task_id} не найдена."

@mcp.tool()
def list_my_tasks_tool(chat_id: int) -> str:
    """
    Возвращает список всех активных фоновых задач текущего пользователя (chat_id).
    """
    tasks = task.get_all_tasks(chat_id)
    if not tasks:
        return "Нет активных задач."
    res = "Активные задачи:\n"
    for t in tasks:
        res += f"- ID: {t['id']}, CRON: {t['cron']}, Промпт: {t['prompt']}\n"
    return res

@mcp.tool()
def sleep_tool(seconds: int) -> str:
    """
    Приостанавливает выполнение логики на указанное количество секунд.
    Максимум 300 секунд.
    """
    sec_val = min(max(int(seconds), 1), 300)
    time.sleep(sec_val)
    return f"Успех: ожидание {sec_val} секунд завершено."

if __name__ == "__main__":
    # Запускаем транспорт. По умолчанию FastMCP использует stdio.
    mcp.run()
