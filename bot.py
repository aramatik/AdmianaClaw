import os
import subprocess
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import google.generativeai as genai
import html
import re
import shlex
import asyncio
import edge_tts
import time
import json
import threading
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

# Импортируем MCP SDK
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from contextlib import AsyncExitStack

# Импортируем наши внешние модули
from markdown import split_text_safely, md_to_html
from search import parse_search_query, run_grep_search, run_archive_search, format_search_results
import task # МОДУЛЬ ПЛАНИРОВЩИКА ЗАДАЧ
# Модуль web_search убран, так как он теперь полностью работает на стороне MCP сервера

# Загружаем ключи
TG_TOKEN = os.getenv("TG_TOKEN")
API_KEY_1 = os.getenv("GEMINI_API_KEY")
API_KEY_2 = os.getenv("GEMINI2_API_KEY")
API_KEY_3 = os.getenv("GEMINI3_API_KEY")

# Безопасно собираем все ID админов
ADMIN_IDS = set()
for env_var in ["ADMIN_ID", "ADMIN2_ID", "ADMIN3_ID"]:
    val = os.getenv(env_var)
    if val and val.strip().lstrip('-').isdigit():
        ADMIN_IDS.add(int(val.strip()))

if not ADMIN_IDS:
    print("ВНИМАНИЕ: Не задано ни одного ADMIN_ID! Бот никого не пустит.")

bot = telebot.TeleBot(TG_TOKEN)

genai.configure(api_key=API_KEY_1)
CURRENT_KEY_NUM = 1

AVAILABLE_MODELS = []
PRIORITY_MODELS_CACHE = []
OTHER_MODELS_CACHE = []
CURRENT_MODEL = None
chat_agent = None
model_advisor = None
CURRENT_CHAT_ID = None
PENDING_RETRY_MESSAGE = None
PENDING_FILES = {}
PENDING_SEARCH_RESULTS = {}

# Единые настройки для ВСЕХ моделей
MODEL_ROLE = {}  
MODEL_MODE = {}  
PENDING_ACTION = {} 

ACTION_LOGS = {}
LAST_ACTIONS = {} 
VOICE_MODE = {}  
STATUS_MSG = {}

# ТРЕКЕРЫ И ФЛАГИ
TURN_STATS = {}
CONSECUTIVE_SLEEPS = {}
ABORT_FLAGS = {} 

# ─────────────────────────────────────────────
#  ПОДКЛЮЧЕНИЕ К MCP СЕРВЕРУ В ФОНОВОМ ПОТОКЕ
# ─────────────────────────────────────────────
mcp_loop = asyncio.new_event_loop()
mcp_session = None
mcp_exit_stack = AsyncExitStack()

def _run_mcp_thread():
    asyncio.set_event_loop(mcp_loop)
    mcp_loop.run_forever()

threading.Thread(target=_run_mcp_thread, daemon=True).start()

async def _init_mcp_async():
    global mcp_session
    server_script = os.path.join(os.path.dirname(__file__), "mcp_server.py")
    server_params = StdioServerParameters(command="python", args=[server_script])
    stdio_transport = await mcp_exit_stack.enter_async_context(stdio_client(server_params))
    read, write = stdio_transport
    mcp_session = await mcp_exit_stack.enter_async_context(ClientSession(read, write))
    await mcp_session.initialize()

def call_mcp_tool_sync(name: str, args: dict) -> str:
    """Синхронная обертка для вызова инструментов из MCP сервера"""
    try:
        future = asyncio.run_coroutine_threadsafe(mcp_session.call_tool(name, arguments=args), mcp_loop)
        result = future.result(timeout=120)
        return result.content[0].text if result.content else "Success (No output)"
    except Exception as e:
        return f"Error executing tool {name} via MCP: {str(e)}"

# ─────────────────────────────────────────────
#  КОНФИГУРАЦИЯ: ПРОМПТЫ, МОДЕЛИ, ЛИМИТЫ
# ─────────────────────────────────────────────

PROMPTS_FILE = "prompts.txt"
PROMPTS = {}

MODELS_FILE = "models.txt"
MODEL_RPM_LIMITS = {}
MODEL_RESTRICTED_KEYS = {} 
MODEL_TPM_LIMITS = {}
MODEL_RPD_LIMITS = {}
PRIORITY_MODELS = []

# Файл персистентного хранения RPD
LIMITS_STATE_FILE = "/app/downloads/temp/api_limits.json"

API_RPD_HISTORY = {
    1: {"date": "", "usage": {}},
    2: {"date": "", "usage": {}},
    3: {"date": "", "usage": {}}
}
API_REQUEST_HISTORY = {1: deque(), 2: deque(), 3: deque()}
API_TOKEN_HISTORY = {1: deque(), 2: deque(), 3: deque()}

def load_limits_state():
    global API_RPD_HISTORY
    os.makedirs(os.path.dirname(LIMITS_STATE_FILE), exist_ok=True)
    if os.path.exists(LIMITS_STATE_FILE):
        try:
            with open(LIMITS_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    API_RPD_HISTORY[int(k)] = v
        except Exception as e: 
            print(f"Ошибка загрузки лимитов: {e}")
    
    today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    for i in [1, 2, 3]:
        if i not in API_RPD_HISTORY or API_RPD_HISTORY[i].get("date") != today_str:
            API_RPD_HISTORY[i] = {"date": today_str, "usage": {}}

def save_limits_state():
    os.makedirs(os.path.dirname(LIMITS_STATE_FILE), exist_ok=True)
    try:
        with open(LIMITS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(API_RPD_HISTORY, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Ошибка сохранения лимитов: {e}")

def load_prompts_config():
    global PROMPTS
    PROMPTS.clear()
    if not os.path.exists(PROMPTS_FILE):
        with open(PROMPTS_FILE, "w", encoding="utf-8") as f: f.write("")

    with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
        current_key = None
        current_text = []
        for line in f:
            match = re.match(r'^\[(.*?)\]$', line.strip())
            if match:
                if current_key: PROMPTS[current_key] = "\n".join(current_text).strip()
                current_key = match.group(1)
                current_text = []
            else: current_text.append(line.rstrip('\n'))
        if current_key: PROMPTS[current_key] = "\n".join(current_text).strip()

def load_models_config():
    global PRIORITY_MODELS, MODEL_RPM_LIMITS, MODEL_RESTRICTED_KEYS, MODEL_TPM_LIMITS, MODEL_RPD_LIMITS
    PRIORITY_MODELS.clear()
    MODEL_RPM_LIMITS.clear()
    MODEL_RESTRICTED_KEYS.clear()
    MODEL_TPM_LIMITS.clear()
    MODEL_RPD_LIMITS.clear()
    
    if not os.path.exists(MODELS_FILE):
        default_content = "gemini-2.5-flash-lite [RPM:10, TPM:250000, RPD:20, KEY:2,3]\n"
        with open(MODELS_FILE, "w", encoding="utf-8") as f: f.write(default_content)
    
    with open(MODELS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            
            model_match = re.search(r'^([\w\-\.]+)', line)
            if model_match:
                model_name = model_match.group(1)
                PRIORITY_MODELS.append(model_name)
                
                rpm_match = re.search(r'RPM:(\d+)', line)
                if rpm_match: MODEL_RPM_LIMITS[model_name] = int(rpm_match.group(1))
                
                tpm_match = re.search(r'TPM:(\d+)', line)
                if tpm_match: MODEL_TPM_LIMITS[model_name] = int(tpm_match.group(1))
                
                rpd_match = re.search(r'RPD:(\d+)', line)
                if rpd_match: MODEL_RPD_LIMITS[model_name] = int(rpd_match.group(1))
                    
                key_match = re.search(r'KEY:([\d,]+)', line)
                if key_match: MODEL_RESTRICTED_KEYS[model_name] = [int(k) for k in key_match.group(1).split(',') if k.isdigit()]

load_prompts_config()
load_models_config()
load_limits_state()

def log_admin_action(user_id, action):
    print(f"[ADMIN {user_id}] {action}")

# ─────────────────────────────────────────────
#  Live-статус, Трекер Лимитов и Авто-Переключение
# ─────────────────────────────────────────────

def set_status(chat_id, text: str, show_abort=False):
    global STATUS_MSG
    markup = None
    if show_abort:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛑 Аварийный стоп", callback_data=f"abort_{chat_id}"))
        
    msg_id = STATUS_MSG.get(chat_id)
    if msg_id:
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode='HTML', reply_markup=markup)
            return
        except Exception as e:
            err_str = str(e).lower()
            if "is not modified" in err_str or "retry after" in err_str or "too many requests" in err_str:
                return
            else:
                STATUS_MSG.pop(chat_id, None)
    try:
        msg = bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=markup)
        STATUS_MSG[chat_id] = msg.message_id
    except: pass

def clear_status(chat_id):
    msg_id = STATUS_MSG.pop(chat_id, None)
    if msg_id:
        try: bot.delete_message(chat_id, msg_id)
        except: pass

def track_token_usage(token_count):
    global CURRENT_KEY_NUM
    if token_count: API_TOKEN_HISTORY[CURRENT_KEY_NUM].append((time.time(), token_count))

def check_api_rate_limit(chat_id, current_status_text, model_name=None):
    global CURRENT_KEY_NUM
    if model_name is None: model_name = CURRENT_MODEL
    if not model_name: return

    clean_name = model_name.replace('models/', '')
    rpm_limit = MODEL_RPM_LIMITS.get(clean_name)
    tpm_limit = MODEL_TPM_LIMITS.get(clean_name)
    rpd_limit = MODEL_RPD_LIMITS.get(clean_name)
    now = time.time()
    
    today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    key_data = API_RPD_HISTORY[CURRENT_KEY_NUM]
    
    if key_data["date"] != today_str:
        key_data["date"] = today_str
        key_data["usage"] = {}
        
    if rpd_limit:
        current_rpd = key_data["usage"].get(clean_name, 0)
        if current_rpd >= rpd_limit:
            raise Exception(f"RPD_LIMIT_REACHED|{clean_name}|{rpd_limit}")
            
    if rpm_limit:
        history_rpm = API_REQUEST_HISTORY.setdefault(CURRENT_KEY_NUM, deque())
        while history_rpm and now - history_rpm[0] > 60.5: history_rpm.popleft()
        if len(history_rpm) >= rpm_limit:
            wait_time = 60.5 - (now - history_rpm[0])
            if wait_time > 0:
                if chat_id: set_status(chat_id, f"{current_status_text}\n⏳ <i>Пауза: ожидание {wait_time:.1f}с (лимит {rpm_limit} RPM)</i>", show_abort=True)
                time.sleep(wait_time) 
                if chat_id: set_status(chat_id, current_status_text, show_abort=True)
                now = time.time()

    if tpm_limit:
        history_tpm = API_TOKEN_HISTORY.setdefault(CURRENT_KEY_NUM, deque())
        while history_tpm and now - history_tpm[0][0] > 60.5: history_tpm.popleft()
        current_tpm = sum(count for _, count in history_tpm)
        if current_tpm > (tpm_limit - 1000): 
            wait_time = 60.5 - (now - history_tpm[0][0])
            if wait_time > 0:
                if chat_id: set_status(chat_id, f"{current_status_text}\n⏳ <i>Тайм-аут токенов: ожидание {wait_time:.1f}с (использовано {current_tpm}/{tpm_limit} TPM)</i>", show_abort=True)
                time.sleep(wait_time)
                if chat_id: set_status(chat_id, current_status_text, show_abort=True)
                now = time.time()

    API_REQUEST_HISTORY[CURRENT_KEY_NUM].append(now)
    key_data["usage"][clean_name] = key_data["usage"].get(clean_name, 0) + 1
    save_limits_state() 

def switch_api_key(chat_id, reason):
    global CURRENT_KEY_NUM, CURRENT_MODEL
    old_key = CURRENT_KEY_NUM
    keys = [1, 2, 3]
    idx = keys.index(old_key)
    clean_name = CURRENT_MODEL.replace('models/', '') if CURRENT_MODEL else ""
    
    for i in range(1, 3):
        next_key = keys[(idx + i) % 3]
        target_key = API_KEY_1 if next_key == 1 else (API_KEY_2 if next_key == 2 else API_KEY_3)
        if not target_key: continue
        
        rpd_limit = MODEL_RPD_LIMITS.get(clean_name)
        if rpd_limit:
            today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
            key_data = API_RPD_HISTORY.get(next_key, {"date": today_str, "usage": {}})
            if key_data["date"] == today_str and key_data["usage"].get(clean_name, 0) >= rpd_limit:
                continue 
        
        CURRENT_KEY_NUM = next_key
        genai.configure(api_key=target_key)
        if chat_id:
            bot.send_message(chat_id, f"🔄 <b>Авто-переключение ключа!</b>\n<i>{reason}</i>\nТеперь активен: <b>KEY {CURRENT_KEY_NUM}</b>", parse_mode='HTML')
        return True
        
    return False

def safe_send_message(agent, chat_id, prompt_or_parts, status_text="🤖 <b>Обрабатываю запрос...</b>", is_advisor=False, model_name=None):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            check_api_rate_limit(chat_id, status_text, model_name)
            
            if is_advisor:
                response = agent.generate_content(prompt_or_parts)
            else:
                response = agent.send_message(prompt_or_parts)
            
            if chat_id in TURN_STATS:
                TURN_STATS[chat_id]["rpd"] += 1
                if hasattr(response, 'usage_metadata'):
                    TURN_STATS[chat_id]["tpm"] += response.usage_metadata.total_token_count
                    
            if hasattr(response, 'usage_metadata'):
                track_token_usage(response.usage_metadata.total_token_count)
                
            if not is_advisor:
                trim_chat_history(agent, model_name)
                
            return response
            
        except Exception as e:
            error_text = str(e)
            if "RPD_LIMIT_REACHED" in error_text:
                if switch_api_key(chat_id, f"Достигнут дневной лимит (RPD) на ключе {CURRENT_KEY_NUM}."):
                    continue
                else: raise Exception("Все доступные ключи исчерпали дневной лимит (RPD) для этой модели.")
                
            elif "429" in error_text or "Quota exceeded" in error_text:
                if switch_api_key(chat_id, f"Превышена квота API (Ошибка 429) на ключе {CURRENT_KEY_NUM}."):
                    continue
                else: raise Exception("Все ключи исчерпали квоту API (429). Попробуйте позже.")
                
            elif "function response turn comes immediately after a function call" in error_text:
                if not is_advisor and hasattr(agent, 'history'): agent.history.clear()
                raise Exception("Сбой синхронизации API! Контекст был поврежден. Память очищена.")
            else:
                raise e
                
    raise Exception("Превышено количество попыток переключения ключей.")

def handle_api_error(e, chat_id, message_id, clean_model_name):
    error_text = str(e)
    if "Все доступные ключи исчерпали" in error_text or "Все ключи исчерпали квоту" in error_text or "Превышено количество попыток" in error_text:
        safe_edit_message(chat_id, message_id, f"⚠️ <b>Лимиты API исчерпаны!</b>\n\n{html.escape(error_text)}\nСмените модель.", reply_markup=get_models_keyboard())
    elif "Сбой синхронизации API" in error_text or "function response turn comes immediately after" in error_text:
        safe_edit_message(chat_id, message_id, f"⚠️ <b>Сбой синхронизации API!</b>\nКонтекст был поврежден. Память ИИ автоматически очищена. Повторите запрос.")
    else:
        safe_edit_message(chat_id, message_id, f"❌ Ошибка ИИ: {html.escape(error_text)}")

# ─────────────────────────────────────────────
#  ФУНКЦИИ ВЫВОДА
# ─────────────────────────────────────────────

def safe_edit_message(chat_id, message_id, text, parse_mode='HTML', reply_markup=None):
    try: bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception: pass

def send_long_text(chat_id, text, first_msg_id=None, is_code=False, prefix="", reply_markup=None):
    if not text: return
    text = text.replace('\r\n', '\n')
    chunks = split_text_safely(text, max_len=3500)

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        current_markup = reply_markup if is_last else None

        if is_code: formatted = f'<pre><code class="language-bash">{html.escape(chunk.strip())}</code></pre>'
        else:
            formatted_chunk = md_to_html(chunk)
            formatted = f"{prefix}{formatted_chunk}" if i == 0 else formatted_chunk

        if i == 0 and first_msg_id:
            try: safe_edit_message(chat_id, first_msg_id, formatted, parse_mode='HTML', reply_markup=current_markup)
            except Exception: safe_edit_message(chat_id, first_msg_id, f"{prefix}{chunk.strip()}" if i == 0 else chunk.strip(), reply_markup=current_markup)
        else:
            try: bot.send_message(chat_id, formatted, parse_mode='HTML', reply_markup=current_markup)
            except Exception: bot.send_message(chat_id, chunk.strip(), reply_markup=current_markup)

def clean_text_for_voice(text: str) -> str:
    if not text: return ""
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'`.*?`', '', text)
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'<[^>]*>', '', text)
    text = re.sub(r'[*#_~()\[\]{}<>@|\\/]', '', text)
    text = text.replace('\n', '. ')
    text = re.sub(r'\.{2,}', '.', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()

def generate_and_send_voice(chat_id, text):
    clean_text = clean_text_for_voice(text)
    if not clean_text: return
    voice_chunks = split_text_safely(clean_text, max_len=2000)
    total_chunks = len(voice_chunks)

    for i, chunk in enumerate(voice_chunks):
        if not chunk.strip(): continue
        mp3_path = f"temp_tts_{chat_id}_{i}.mp3"
        ogg_path = f"temp_tts_{chat_id}_{i}.ogg"
        msg_wait_voice = None
        try:
            part_text = f" [часть {i+1}/{total_chunks}]" if total_chunks > 1 else ""
            msg_wait_voice = bot.send_message(chat_id, f"🎙 <i>Отправка голосового сообщения{part_text}...</i>", parse_mode='HTML')
            bot.send_chat_action(chat_id, 'record_voice')
            async def _generate():
                communicate = edge_tts.Communicate(chunk, "ru-RU-SvetlanaNeural")
                await communicate.save(mp3_path)
            asyncio.run(_generate())
            if os.path.exists(mp3_path):
                subprocess.run(f"ffmpeg -i {mp3_path} -c:a libopus -b:a 32k -v quiet -y {ogg_path}", shell=True, timeout=60)
                if os.path.exists(ogg_path):
                    with open(ogg_path, 'rb') as voice_file: bot.send_voice(chat_id, voice_file)
        except Exception as e: print(f"Ошибка синтеза: {e}")
        finally:
            if msg_wait_voice: clear_status(chat_id)
            if os.path.exists(mp3_path): os.remove(mp3_path)
            if os.path.exists(ogg_path): os.remove(ogg_path)

# ─────────────────────────────────────────────
#  АГЕНТСКИЕ ИНСТРУМЕНТЫ (MCP-ПРОКСИ)
# ─────────────────────────────────────────────

def execute_bash(command: str) -> str:
    """Выполняет bash команду в системе."""
    if CURRENT_CHAT_ID:
        ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("bash", command))
        short_cmd = command if len(command) <= 80 else command[:77] + "…"
        status_text = f"⚙️ <b>Выполняю команду:</b>\n<code>{html.escape(short_cmd)}</code>"
        set_status(CURRENT_CHAT_ID, status_text, show_abort=True)
    return call_mcp_tool_sync("execute_bash", {"command": command})

def search_web_tool(query: str) -> str:
    """Ищет актуальную информацию в интернете."""
    if CURRENT_CHAT_ID:
        ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("search", query))
        short_q = query if len(query) <= 80 else query[:77] + "…"
        status_text = f"🔍 <b>Ищу в интернете:</b>\n<i>{html.escape(short_q)}</i>"
        set_status(CURRENT_CHAT_ID, status_text, show_abort=True)
    return call_mcp_tool_sync("search_web_tool", {"query": query})

def download_file_tool(url: str) -> str:
    """Скачивает файл по URL. Возвращает локальный путь."""
    if CURRENT_CHAT_ID:
        ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("download", url))
        short_url = url if len(url) <= 70 else url[:67] + "…"
        status_text = f"⬇️ <b>Скачиваю файл:</b>\n<code>{html.escape(short_url)}</code>"
        set_status(CURRENT_CHAT_ID, status_text, show_abort=True)
    return call_mcp_tool_sync("download_file_tool", {"url": url})

def send_file_to_telegram(filepath: str) -> str:
    """Отправляет локальный файл пользователю."""
    global CURRENT_CHAT_ID
    if CURRENT_CHAT_ID:
        ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("file", filepath))
        short_path = os.path.basename(filepath)
        set_status(CURRENT_CHAT_ID, f"📤 <b>Отправляю файл:</b>\n<code>{html.escape(short_path)}</code>", show_abort=True)

    if not CURRENT_CHAT_ID: return "Ошибка: ID чата неизвестен."
    
    res = call_mcp_tool_sync("send_file_to_telegram", {"filepath": filepath})
    if res.startswith("__MCP_SEND_FILE_TRIGGER__:"):
        actual_path = res.split(":", 1)[1]
        try:
            with open(actual_path, 'rb') as f: bot.send_document(CURRENT_CHAT_ID, f)
            return f"Успех: Файл {actual_path} отправлен."
        except Exception as e: return f"Ошибка отправки файла: {str(e)}"
    return res

def delete_scheduled_task_tool(task_id: str) -> str:
    """Удаляет фоновую задачу по её ID."""
    global CURRENT_CHAT_ID
    if not CURRENT_CHAT_ID: return "Error: Chat ID unknown."
    ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("delete_task", task_id))
    return call_mcp_tool_sync("delete_scheduled_task_tool", {"chat_id": CURRENT_CHAT_ID, "task_id": task_id})

def list_my_tasks_tool() -> str:
    """Возвращает список всех активных фоновых задач текущего пользователя."""
    global CURRENT_CHAT_ID
    if not CURRENT_CHAT_ID: return "Error: Chat ID unknown."
    ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("list_tasks", "all"))
    return call_mcp_tool_sync("list_my_tasks_tool", {"chat_id": CURRENT_CHAT_ID})

def sleep_tool(seconds: int) -> str:
    """Приостанавливает выполнение логики. Максимум 300 сек."""
    global CURRENT_CHAT_ID
    try:
        sec_val = int(float(seconds))
    except:
        sec_val = 5
        
    if CURRENT_CHAT_ID:
        ACTION_LOGS.setdefault(CURRENT_CHAT_ID, []).append(("sleep", sec_val))
        set_status(CURRENT_CHAT_ID, f"💤 <b>ИИ ожидает {sec_val} сек...</b>", show_abort=True)
        
    max_sleep = min(sec_val, 300)
    for _ in range(max_sleep):
        if CURRENT_CHAT_ID and ABORT_FLAGS.get(CURRENT_CHAT_ID):
            return "Process interrupted by user during sleep."
        time.sleep(1)
        
    return f"Success: slept for {max_sleep} seconds."

# ─────────────────────────────────────────────
#  ЛОГИКА МОДЕЛЕЙ И УМНЫЙ ТРИММЕР
# ─────────────────────────────────────────────

def get_models_lists():
    raw_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
    priority, other, used_models = [], [], set()

    for p in PRIORITY_MODELS:
        if CURRENT_KEY_NUM in MODEL_RESTRICTED_KEYS.get(p, []): continue 
        search_str = p.lower().replace("sep-2025", "09-2025")
        best_match = None
        for m in raw_models:
            if m in used_models: continue
            clean_m = m.replace('models/', '').lower()
            if clean_m == search_str or clean_m == f"{search_str}-it":
                best_match = m
                break
            elif search_str in clean_m:
                if "tts" in clean_m or "image" in clean_m: continue
                if not best_match: best_match = m
        if best_match:
            priority.append(best_match)
            used_models.add(best_match)
            
            clean_best_match = best_match.replace('models/', '')
            if p in MODEL_RPM_LIMITS: MODEL_RPM_LIMITS[clean_best_match] = MODEL_RPM_LIMITS[p]
            if p in MODEL_TPM_LIMITS: MODEL_TPM_LIMITS[clean_best_match] = MODEL_TPM_LIMITS[p]
            if p in MODEL_RPD_LIMITS: MODEL_RPD_LIMITS[clean_best_match] = MODEL_RPD_LIMITS[p]
            if p in MODEL_RESTRICTED_KEYS: MODEL_RESTRICTED_KEYS[clean_best_match] = MODEL_RESTRICTED_KEYS[p]

    for m in raw_models:
        if m not in used_models:
            clean_m = m.replace('models/', '').lower()
            if CURRENT_KEY_NUM not in MODEL_RESTRICTED_KEYS.get(clean_m, []):
                other.append(m)

    return priority, other

def get_models_keyboard(show_all=False):
    global PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS
    if not PRIORITY_MODELS_CACHE and not OTHER_MODELS_CACHE:
        PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE = get_models_lists()
        AVAILABLE_MODELS = PRIORITY_MODELS_CACHE + OTHER_MODELS_CACHE

    markup = InlineKeyboardMarkup()
    for model_name in PRIORITY_MODELS_CACHE:
        clean_name = model_name.replace('models/', '')
        rpm_info = MODEL_RPM_LIMITS.get(clean_name)
        btn_text = f"{clean_name} (RPM:{rpm_info})" if rpm_info else clean_name
        markup.add(InlineKeyboardButton(text=btn_text, callback_data=f"mod_{model_name}"))

    if show_all:
        for model_name in OTHER_MODELS_CACHE:
            clean_name = model_name.replace('models/', '')
            rpm_info = MODEL_RPM_LIMITS.get(clean_name)
            btn_text = f"{clean_name} (RPM:{rpm_info})" if rpm_info else clean_name
            markup.add(InlineKeyboardButton(text=btn_text, callback_data=f"mod_{model_name}"))
    else:
        if OTHER_MODELS_CACHE:
            markup.add(InlineKeyboardButton(text="⬇️ Другие модели", callback_data="show_all_mods"))
    return markup

def init_models(model_name, role="admin", mode="auto"):
    global chat_agent, model_advisor
    is_gemma = "gemma" in model_name.lower()

    if is_gemma:
        model_agent = genai.GenerativeModel(model_name=model_name)
        chat_agent = model_agent.start_chat()
        model_advisor = genai.GenerativeModel(model_name=model_name)
    else:
        if role == "chat":
            model_agent = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=PROMPTS.get("CHAT_BOT", "")
            )
            chat_agent = model_agent.start_chat()
        else:
            sys_prompt = PROMPTS.get("GEMINI_ADMIN", "") + "\n\n[IMPORTANT] Return EXACTLY ONE tool call per response. Do NOT use parallel function calling."
            model_agent = genai.GenerativeModel(
                model_name=model_name,
                tools=[execute_bash, search_web_tool, download_file_tool, send_file_to_telegram, delete_scheduled_task_tool, list_my_tasks_tool, sleep_tool],
                system_instruction=sys_prompt
            )
            chat_agent = model_agent.start_chat(enable_automatic_function_calling=False)

        model_advisor = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=PROMPTS.get("GEMINI_ADVISOR", "")
        )

def trim_chat_history(agent, model_name=None):
    if not model_name: model_name = CURRENT_MODEL
    if not model_name: return
        
    clean_name = model_name.replace('models/', '')
    tpm_limit = MODEL_TPM_LIMITS.get(clean_name, 15000)
    
    if tpm_limit >= 200000: max_history = 40
    elif tpm_limit >= 100000: max_history = 20
    elif tpm_limit >= 30000: max_history = 10
    else: max_history = 6
        
    if not hasattr(agent, 'history') or len(agent.history) <= max_history:
        return
        
    cut_idx = len(agent.history) - max_history
    safe_idx = -1
    
    while cut_idx < len(agent.history):
        msg = agent.history[cut_idx]
        if msg.role == 'user':
            is_function_response = False
            for part in msg.parts:
                if hasattr(part, 'function_response') and part.function_response:
                    is_function_response = True
                    break
            if not is_function_response:
                safe_idx = cut_idx
                break 
        cut_idx += 1
        
    if safe_idx != -1:
        agent.history = agent.history[safe_idx:]

def get_gemma_react_prompt(clean_model_name):
    sys_str = "\n\n" + PROMPTS.get("GEMMA4_ADMIN_REACT", "") if "gemma-4" in clean_model_name.lower() else "\n\n" + PROMPTS.get("GEMMA_ADMIN_REACT", "")
    if "<SLEEP>" not in sys_str:
        sys_str += "\nЕсли нужно подождать, напиши <SLEEP>секунды</SLEEP>."
    return sys_str

# ─────────────────────────────────────────────
#  ФОНОВЫЙ ВЫПОЛНИТЕЛЬ ЗАДАЧ (SCHEDULER CALLBACK)
# ─────────────────────────────────────────────

def execute_scheduled_task(chat_id, prompt, model_name, task_id):
    try:
        ABORT_FLAGS[chat_id] = False
        msg_first = bot.send_message(chat_id, f"⏰ <b>Запуск фоновой задачи:</b> <code>{task_id}</code>", parse_mode='HTML')
        status_text = "🤖 <b>Выполняю фоновую задачу...</b>"
        set_status(chat_id, status_text, show_abort=True)
        
        task_specific_instructions = (
            f"\n\n[SYSTEM: BACKGROUND TASK RUNNER]\n"
            f"1. You are currently running as a background scheduled task.\n"
            f"2. Your unique Task ID is: {task_id}\n"
            f"3. State Management: You have no memory of previous runs. If you need to keep track of counts or data between executions, you MUST use the `execute_bash` tool to read/write to a text file in `/app/downloads/tasks/state_{task_id}.txt`.\n"
            f"4. Self-Deletion: If your instructions say to stop or delete the task after a certain condition, use the `delete_scheduled_task_tool(task_id)` passing your ID: {task_id}.\n"
            f"5. Try to combine your bash commands into a single script execution to save time. Do not make more than 1-2 bash calls per execution.\n"
            f"6. Complete the user's prompt using your tools, and provide the final report.\n"
            f"7. Return EXACTLY ONE tool call at a time. Do NOT use parallel function calling."
        )
        
        system_prompt = PROMPTS.get("GEMINI_ADMIN", "") + task_specific_instructions
        
        task_model = genai.GenerativeModel(
            model_name=model_name,
            tools=[execute_bash, search_web_tool, download_file_tool, send_file_to_telegram, delete_scheduled_task_tool, list_my_tasks_tool, sleep_tool],
            system_instruction=system_prompt
        )
        task_chat = task_model.start_chat(enable_automatic_function_calling=False) 
        
        global CURRENT_CHAT_ID
        CURRENT_CHAT_ID = chat_id
        
        ACTION_LOGS[chat_id] = []
        TURN_STATS[chat_id] = {"rpd": 0, "tpm": 0}
        CONSECUTIVE_SLEEPS[chat_id] = 0
        
        response = safe_send_message(task_chat, chat_id, prompt, status_text, model_name=model_name)
        
        parse_and_route_response(task_chat, chat_id, response, msg_first.message_id, prompt, is_background=True, model_name=model_name, task_id=task_id)
        
    except Exception as e:
        clear_status(chat_id)
        handle_api_error(e, chat_id, msg_first.message_id, model_name.replace('models/', ''))
        task.log_task_event(f"ERROR: Task ID={task_id} failed: {e}")

# ─────────────────────────────────────────────
#  ОБРАБОТЧИКИ КОМАНД
# ─────────────────────────────────────────────

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.from_user.id not in ADMIN_IDS: return
    bot.reply_to(message, "👋 Привет, Админ!\nВыбери модель Gemini:", reply_markup=get_models_keyboard())

@bot.message_handler(commands=['help'])
def send_help(message):
    if message.from_user.id not in ADMIN_IDS: return
    log_admin_action(message.from_user.id, "Команда /help")
    help_text = (
        "🤖 <b>Справка по командам AI-Админа:</b>\n\n"
        "🔸 /start — Выбор модели\n"
        "🔸 /help — Показать эту справку\n"
        "🔸 /gemini — Выбрать активную модель ИИ\n"
        "🔸 /changekey — Сменить текущий API-ключ Gemini\n"
        "🔸 /status — Посмотреть загрузку лимитов (RPM, TPM, RPD)\n"
        "🔸 /reload — Перезагрузить конфиги (models.txt, prompts.txt)\n"
        "🔸 /voice — Управление голосовыми ответами (Вкл/Выкл)\n"
        "🔸 /clear — Очистить память (контекст) текущей модели\n"
        "🔸 /search — Поиск по базам данных (csv) и архивам (zip, 7z)\n"
        "🔸 /task — Создать задачу по расписанию\n"
        "🔸 /tasks — Список активных задач\n"
        "🔸 /deltask — Удалить задачу\n\n"
        "💡 <b>Скрытые команды:</b>\n"
        "<code>!команда</code> — Выполнить bash-команду напрямую (без ИИ)\n"
        "<code>#запрос</code> — Выполнить запрос через ИИ-советника (вернет только команду)"
    )
    bot.reply_to(message, help_text, parse_mode='HTML')

@bot.message_handler(commands=['status'])
def status_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(text="🔑 KEY 1" + (" (Текущий)" if CURRENT_KEY_NUM == 1 else ""), callback_data="status_key_1"),
        InlineKeyboardButton(text="🔑 KEY 2" + (" (Текущий)" if CURRENT_KEY_NUM == 2 else ""), callback_data="status_key_2"),
        InlineKeyboardButton(text="🔑 KEY 3" + (" (Текущий)" if CURRENT_KEY_NUM == 3 else ""), callback_data="status_key_3")
    )
    bot.reply_to(message, "📊 Выберите ключ для просмотра статистики:", reply_markup=markup)

@bot.message_handler(commands=['gemini'])
def change_model(message):
    if message.from_user.id not in ADMIN_IDS: return
    bot.reply_to(message, "Выберите модель:", reply_markup=get_models_keyboard())

@bot.message_handler(commands=['reload'])
def reload_configs_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    try:
        load_models_config()
        load_prompts_config()
        global PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS
        PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS = [], [], [] 
        bot.reply_to(message, f"✅ Конфигурация обновлена!\nЗагружено <b>{len(PRIORITY_MODELS)}</b> моделей.\nПромпты: <b>{', '.join(PROMPTS.keys())}</b>.", parse_mode='HTML')
    except Exception as e: bot.reply_to(message, f"❌ Ошибка: {e}")

@bot.message_handler(commands=['changekey'])
def change_key_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(text="🔑 KEY 1" + (" (Активен)" if CURRENT_KEY_NUM == 1 else ""), callback_data="key_1"),
        InlineKeyboardButton(text="🔑 KEY 2" + (" (Активен)" if CURRENT_KEY_NUM == 2 else ""), callback_data="key_2"),
        InlineKeyboardButton(text="🔑 KEY 3" + (" (Активен)" if CURRENT_KEY_NUM == 3 else ""), callback_data="key_3")
    )
    bot.reply_to(message, "Выберите API-ключ для работы:", reply_markup=markup)

@bot.message_handler(commands=['voice'])
def voice_mode_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    is_active = VOICE_MODE.get(message.chat.id, False)
    status_text = "ВКЛЮЧЕН 🟢" if is_active else "ВЫКЛЮЧЕН 🔴"
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🟢 Включить", callback_data="voice_on"), InlineKeyboardButton("🔴 Выключить", callback_data="voice_off"))
    bot.reply_to(message, f"🎙 <b>Голосовой ответ ИИ</b>\n\nСейчас: <b>{status_text}</b>", reply_markup=markup, parse_mode='HTML')

@bot.message_handler(commands=['clear'])
def clear_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    global chat_agent, CURRENT_MODEL
    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Модель еще не выбрана. Память пуста.")
        return
    try:
        init_models(CURRENT_MODEL, role=MODEL_ROLE.get(message.chat.id, "admin"), mode=MODEL_MODE.get(message.chat.id, "auto"))
        bot.reply_to(message, "🧹 Контекст и память ИИ успешно очищены!")
    except Exception as e: bot.reply_to(message, f"❌ Ошибка: {e}")

# ─────────────────────────────────────────────
#  МЕНЕДЖМЕНТ ЗАДАЧ (TASKS)
# ─────────────────────────────────────────────

@bot.message_handler(commands=['tasks'])
def list_tasks_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    tasks = task.get_all_tasks(message.chat.id)
    if not tasks:
        bot.reply_to(message, "📂 Нет активных задач.")
        return
    
    msg_text = "📋 <b>Ваши активные задачи:</b>\n\n"
    for t in tasks:
        msg_text += f"ID: <code>{t['id']}</code>\nCRON: <code>{t['cron']}</code>\nМодель: {t['model'].replace('models/', '')}\nЗадача: <i>{t['prompt']}</i>\n\n"
    bot.reply_to(message, msg_text, parse_mode='HTML')

@bot.message_handler(commands=['deltask'])
def del_task_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ Формат команды: /deltask [ID]\nНапример: <code>/deltask a1b2c3d4</code>", parse_mode='HTML')
        return
        
    task_id = parts[1].strip()
    if task.delete_task(message.chat.id, task_id):
        bot.reply_to(message, f"✅ Задача <code>{task_id}</code> успешно удалена.", parse_mode='HTML')
    else:
        bot.reply_to(message, f"❌ Задача <code>{task_id}</code> не найдена.", parse_mode='HTML')

@bot.message_handler(commands=['task'])
def task_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Сначала выберите модель (/gemini), которая будет выполнять эту задачу.")
        return
        
    if "gemma" in CURRENT_MODEL.lower():
        bot.reply_to(message, "⚠️ Фоновые задачи по расписанию поддерживаются <b>ТОЛЬКО для моделей Gemini</b>, так как они требуют нативного Function Calling. Пожалуйста, смените модель.", parse_mode='HTML')
        return
        
    user_text = message.text.replace("/task", "").strip()
    if not user_text:
        bot.reply_to(message, "⚠️ Вы не указали задачу. Пример:\n<code>/task каждый день в 20:00 присылай мне выжимку новостей</code>", parse_mode='HTML')
        return
        
    msg_wait = bot.send_message(message.chat.id, "🧠 <i>Анализирую расписание...</i>", parse_mode='HTML')
    
    try:
        now_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S PT")
        parse_prompt = (
            "Convert the user's task request into a standard CRON expression and a clear system prompt for an AI agent. "
            "Return ONLY a raw JSON object with keys 'cron' and 'prompt'. No markdown formatting, no comments. "
            "CRON format: minute hour day month day_of_week. "
            f"The current time on the server is {now_str}. "
            "Create a CRON expression that triggers EXACTLY at the user's requested time (do NOT shift timezones, output the exact requested hours). "
            "IMPORTANT: If the user asks for random numbers or dynamic system data, instruct the AI to ACTUALLY execute bash commands (e.g. `echo $((1 + $RANDOM % 10000))`) using tools, instead of just guessing the text. "
            "If the user wants the task to stop after N times, explicitly instruct the AI to use `delete_scheduled_task_tool` when the condition is met. "
            f"User request: '{user_text}'"
        )
        
        parser_model = genai.GenerativeModel("gemini-2.5-flash") 
        res = parser_model.generate_content(parse_prompt)
        
        cleaned_json_text = res.text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed_data = json.loads(cleaned_json_text)
        
        cron_expr = parsed_data["cron"]
        ai_prompt = parsed_data["prompt"]
        
        task_id = task.add_task(message.chat.id, cron_expr, ai_prompt, CURRENT_MODEL, user_text)
        
        safe_edit_message(message.chat.id, msg_wait.message_id, 
            f"✅ <b>Задача успешно создана!</b>\n\n"
            f"ID: <code>{task_id}</code>\n"
            f"CRON: <code>{cron_expr}</code> (Время системное)\n"
            f"Модель: <b>{CURRENT_MODEL.replace('models/', '')}</b>\n"
            f"Инструкция: <i>{ai_prompt}</i>\n\n"
            f"Для удаления введите: /deltask {task_id}", 
            parse_mode='HTML'
        )
        
    except Exception as e:
        safe_edit_message(message.chat.id, msg_wait.message_id, f"❌ Не удалось распознать расписание. Ошибка: {e}\n\nПопробуйте сформулировать время более четко.")


@bot.message_handler(commands=['search'])
def search_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📂 Обычные базы", callback_data="search_type_regular"), InlineKeyboardButton("🗄 В архивах", callback_data="search_type_archive"))
    bot.reply_to(message, "Где искать?", reply_markup=markup)

def process_search_query(message, search_type="regular"):
    if message.from_user.id not in ADMIN_IDS: return
    query = (message.text or "").strip()
    if not query: return
    words = parse_search_query(query)
    if not words: return
    mode_text = "в архивах (.zip, .7z)" if search_type == "archive" else "в обычных базах (.csv)"
    msg_wait = bot.send_message(message.chat.id, f"🔍 Ищу {mode_text}: <code>{' | '.join(words)}</code>...", parse_mode='HTML')
    try:
        output = run_archive_search(words) if search_type == "archive" else run_grep_search(words)
        if not output:
            safe_edit_message(message.chat.id, msg_wait.message_id, "🤷‍♂️ По вашему запросу ничего не найдено.")
            return
        bot.delete_message(message.chat.id, msg_wait.message_id)
        formatted_chunks, clean_text_for_file = format_search_results(output, words)
        for chunk in formatted_chunks[:5]: bot.send_message(message.chat.id, f"<pre>{chunk.strip()}</pre>", parse_mode='HTML')
        if len(formatted_chunks) > 5:
            PENDING_SEARCH_RESULTS[message.chat.id] = clean_text_for_file
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📥 Скачать всё (.txt)", callback_data="download_search_txt"))
            bot.send_message(message.chat.id, f"⚠️ <b>Внимание:</b> Показано 5 сообщений. Остальной текст обрезан.\n\nСкачать полные результаты:", parse_mode='HTML', reply_markup=markup)
    except Exception as e: safe_edit_message(message.chat.id, msg_wait.message_id, f"❌ Ошибка поиска: {html.escape(str(e))}")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    if message.from_user.id not in ADMIN_IDS: return
    global CURRENT_CHAT_ID
    CURRENT_CHAT_ID = message.chat.id
    ABORT_FLAGS[message.chat.id] = False
    PENDING_FILES[message.chat.id] = {'file_id': message.document.file_id, 'file_name': message.document.file_name, 'mime_type': message.document.mime_type}
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("✅ Да", callback_data="file_yes"), InlineKeyboardButton("❌ Нет", callback_data="file_no"))
    markup.row(InlineKeyboardButton("🧠 Обработать ИИ", callback_data="file_ai"))
    bot.reply_to(message, f"📥 Загрузить файл <b>{html.escape(message.document.file_name)}</b> на сервер?", reply_markup=markup, parse_mode='HTML')

# ─────────────────────────────────────────────
#  ЕДИНЫЙ ПАРСЕР И МАРШРУТИЗАТОР ОТВЕТОВ
# ─────────────────────────────────────────────

def parse_and_route_response(agent, chat_id, response, first_msg_id, original_text, is_background=False, model_name=None, task_id=None):
    if not model_name: model_name = CURRENT_MODEL
    clean_model_name = model_name.replace('models/', '') if model_name else ""

    if ABORT_FLAGS.get(chat_id):
        finish_response(chat_id, "🛑 <b>Выполнение принудительно остановлено.</b>", first_msg_id, clean_model_name, is_background, task_id)
        return

    is_gemma = "gemma" in clean_model_name.lower()
    role = MODEL_ROLE.get(chat_id, "admin")
    
    if role == "chat":
        finish_response(chat_id, response.text, first_msg_id, clean_model_name, is_background, task_id)
        return

    action = None
    if is_gemma:
        response_text = response.text or ""
        bash_match = re.search(r'<BASH>(.*?)</BASH>', response_text, re.DOTALL | re.IGNORECASE)
        search_match = re.search(r'<SEARCH>(.*?)</SEARCH>', response_text, re.DOTALL | re.IGNORECASE)
        dl_match = re.search(r'<DOWNLOAD>(.*?)</DOWNLOAD>', response_text, re.DOTALL | re.IGNORECASE)
        file_match = re.search(r'<FILE>(.*?)</FILE>', response_text, re.DOTALL | re.IGNORECASE)
        sleep_match = re.search(r'<SLEEP>(.*?)</SLEEP>', response_text, re.DOTALL | re.IGNORECASE)
        
        if bash_match: action = {"type": "react", "name": "bash", "val": bash_match.group(1).strip(), "disp_name": "Команда BASH", "disp_val": bash_match.group(1).strip()}
        elif search_match: action = {"type": "react", "name": "search", "val": search_match.group(1).strip(), "disp_name": "Поиск в сети", "disp_val": search_match.group(1).strip()}
        elif dl_match: action = {"type": "react", "name": "download", "val": dl_match.group(1).strip(), "disp_name": "Скачивание файла", "disp_val": dl_match.group(1).strip()}
        elif file_match: action = {"type": "react", "name": "file", "val": file_match.group(1).strip(), "disp_name": "Отправка файла в чат", "disp_val": file_match.group(1).strip()}
        elif sleep_match: action = {"type": "react", "name": "sleep", "val": sleep_match.group(1).strip(), "disp_name": "Пауза/Сон", "disp_val": sleep_match.group(1).strip()}
    else:
        if response.parts:
            function_calls = []
            for part in response.parts:
                if hasattr(part, 'function_call') and part.function_call:
                    function_calls.append(part.function_call)
            
            if function_calls:
                first_call = function_calls[0]
                fn_name = first_call.name
                fn_args = {key: val for key, val in first_call.args.items()}
                disp_name = fn_name
                disp_val = str(fn_args)
                if fn_name == "execute_bash": disp_name, disp_val = "Команда BASH", fn_args.get("command", "")
                elif fn_name == "search_web_tool": disp_name, disp_val = "Поиск в сети", fn_args.get("query", "")
                elif fn_name == "download_file_tool": disp_name, disp_val = "Скачивание файла", fn_args.get("url", "")
                elif fn_name == "send_file_to_telegram": disp_name, disp_val = "Отправка файла в чат", fn_args.get("filepath", "")
                elif fn_name == "delete_scheduled_task_tool": disp_name, disp_val = "Удаление задачи", fn_args.get("task_id", "")
                elif fn_name == "list_my_tasks_tool": disp_name, disp_val = "Список задач", ""
                elif fn_name == "sleep_tool": disp_name, disp_val = "Пауза/Сон", fn_args.get("seconds", "")
                
                action = {"type": "native", "name": fn_name, "args": fn_args, "disp_name": disp_name, "disp_val": disp_val, "all_calls": function_calls}

    if action:
        action["msg_id"] = first_msg_id
        action["orig_text"] = original_text
        process_action_request(agent, chat_id, action, is_background, model_name, task_id)
    else:
        finish_response(chat_id, response.text, first_msg_id, clean_model_name, is_background, task_id)

def finish_response(chat_id, text, msg_id, clean_model_name, is_background=False, task_id=None):
    markup = None
    if ACTION_LOGS.get(chat_id):
        stats = TURN_STATS.get(chat_id, {"rpd": 0, "tpm": 0})
        LAST_ACTIONS[msg_id] = {
            "actions": ACTION_LOGS[chat_id].copy(),
            "stats": stats
        }
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛠 Выполненные действия", callback_data=f"show_acts_{msg_id}"))
    
    if is_background and task_id:
        prefix = f"⏰ <b>Запуск фоновой задачи:</b> <code>{task_id}</code>\n\n<b>Отчет ({clean_model_name}):</b>\n\n"
        used_actions = ACTION_LOGS.get(chat_id, [])
        log_actions_str = ", ".join([f"{a[0]}: {str(a[1])[:30]}..." for a in used_actions]) if used_actions else "No tools used"
        task.log_task_event(f"EXECUTE: Chat={chat_id} | ID={task_id} | Actions: [{log_actions_str}]")
    else:
        prefix = f"<b>{clean_model_name}:</b>\n\n"
        
    send_long_text(chat_id, text, first_msg_id=msg_id, prefix=prefix, reply_markup=markup)
    if VOICE_MODE.get(chat_id) and not is_background: 
        generate_and_send_voice(chat_id, text)
    clear_status(chat_id)

def process_action_request(agent, chat_id, action, is_background=False, model_name=None, task_id=None):
    mode = MODEL_MODE.get(chat_id, "semi")
    
    if action["name"] in ["sleep", "sleep_tool"]:
        CONSECUTIVE_SLEEPS[chat_id] = CONSECUTIVE_SLEEPS.get(chat_id, 0) + 1
        if CONSECUTIVE_SLEEPS[chat_id] > 3:
            action["is_sleep_error"] = True 
    else:
        CONSECUTIVE_SLEEPS[chat_id] = 0

    if mode == "semi" and not is_background:
        PENDING_ACTION[chat_id] = action
        clear_status(chat_id)
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("✅ Выполнить", callback_data=f"act_yes_{chat_id}"), InlineKeyboardButton("❌ Отмена", callback_data=f"act_no_{chat_id}"))
        bot.send_message(chat_id, f"🤖 <b>Запрос действия ИИ:</b>\n\n{action['disp_name']}:\n<code>{html.escape(action['disp_val'])}</code>", parse_mode='HTML', reply_markup=markup)
    else:
        execute_pending_action(agent, chat_id, action, is_background, model_name, task_id)

def execute_pending_action(agent, chat_id, action, is_background=False, model_name=None, task_id=None):
    clean_model_name = model_name.replace('models/', '') if model_name else ""
    if ABORT_FLAGS.get(chat_id):
        finish_response(chat_id, "🛑 <b>Выполнение принудительно остановлено.</b>\nНажмите кнопку ниже, чтобы посмотреть выполненные действия.", action["msg_id"], clean_model_name, is_background, task_id)
        return
        
    res = "Нет результата."
    
    if action.get("is_sleep_error"):
        res = "ERROR: You have slept 3 times consecutively. You are not allowed to sleep again. You must perform a different action or give the final text response."
    elif action["type"] == "react":
        if action["name"] == "bash": res = execute_bash(action["val"])
        elif action["name"] == "search": res = search_web_tool(action["val"])
        elif action["name"] == "download": res = download_file_tool(action["val"])
        elif action["name"] == "file": res = send_file_to_telegram(action["val"])
        elif action["name"] == "sleep": res = sleep_tool(action["val"])
    elif action["type"] == "native":
        fn_name = action["name"]
        args = action["args"]
        if fn_name == "execute_bash": res = execute_bash(args.get("command", ""))
        elif fn_name == "search_web_tool": res = search_web_tool(args.get("query", ""))
        elif fn_name == "download_file_tool": res = download_file_tool(args.get("url", ""))
        elif fn_name == "send_file_to_telegram": res = send_file_to_telegram(args.get("filepath", ""))
        elif fn_name == "delete_scheduled_task_tool": res = delete_scheduled_task_tool(args.get("task_id", ""))
        elif fn_name == "list_my_tasks_tool": res = list_my_tasks_tool()
        elif fn_name == "sleep_tool": res = sleep_tool(args.get("seconds", 5))
        
    if not res: res = "Success (No output)"
        
    status_text = "🧠 <b>Анализирую результат...</b>"
    set_status(chat_id, status_text, show_abort=True)
    
    if ABORT_FLAGS.get(chat_id):
        finish_response(chat_id, f"🛑 <b>Выполнение принудительно остановлено.</b>\nПоследний инструмент ({action['name']}) отработал, но дальнейший анализ прерван.", action["msg_id"], clean_model_name, is_background, task_id)
        return
    
    try:
        if action["type"] == "react":
            followup_prompt = f"РЕЗУЛЬТАТ ВЫПОЛНЕНИЯ ({action['name']}):\n{res}\nОсновываясь на этом, дай финальный ответ или вызови новый тег."
            response = safe_send_message(agent, chat_id, followup_prompt, status_text, model_name=model_name)
            if hasattr(agent, 'history') and len(agent.history) >= 2:
                agent.history[-2].parts[0].text = f"[Система сообщила результат действия {action['name']}]"
                
        elif action["type"] == "native":
            func_responses = [{"function_response": {"name": action["name"], "response": {"result": str(res)}}}]
            if "all_calls" in action and len(action["all_calls"]) > 1:
                for extra in action["all_calls"][1:]:
                    func_responses.append({"function_response": {"name": extra.name, "response": {"result": "Ignored. Execute ONE function at a time."}}})
            response = safe_send_message(agent, chat_id, func_responses, status_text, model_name=model_name)
            
        parse_and_route_response(agent, chat_id, response, action["msg_id"], action["orig_text"], is_background, model_name, task_id)
    except Exception as e:
        clear_status(chat_id)
        handle_api_error(e, chat_id, action["msg_id"], clean_model_name)
        if is_background and task_id:
            task.log_task_event(f"ERROR: Task ID={task_id} failed: {e}")

# ─────────────────────────────────────────────
#  ОБРАБОТКА CALLBACKS (КНОПКИ)
# ─────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.from_user.id not in ADMIN_IDS: return
    global CURRENT_MODEL, CURRENT_KEY_NUM, PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS
    global CURRENT_CHAT_ID, MODEL_ROLE, MODEL_MODE, PENDING_ACTION
    data = call.data

    if data.startswith("abort_"):
        target_chat_id = int(data.replace("abort_", ""))
        ABORT_FLAGS[target_chat_id] = True
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id, 
                message_id=call.message.message_id, 
                text="🛑 <b>Остановка выполнения...</b> (Ожидаю завершения текущего процесса)", 
                parse_mode='HTML'
            )
        except: pass
        bot.answer_callback_query(call.id, "Сигнал на остановку отправлен!")
        return

    if data.startswith("status_key_"):
        key_num = int(data.split("_")[2])
        
        now = time.time()
        history_rpm = API_REQUEST_HISTORY[key_num]
        valid_rpm = [t for t in history_rpm if now - t <= 60.5]
        
        history_tpm = API_TOKEN_HISTORY[key_num]
        valid_tpm = sum(count for t, count in history_tpm if now - t <= 60.5)
        
        today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        key_data = API_RPD_HISTORY.get(key_num, {"date": today_str, "usage": {}})
        if key_data["date"] != today_str:
            key_data["date"] = today_str
            key_data["usage"] = {}
            
        text = f"📊 <b>Статистика KEY {key_num}</b>\n\n"
        text += f"⚡ <b>Текущая нагрузка (за 1 мин):</b>\n"
        text += f"• API Запросов (RPM): {len(valid_rpm)}\n"
        text += f"• Токенов (TPM): ~{valid_tpm}\n\n"
        
        text += f"📅 <b>Использование за день (RPD) [PT]:</b>\n"
        if not key_data["usage"]:
            text += "<i>Нет данных за сегодня.</i>\n"
        else:
            for mod, count in key_data["usage"].items():
                limit = MODEL_RPD_LIMITS.get(mod, '∞')
                text += f"• <code>{mod}</code>: {count} / {limit}\n"
                
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 Обновить", callback_data=f"status_key_{key_num}"))
        
        try: bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
        except Exception: pass
        return

    if data in ["search_type_regular", "search_type_archive"]:
        search_type = "archive" if "archive" in data else "regular"
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        mode_text = "В архивах (.zip, .7z)" if search_type == "archive" else "Обычные базы (.csv)"
        msg = bot.send_message(call.message.chat.id, f"🔍 Режим: <b>{mode_text}</b>\n\nВведите поисковый запрос:", parse_mode='HTML')
        bot.register_next_step_handler(msg, process_search_query, search_type=search_type)
        return

    if data == "voice_on":
        VOICE_MODE[call.message.chat.id] = True
        safe_edit_message(call.message.chat.id, call.message.message_id, "🟢 Голосовой режим <b>ВКЛЮЧЕН</b>.", parse_mode='HTML')
        return
    if data == "voice_off":
        VOICE_MODE[call.message.chat.id] = False
        safe_edit_message(call.message.chat.id, call.message.message_id, "🔴 Голосовой режим <b>ВЫКЛЮЧЕН</b>.", parse_mode='HTML')
        return
    if data == "hide_message":
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        return

    if data.startswith("show_acts_"):
        try: target_msg_id = int(data.split("_")[2])
        except: target_msg_id = 0
        action_data = LAST_ACTIONS.get(target_msg_id)
        
        if not action_data:
            bot.answer_callback_query(call.id, "❌ Данные об этих действиях устарели.", show_alert=True)
            return

        if isinstance(action_data, list):
            actions = action_data
            stats = {"rpd": "?", "tpm": "?"}
        else:
            actions = action_data.get("actions", [])
            stats = action_data.get("stats", {"rpd": "?", "tpm": "?"})

        log_text = ""
        bash_commands = []
        for act_type, act_val in actions:
            if act_type == "bash": bash_commands.append(act_val)
            elif act_type == "search": log_text += f"🌐 <b>Поиск:</b> <i>{html.escape(act_val)}</i>\n"
            elif act_type == "download": log_text += f"⬇️ <b>Скачан файл:</b> <i>{html.escape(act_val)}</i>\n"
            elif act_type == "file": log_text += f"📤 <b>Отправлен файл:</b> <i>{html.escape(act_val)}</i>\n"
            elif act_type == "delete_task": log_text += f"🗑 <b>Удалил задачу:</b> <i>ID {html.escape(act_val)}</i>\n"
            elif act_type == "list_tasks": log_text += f"📋 <b>Просмотрел активные задачи</b>\n"
            elif act_type == "sleep": log_text += f"💤 <b>Пауза:</b> <i>{act_val} сек.</i>\n"

        if bash_commands:
            bash_str = "\n".join(bash_commands)
            log_text += f'\n<pre><code class="language-bash">{html.escape(bash_str)}</code></pre>'

        log_text += f"\n📊 <b>Затраты на запрос:</b>\n"
        log_text += f"• API Запросов: {stats['rpd']}\n"
        log_text += f"• Токенов: {stats['tpm']}\n"

        hide_markup = InlineKeyboardMarkup()
        hide_markup.add(InlineKeyboardButton("❌ Скрыть", callback_data="hide_message"))
        bot.send_message(call.message.chat.id, log_text.strip(), parse_mode='HTML', reply_markup=hide_markup)
        bot.answer_callback_query(call.id)
        return

    if data == "download_search_txt":
        full_text = PENDING_SEARCH_RESULTS.get(call.message.chat.id)
        if not full_text:
            bot.answer_callback_query(call.id, "❌ Результаты поиска устарели.", show_alert=True)
            return
        safe_edit_message(call.message.chat.id, call.message.message_id, "⏳ Формирую файл...")
        temp_filename = f"search_results_temp_{call.message.chat.id}.txt"
        try:
            with open(temp_filename, "w", encoding="utf-8") as f: f.write(full_text)
            with open(temp_filename, "rb") as f: bot.send_document(call.message.chat.id, f, caption="📁 Полные результаты поиска")
            safe_edit_message(call.message.chat.id, call.message.message_id, "✅ Файл успешно отправлен.")
        except Exception as e: safe_edit_message(call.message.chat.id, call.message.message_id, f"❌ Ошибка: {str(e)}")
        finally:
            if os.path.exists(temp_filename): os.remove(temp_filename)
        return

    if data == "show_all_mods":
        safe_edit_message(call.message.chat.id, call.message.message_id, "Выберите модель:", reply_markup=get_models_keyboard(show_all=True))
        return

    if data.startswith("key_"):
        key_num = int(data.split("_")[1])
        target_key = API_KEY_1 if key_num == 1 else (API_KEY_2 if key_num == 2 else API_KEY_3)
        if not target_key:
            bot.answer_callback_query(call.id, f"❌ KEY {key_num} type= не задан!", show_alert=True)
            return
        CURRENT_KEY_NUM = key_num
        genai.configure(api_key=target_key)
        PRIORITY_MODELS_CACHE, OTHER_MODELS_CACHE, AVAILABLE_MODELS, CURRENT_MODEL = [], [], [], None
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.send_message(call.message.chat.id, f"✅ Активен <b>KEY {key_num}</b>.\nВызовите /gemini для выбора модели.", parse_mode='HTML')
        return

    if data.startswith("mod_"):
        CURRENT_MODEL = data.replace("mod_", "")
        clean_name = CURRENT_MODEL.replace('models/', '')
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🛠 Админ", callback_data="role_admin"), InlineKeyboardButton("💬 Чат-бот", callback_data="role_chat"))
        bot.send_message(call.message.chat.id, f"Выбрана модель <b>{clean_name}</b>.\nВыберите роль ИИ:", reply_markup=markup, parse_mode='HTML')
        return

    if data.startswith("role_"):
        role = data.replace("role_", "")
        MODEL_ROLE[call.message.chat.id] = role
        clean_name = CURRENT_MODEL.replace('models/', '') if CURRENT_MODEL else ""
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        
        if role == "admin":
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⚡ Авто", callback_data="mode_auto"), InlineKeyboardButton("🛑 Полуавтомат", callback_data="mode_semi"))
            bot.send_message(call.message.chat.id, f"Выберите режим выполнения команд для <b>{clean_name}</b>:", parse_mode='HTML', reply_markup=markup)
        else:
            MODEL_MODE[call.message.chat.id] = "auto"
            init_models(CURRENT_MODEL, role="chat", mode="auto")
            bot.send_message(call.message.chat.id, f"✅ <b>{clean_name}</b> запущен в роли: <b>Чат-бот</b>", parse_mode='HTML')
        return

    if data.startswith("mode_"):
        mode = data.replace("mode_", "")
        MODEL_MODE[call.message.chat.id] = mode
        clean_name = CURRENT_MODEL.replace('models/', '') if CURRENT_MODEL else ""
        init_models(CURRENT_MODEL, role="admin", mode=mode)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        m_text = "АВТО (выполняет функции сам)" if mode == "auto" else "ПОЛУАВТОМАТ (спрашивает разрешение)"
        bot.send_message(call.message.chat.id, f"✅ <b>{clean_name}</b> запущен в роли: <b>Админ -> {m_text}</b>", parse_mode='HTML')
        return

    if data.startswith("act_yes_"):
        action = PENDING_ACTION.get(call.message.chat.id)
        if not action:
            bot.answer_callback_query(call.id, "❌ Действие устарело.", show_alert=True)
            return
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        execute_pending_action(chat_agent, call.message.chat.id, action, is_background=False, model_name=CURRENT_MODEL)
        return

    if data.startswith("act_no_"):
        action = PENDING_ACTION.pop(call.message.chat.id, None)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.send_message(call.message.chat.id, "❌ Действие отменено пользователем.")
        
        if action:
            status_text = "🧠 <b>Сообщаю об отмене...</b>"
            set_status(call.message.chat.id, status_text)
            try:
                if action["type"] == "react":
                    resp = safe_send_message(chat_agent, call.message.chat.id, "ПОЛЬЗОВАТЕЛЬ ЗАПРЕТИЛ ВЫПОЛНЕНИЕ ЭТОЙ ОПЕРАЦИИ. Ответь пользователю.", status_text)
                elif action["type"] == "native":
                    func_responses = [{"function_response": {"name": action["name"], "response": {"result": "ERROR: User denied permission to execute."}}}]
                    if "all_calls" in action and len(action["all_calls"]) > 1:
                        for extra in action["all_calls"][1:]:
                            func_responses.append({"function_response": {"name": extra.name, "response": {"result": "Ignored."}}})
                    resp = safe_send_message(chat_agent, call.message.chat.id, func_responses, status_text, model_name=CURRENT_MODEL)
                parse_and_route_response(chat_agent, call.message.chat.id, resp, action["msg_id"], action["orig_text"], is_background=False, model_name=CURRENT_MODEL)
            except Exception as e:
                clear_status(call.message.chat.id)
                handle_api_error(e, call.message.chat.id, action["msg_id"], CURRENT_MODEL.replace('models/', '') if CURRENT_MODEL else "")
        return

    if data in ["file_yes", "file_no", "file_ai"]:
        file_info_dict = PENDING_FILES.get(call.message.chat.id)
        if data == "file_no":
            safe_edit_message(call.message.chat.id, call.message.message_id, "❌ Отменено.")
            PENDING_FILES.pop(call.message.chat.id, None)
            return
        if not file_info_dict:
            bot.answer_callback_query(call.id, "❌ Файл устарел.", show_alert=True)
            return

        if data == "file_yes":
            safe_edit_message(call.message.chat.id, call.message.message_id, "⏳ Сохраняю...")
            try:
                file_info = bot.get_file(file_info_dict['file_id'])
                downloaded_file = bot.download_file(file_info.file_path)
                os.makedirs("/app/downloads", exist_ok=True)
                save_path = os.path.join("/app/downloads", file_info_dict['file_name'])
                with open(save_path, 'wb') as new_file: new_file.write(downloaded_file)
                safe_edit_message(call.message.chat.id, call.message.message_id, f"✅ Файл сохранен:\n<code>{html.escape(save_path)}</code>")
            except Exception as e: safe_edit_message(call.message.chat.id, call.message.message_id, f"❌ Ошибка: {e}")
            PENDING_FILES.pop(call.message.chat.id, None)
            return

        if data == "file_ai":
            if not CURRENT_MODEL:
                bot.answer_callback_query(call.id, "⚠️ Выберите модель (/gemini)!", show_alert=True)
                return
            clean_name = CURRENT_MODEL.replace('models/', '')
            is_gemma = "gemma" in clean_name.lower()
            CURRENT_CHAT_ID = call.message.chat.id
            ABORT_FLAGS[CURRENT_CHAT_ID] = False
            ACTION_LOGS[CURRENT_CHAT_ID] = []
            TURN_STATS[CURRENT_CHAT_ID] = {"rpd": 0, "tpm": 0}
            STATUS_MSG.pop(CURRENT_CHAT_ID, None)  
            safe_edit_message(call.message.chat.id, call.message.message_id, f"<b>{clean_name}:</b>\n🧠 Читаю файл...")
            status_text = "🧠 <b>Анализирую файл...</b>"
            set_status(call.message.chat.id, status_text, show_abort=True)

            try:
                file_info = bot.get_file(file_info_dict['file_id'])
                temp_file_name = f"temp_ai_{file_info_dict['file_name']}"
                with open(temp_file_name, 'wb') as new_file: new_file.write(bot.download_file(file_info.file_path))
                mime = file_info_dict['mime_type']
                gemini_file = genai.upload_file(path=temp_file_name, mime_type=mime) if mime else genai.upload_file(path=temp_file_name)

                if is_gemma:
                    response = safe_send_message(chat_agent, call.message.chat.id, "Я текстовая модель Gemma, я пока не умею читать файлы напрямую.", status_text, model_name=CURRENT_MODEL)
                    parse_and_route_response(chat_agent, call.message.chat.id, response, call.message.message_id, "file", is_background=False, model_name=CURRENT_MODEL)
                else:
                    response = safe_send_message(chat_agent, call.message.chat.id, [gemini_file, "Проанализируй этот файл. Расскажи, что в нём, либо выполни инструкции."], status_text, model_name=CURRENT_MODEL)
                    parse_and_route_response(chat_agent, call.message.chat.id, response, call.message.message_id, "file", is_background=False, model_name=CURRENT_MODEL)
                os.remove(temp_file_name)

            except Exception as e:
                clear_status(call.message.chat.id)
                handle_api_error(e, call.message.chat.id, call.message.message_id, clean_name)
            PENDING_FILES.pop(call.message.chat.id, None)
            return

# ─────────────────────────────────────────────
#  ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ─────────────────────────────────────────────

@bot.message_handler(content_types=['voice', 'text', 'photo'])
def handle_message(message):
    if message.from_user.id not in ADMIN_IDS: return

    global CURRENT_CHAT_ID
    CURRENT_CHAT_ID = message.chat.id
    ABORT_FLAGS[message.chat.id] = False
    text = (message.text or message.caption or "").strip()

    if text.startswith('/'):
        cmd = text.split()[0].lower()
        if cmd == '/voice': voice_mode_cmd(message)
        elif cmd == '/search': search_cmd(message)
        elif cmd == '/clear': clear_cmd(message)
        elif cmd == '/gemini': change_model(message)
        elif cmd == '/changekey': change_key_cmd(message)
        elif cmd == '/status': status_cmd(message)
        elif cmd == '/reload': reload_configs_cmd(message)
        elif cmd == '/help': send_help(message)
        elif cmd == '/task': task_cmd(message)
        elif cmd == '/tasks': list_tasks_cmd(message)
        elif cmd == '/deltask': del_task_cmd(message)
        elif cmd == '/start': send_welcome(message)
        else: bot.reply_to(message, "⚠️ Неизвестная команда. Введите /help для справки.")
        return

    if not CURRENT_MODEL:
        bot.reply_to(message, "⚠️ Сначала выберите модель (/gemini)", reply_markup=get_models_keyboard())
        return

    pending = PENDING_ACTION.pop(message.chat.id, None)
    if pending and pending.get("type") == "native":
        try: chat_agent.send_message({"function_response": {"name": pending["name"], "response": {"result": "Отменено пользователем (написал новое сообщение)"}}})
        except: pass

    clean_model_name = CURRENT_MODEL.replace('models/', '')
    is_gemma = "gemma" in clean_model_name.lower()
    role = MODEL_ROLE.get(message.chat.id, "admin")

    is_voice = message.content_type == 'voice'
    is_photo = message.content_type == 'photo'

    if not is_voice and not is_photo:
        if text.startswith('!'):
            cmd = text[1:].strip()
            bot.send_message(message.chat.id, f"⚡ Выполняю напрямую:\n<code>{html.escape(cmd)}</code>", parse_mode='HTML')
            result = execute_bash(cmd)
            send_long_text(message.chat.id, result, is_code=True)
            return

        if text.startswith('#'):
            task_cmd = text[1:].strip()
            msg_first = bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
            status_text = "🧠 <b>Думаю...</b>"
            set_status(message.chat.id, status_text, show_abort=True)
            
            TURN_STATS[message.chat.id] = {"rpd": 0, "tpm": 0}
            CONSECUTIVE_SLEEPS[message.chat.id] = 0
            
            try:
                response = safe_send_message(model_advisor, message.chat.id, task_cmd, status_text, is_advisor=True, model_name=CURRENT_MODEL)
                clear_status(message.chat.id)
                send_long_text(message.chat.id, response.text, first_msg_id=msg_first.message_id, is_code=True)
            except Exception as e:
                clear_status(message.chat.id)
                handle_api_error(e, message.chat.id, msg_first.message_id, clean_model_name)
            return

    ACTION_LOGS[CURRENT_CHAT_ID] = []
    STATUS_MSG.pop(CURRENT_CHAT_ID, None)
    TURN_STATS[CURRENT_CHAT_ID] = {"rpd": 0, "tpm": 0}
    CONSECUTIVE_SLEEPS[CURRENT_CHAT_ID] = 0

    msg_first = bot.send_message(message.chat.id, f"<b>{clean_model_name}:</b>", parse_mode='HTML')
    status_text = "🤖 <b>Обрабатываю запрос...</b>"
    set_status(message.chat.id, status_text, show_abort=True)

    try:
        if is_voice:
            set_status(message.chat.id, "🎙 <b>Слушаю голосовое сообщение...</b>", show_abort=True)
            file_info = bot.get_file(message.voice.file_id)
            voice_path = f"temp_voice_{message.message_id}.ogg"
            with open(voice_path, 'wb') as new_file: new_file.write(bot.download_file(file_info.file_path))

            audio_file = genai.upload_file(path=voice_path, mime_type="audio/ogg")
            status_text = "🧠 <b>Анализирую аудио...</b>"
            set_status(message.chat.id, status_text, show_abort=True)
            
            if is_gemma:
                response = safe_send_message(chat_agent, message.chat.id, "Я текстовая модель Gemma и не умею слушать звук.", status_text, model_name=CURRENT_MODEL)
            else:
                base_prompt = "Прослушай этот аудиофайл. Если в нём звучит команда для сервера — выполни её. Если обычный разговор — ответь."
                prompt = f"{text}\n\n{base_prompt}" if text else base_prompt
                response = safe_send_message(chat_agent, message.chat.id, [audio_file, prompt], status_text, model_name=CURRENT_MODEL)

            os.remove(voice_path)

        elif is_photo:
            set_status(message.chat.id, "🖼 <b>Анализирую изображение...</b>", show_abort=True)
            file_info = bot.get_file(message.photo[-1].file_id)
            photo_path = f"temp_photo_{message.message_id}.jpg"
            with open(photo_path, 'wb') as new_file: new_file.write(bot.download_file(file_info.file_path))

            img_file = genai.upload_file(path=photo_path)
            status_text = "🖼 <b>Анализирую изображение...</b>"
            set_status(message.chat.id, status_text, show_abort=True)
            
            if is_gemma:
                response = safe_send_message(chat_agent, message.chat.id, "Я текстовая модель Gemma и не умею смотреть картинки.", status_text, model_name=CURRENT_MODEL)
            else:
                prompt = text if text else "Проанализируй это изображение."
                response = safe_send_message(chat_agent, message.chat.id, [img_file, prompt], status_text, model_name=CURRENT_MODEL)

            os.remove(photo_path)

        else:
            if is_gemma and role == "admin":
                react_sys = get_gemma_react_prompt(clean_model_name)
                final_text = text + react_sys
                response = safe_send_message(chat_agent, message.chat.id, final_text, status_text, model_name=CURRENT_MODEL)
                
                if hasattr(chat_agent, 'history') and len(chat_agent.history) >= 2:
                    chat_agent.history[-2].parts[0].text = text 
                
            else:
                response = safe_send_message(chat_agent, message.chat.id, text, status_text, model_name=CURRENT_MODEL)

        parse_and_route_response(chat_agent, message.chat.id, response, msg_first.message_id, text, is_background=False, model_name=CURRENT_MODEL)

    except Exception as e:
        clear_status(message.chat.id)
        handle_api_error(e, message.chat.id, msg_first.message_id, clean_model_name)

if __name__ == '__main__':
    print("Подключение к MCP серверу...")
    asyncio.run_coroutine_threadsafe(_init_mcp_async(), mcp_loop).result()
    task.init_scheduler(execute_scheduled_task)
    print(f"AI-Админ запущен. Допущено админов: {len(ADMIN_IDS)}.")
    bot.polling(none_stop=True)
    
