import os
import asyncio
import html
import tempfile
import subprocess
import json
import edge_tts
import google.generativeai as genai
from datetime import datetime
from zoneinfo import ZoneInfo
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Наши модули
from ai_client import AIManager
import search
import task
import markdown

TG_TOKEN = os.getenv("TG_TOKEN")
ADMIN_IDS = set()
for env_var in ["ADMIN_ID", "ADMIN2_ID", "ADMIN3_ID"]:
    val = os.getenv(env_var)
    if val and val.strip().lstrip('-').isdigit():
        ADMIN_IDS.add(int(val.strip()))

bot = AsyncTeleBot(TG_TOKEN)
ai_manager = AIManager()

# ─────────────────────────────────────────────
#  СОСТОЯНИЯ И UI ТРЕКЕРЫ (ВОЗВРАЩЕНО ИЗ ОРИГИНАЛА)
# ─────────────────────────────────────────────
STATUS_MSG = {}
ABORT_FLAGS = {}
VOICE_MODE = {}
MODEL_MODE = {}  # auto/semi
PENDING_FILES = {}
PENDING_SEARCH_RESULTS = {}
ACTION_LOGS = {}
LAST_ACTIONS = {}
TURN_STATS = {}

# Для асинхронного полуавтомата (ожидание нажатия кнопки)
PENDING_EVENTS = {}
PENDING_APPROVAL = {}

async def set_status(chat_id, text, show_abort=False):
    markup = None
    if show_abort:
        markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🛑 Аварийный стоп", callback_data=f"abort_{chat_id}"))
    msg_id = STATUS_MSG.get(chat_id)
    if msg_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode='HTML', reply_markup=markup)
            return
        except Exception as e:
            if "is not modified" not in str(e).lower(): STATUS_MSG.pop(chat_id, None)
    try:
        msg = await bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=markup)
        STATUS_MSG[chat_id] = msg.message_id
    except: pass

async def clear_status(chat_id):
    msg_id = STATUS_MSG.pop(chat_id, None)
    if msg_id:
        try: await bot.delete_message(chat_id, msg_id)
        except: pass

async def send_response(chat_id, text, model_name="AI", msg_id=None):
    markup = None
    if ACTION_LOGS.get(chat_id) and msg_id:
        LAST_ACTIONS[msg_id] = {"actions": ACTION_LOGS[chat_id].copy(), "stats": TURN_STATS.get(chat_id, {"rpd": 0, "tpm": 0}).copy()}
        markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🛠 Выполненные действия", callback_data=f"show_acts_{msg_id}"))

    formatted_text = f"<b>{model_name}:</b>\n\n{markdown.md_to_html(text)}"
    chunks = markdown.split_text_safely(formatted_text)
    
    for i, chunk in enumerate(chunks):
        current_markup = markup if i == len(chunks) - 1 else None
        await bot.send_message(chat_id, chunk, parse_mode='HTML', reply_markup=current_markup)
        
    if VOICE_MODE.get(chat_id):
        await generate_and_send_voice(chat_id, text)

# ─────────────────────────────────────────────
#  ГОЛОС И ФАЙЛЫ (ВОЗВРАЩЕНО)
# ─────────────────────────────────────────────
def clean_text_for_voice(text: str) -> str:
    if not text: return ""
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]*>', '', text)
    text = re.sub(r'[*#_~()\[\]{}<>@|\\/]', '', text)
    return text.strip()

async def generate_and_send_voice(chat_id, text):
    clean_text = clean_text_for_voice(text)
    if not clean_text: return
    mp3_path = f"temp_tts_{chat_id}.mp3"
    ogg_path = f"temp_tts_{chat_id}.ogg"
    
    await set_status(chat_id, "🎙 <i>Синтезирую голос...</i>")
    try:
        communicate = edge_tts.Communicate(clean_text, "ru-RU-SvetlanaNeural")
        await communicate.save(mp3_path)
        if os.path.exists(mp3_path):
            await asyncio.to_thread(subprocess.run, f"ffmpeg -i {mp3_path} -c:a libopus -b:a 32k -v quiet -y {ogg_path}", shell=True, timeout=60)
            with open(ogg_path, 'rb') as vf:
                await bot.send_voice(chat_id, vf)
    except Exception as e: print(f"Voice error: {e}")
    finally:
        await clear_status(chat_id)
        if os.path.exists(mp3_path): os.remove(mp3_path)
        if os.path.exists(ogg_path): os.remove(ogg_path)

# ─────────────────────────────────────────────
#  ИНТЕГРАЦИЯ С AIManager CALLBACKS
# ─────────────────────────────────────────────
async def check_abort_cb(chat_id):
    return ABORT_FLAGS.get(chat_id, False)

async def action_log_cb(chat_id, tool_name, tool_args):
    ACTION_LOGS.setdefault(chat_id, []).append((tool_name, tool_args))

def stat_tracker_cb(chat_id, tpm):
    if chat_id not in TURN_STATS: TURN_STATS[chat_id] = {"rpd": 0, "tpm": 0}
    TURN_STATS[chat_id]["rpd"] += 1
    TURN_STATS[chat_id]["tpm"] += tpm

async def semi_auto_cb(chat_id, tool_name, tool_args):
    """Ставит бота на паузу и ждет решения пользователя"""
    if MODEL_MODE.get(chat_id) != "semi":
        return True
    
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("✅ Выполнить", callback_data=f"act_yes_{chat_id}"), InlineKeyboardButton("❌ Отмена", callback_data=f"act_no_{chat_id}"))
    msg = await bot.send_message(chat_id, f"🤖 <b>Запрос действия:</b>\n\n{tool_name}:\n<code>{html.escape(str(tool_args))}</code>", parse_mode='HTML', reply_markup=markup)
    
    PENDING_EVENTS[chat_id] = asyncio.Event()
    await PENDING_EVENTS[chat_id].wait()
    
    try: await bot.delete_message(chat_id, msg.message_id)
    except: pass
    return PENDING_APPROVAL.get(chat_id, False)

# ─────────────────────────────────────────────
#  TASKS SCHEDULER (МОСТ)
# ─────────────────────────────────────────────
def execute_scheduled_task(chat_id, prompt, model_name, task_id):
    async def _async_bridge():
        if chat_id not in ai_manager.active_chats:
            ai_manager.init_chat(chat_id, model_name, role="admin")
        task_prompt = f"[SYSTEM: SCHEDULED TASK {task_id}]\n{prompt}"
        try:
            resp = await ai_manager.process_message(chat_id, task_prompt)
            await send_response(chat_id, resp, model_name=f"⏰ Задача {task_id}")
        except Exception as e:
            await bot.send_message(chat_id, f"❌ Ошибка задачи {task_id}: {e}")
            
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running(): asyncio.run_coroutine_threadsafe(_async_bridge(), loop)
        else: loop.run_until_complete(_async_bridge())
    except: asyncio.run(_async_bridge())

# ─────────────────────────────────────────────
#  ОБРАБОТЧИКИ КОМАНД (ПОЛНЫЙ ВОЗВРАТ)
# ─────────────────────────────────────────────
@bot.message_handler(commands=['start', 'help'])
async def send_help(message):
    if message.from_user.id not in ADMIN_IDS: return
    help_text = (
        "🤖 <b>MCP AI-Админ (v2 Full)</b>\n\n"
        "🔸 /gemini — Выбрать модель\n"
        "🔸 /changekey — Сменить API ключ\n"
        "🔸 /status — Лимиты API\n"
        "🔸 /voice — Голосовой режим\n"
        "🔸 /clear — Очистить память\n"
        "🔸 /reload — Перезагрузить конфиги\n"
        "🔸 /search — Поиск в базах/архивах\n"
        "🔸 /task — Создать задачу\n"
        "🔸 /tasks — Список задач\n"
        "🔸 /deltask — Удалить задачу\n"
        "💡 <code>!cmd</code> — bash напрямую"
    )
    await bot.reply_to(message, help_text, parse_mode='HTML')

@bot.message_handler(commands=['gemini'])
async def change_model(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    for mod in ai_manager.priority_models:
        markup.add(InlineKeyboardButton(text=mod, callback_data=f"mod_{mod}"))
    await bot.reply_to(message, "Выберите модель:", reply_markup=markup)

@bot.message_handler(commands=['changekey'])
async def change_key_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    for i in [1, 2, 3]:
        active = " (Активен)" if ai_manager.current_key_num == i else ""
        markup.add(InlineKeyboardButton(text=f"🔑 KEY {i}{active}", callback_data=f"key_{i}"))
    await bot.reply_to(message, "Выберите API-ключ:", reply_markup=markup)

@bot.message_handler(commands=['status'])
async def status_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    rpm, tpm, usage = ai_manager.get_limits_stats()
    text = f"📊 <b>Статистика KEY {ai_manager.current_key_num}</b>\n\n⚡ RPM: {rpm}\n⚡ TPM: ~{tpm}\n\n📅 RPD (Сегодня):\n"
    for mod, count in usage.items(): text += f"• <code>{mod}</code>: {count}\n"
    await bot.reply_to(message, text, parse_mode='HTML')

@bot.message_handler(commands=['voice'])
async def voice_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    is_active = VOICE_MODE.get(message.chat.id, False)
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("🟢 Вкл", callback_data="voice_on"), InlineKeyboardButton("🔴 Выкл", callback_data="voice_off"))
    await bot.reply_to(message, f"🎙 Голосовой режим: <b>{'ВКЛ' if is_active else 'ВЫКЛ'}</b>", reply_markup=markup, parse_mode='HTML')

@bot.message_handler(commands=['reload'])
async def reload_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    mods, prompts = ai_manager.reload_configs()
    await bot.reply_to(message, f"✅ Обновлено! Моделей: {mods}, Промпты: {', '.join(prompts)}")

@bot.message_handler(commands=['clear'])
async def clear_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    chat_id = message.chat.id
    if chat_id in ai_manager.chat_models:
        ai_manager.init_chat(chat_id, ai_manager.chat_models[chat_id], ai_manager.chat_roles.get(chat_id, "admin"))
        await bot.reply_to(message, "🧹 Память очищена!")
    else: await bot.reply_to(message, "⚠️ Выберите модель.")

@bot.message_handler(commands=['tasks'])
async def list_tasks(message):
    if message.from_user.id not in ADMIN_IDS: return
    tasks = task.get_all_tasks(message.chat.id)
    if not tasks: await bot.reply_to(message, "📂 Нет задач."); return
    res = "📋 <b>Активные задачи:</b>\n\n"
    for t in tasks: res += f"ID: <code>{t['id']}</code> | Cron: <code>{t['cron']}</code>\nPrompt: <i>{t['prompt'][:50]}...</i>\n\n"
    await bot.reply_to(message, res, parse_mode='HTML')

@bot.message_handler(commands=['deltask'])
async def deltask_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split()
    if len(parts) < 2: await bot.reply_to(message, "Формат: /deltask ID"); return
    if task.delete_task(message.chat.id, parts[1]): await bot.reply_to(message, f"✅ Задача {parts[1]} удалена.")
    else: await bot.reply_to(message, "❌ Не найдена.")

@bot.message_handler(commands=['task'])
async def task_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    user_text = message.text.replace("/task", "").strip()
    if not user_text: await bot.reply_to(message, "Пример: /task каждый день в 20:00 проверь логи"); return
    
    msg_wait = await bot.send_message(message.chat.id, "🧠 <i>Генерирую CRON...</i>", parse_mode='HTML')
    try:
        now_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S PT")
        parser = genai.GenerativeModel("gemini-2.5-flash")
        prompt = f"Convert to strict JSON {{'cron': '* * * * *', 'prompt': 'English instruction'}}. Server time: {now_str}. Request: {user_text}"
        res = await asyncio.to_thread(parser.generate_content, prompt)
        parsed = json.loads(res.text.strip().lstrip("```json").rstrip("```").strip())
        
        mod = ai_manager.chat_models.get(message.chat.id, "models/gemini-2.5-flash")
        tid = task.add_task(message.chat.id, parsed["cron"], parsed["prompt"], mod, user_text)
        await bot.edit_message_text(f"✅ Создано! ID: {tid}\nCron: {parsed['cron']}\nPrompt: {parsed['prompt']}", message.chat.id, msg_wait.message_id)
    except Exception as e: await bot.edit_message_text(f"❌ Ошибка генерации: {e}", message.chat.id, msg_wait.message_id)

@bot.message_handler(commands=['search'])
async def search_menu(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("📂 Базы (.csv)", callback_data="st_reg"), InlineKeyboardButton("🗄 Архивы (.zip)", callback_data="st_arch"))
    await bot.reply_to(message, "Режим безопасного поиска:", reply_markup=markup)

# ─────────────────────────────────────────────
#  CALLBACKS И ФАЙЛЫ
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
async def handle_callbacks(call):
    chat_id = call.message.chat.id
    data = call.data

    if data.startswith("abort_"):
        ABORT_FLAGS[chat_id] = True
        await bot.answer_callback_query(call.id, "Остановка...")
        return
        
    if data.startswith("act_"):
        if chat_id in PENDING_EVENTS:
            PENDING_APPROVAL[chat_id] = data.startswith("act_yes_")
            PENDING_EVENTS[chat_id].set()
        return

    if data.startswith("key_"):
        k = int(data.split("_")[1])
        if ai_manager.set_active_key(k): await bot.send_message(chat_id, f"✅ Активен KEY {k}.")
        else: await bot.answer_callback_query(call.id, "Ключ не задан!", show_alert=True)
        return

    if data == "voice_on" or data == "voice_off":
        VOICE_MODE[chat_id] = (data == "voice_on")
        await bot.edit_message_text(f"Голос {'ВКЛЮЧЕН' if VOICE_MODE[chat_id] else 'ВЫКЛЮЧЕН'}", chat_id, call.message.message_id)
        return

    if data.startswith("mod_"):
        mod = data.replace("mod_", "")
        ai_manager.init_chat(chat_id, f"models/{mod}", role="admin")
        markup = InlineKeyboardMarkup().row(InlineKeyboardButton("⚡ Авто", callback_data="mode_auto"), InlineKeyboardButton("🛑 Полуавтомат", callback_data="mode_semi"))
        await bot.edit_message_text(f"Модель: <b>{mod}</b>\nВыберите режим:", chat_id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
        return

    if data.startswith("mode_"):
        MODEL_MODE[chat_id] = data.replace("mode_", "")
        m_txt = "АВТО" if MODEL_MODE[chat_id] == "auto" else "ПОЛУАВТОМАТ"
        await bot.edit_message_text(f"✅ Режим: <b>{m_txt}</b>", chat_id, call.message.message_id, parse_mode='HTML')
        return

    if data.startswith("st_"):
        stype = "archive" if "arch" in data else "regular"
        msg = await bot.send_message(chat_id, "🔍 Введите запрос:")
        bot.register_next_step_handler(msg, process_manual_search, search_type=stype)
        return

    if data == "dl_search":
        txt = PENDING_SEARCH_RESULTS.get(chat_id)
        if txt:
            with tempfile.NamedTemporaryFile(mode='w', suffix=".txt", delete=False, encoding='utf-8') as tf:
                tf.write(txt)
                fname = tf.name
            with open(fname, "rb") as f: await bot.send_document(chat_id, f)
            os.remove(fname)
        return

    if data.startswith("show_acts_"):
        msg_id = int(data.split("_")[2])
        acts = LAST_ACTIONS.get(msg_id)
        if acts:
            res = "🛠 <b>Лог действий:</b>\n"
            for t, a in acts["actions"]: res += f"• {t}: <code>{a}</code>\n"
            res += f"\n📊 RPM: {acts['stats']['rpd']} | TPM: {acts['stats']['tpm']}"
            await bot.send_message(chat_id, res, parse_mode='HTML')
        return

    if data in ["file_yes", "file_no", "file_ai"]:
        f_info = PENDING_FILES.get(chat_id)
        if not f_info: return
        if data == "file_no": await bot.edit_message_text("❌ Отменено", chat_id, call.message.message_id); return
        
        file_obj = await bot.get_file(f_info['id'])
        down_file = await bot.download_file(file_obj.file_path)
        
        if data == "file_yes":
            sp = os.path.join("/app/downloads", f_info['name'])
            with open(sp, 'wb') as f: f.write(down_file)
            await bot.edit_message_text(f"✅ Сохранен: {sp}", chat_id, call.message.message_id)
            
        elif data == "file_ai":
            tp = f"temp_ai_{f_info['name']}"
            with open(tp, 'wb') as f: f.write(down_file)
            await set_status(chat_id, "🧠 <i>Читаю файл...</i>", show_abort=True)
            try:
                gem_file = await asyncio.to_thread(genai.upload_file, path=tp)
                os.remove(tp)
                await process_user_input(chat_id, [gem_file, "Проанализируй этот файл."], call.message.message_id)
            except Exception as e: await bot.edit_message_text(f"❌ Ошибка ИИ: {e}", chat_id, call.message.message_id)
        PENDING_FILES.pop(chat_id, None)

@bot.message_handler(content_types=['document'])
async def handle_document(message):
    if message.from_user.id not in ADMIN_IDS: return
    PENDING_FILES[message.chat.id] = {'id': message.document.file_id, 'name': message.document.file_name}
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Сохранить", callback_data="file_yes"), InlineKeyboardButton("❌ Отмена", callback_data="file_no")).row(InlineKeyboardButton("🧠 В ИИ", callback_data="file_ai"))
    await bot.reply_to(message, f"Файл: {message.document.file_name}", reply_markup=markup)

async def process_manual_search(message, search_type):
    if not message.text: return
    words = search.parse_search_query(message.text)
    msg_wait = await bot.send_message(message.chat.id, "⌛ <i>Поиск...</i>", parse_mode='HTML')
    out = await asyncio.to_thread(search.run_archive_search if search_type == "archive" else search.run_grep_search, words)
    if not out: await bot.edit_message_text("🤷‍♂️ Пусто.", message.chat.id, msg_wait.message_id); return
    fmt, full = search.format_search_results(out, words)
    await bot.delete_message(message.chat.id, msg_wait.message_id)
    for c in fmt[:3]: await bot.send_message(message.chat.id, f"<pre>{c}</pre>", parse_mode='HTML')
    if len(fmt) > 3:
        PENDING_SEARCH_RESULTS[message.chat.id] = full
        await bot.send_message(message.chat.id, "Есть еще.", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("📥 Скачать .txt", callback_data="dl_search")))

# ─────────────────────────────────────────────
#  ОСНОВНОЙ ВВОД В ИИ
# ─────────────────────────────────────────────
async def process_user_input(chat_id, prompt_parts, status_msg_id):
    ABORT_FLAGS[chat_id] = False
    ACTION_LOGS[chat_id] = []
    TURN_STATS[chat_id] = {"rpd": 0, "tpm": 0}

    async def _status_cb(txt): await set_status(chat_id, txt, show_abort=True)

    try:
        resp = await ai_manager.process_message(
            chat_id=chat_id,
            prompt_parts=prompt_parts,
            update_status_cb=_status_cb,
            check_abort_cb=check_abort_cb,
            semi_auto_cb=semi_auto_cb,
            action_log_cb=action_log_cb,
            stat_cb=stat_tracker_cb
        )
        await clear_status(chat_id)
        if resp.startswith("__MCP_SEND_FILE_TRIGGER__"):
            with open(resp.split(":", 1)[1], 'rb') as f: await bot.send_document(chat_id, f)
            return
            
        m_name = ai_manager.chat_models[chat_id].replace("models/", "")
        await send_response(chat_id, resp, model_name=m_name, msg_id=STATUS_MSG.get(chat_id, message_id_generator()))
    except Exception as e:
        await set_status(chat_id, f"❌ Ошибка:\n{e}")

def message_id_generator(): return int(time.time())

@bot.message_handler(content_types=['text', 'photo', 'voice'])
async def handle_message(message):
    if message.from_user.id not in ADMIN_IDS: return
    chat_id = message.chat.id
    text = (message.text or message.caption or "").strip()

    if text.startswith('!'):
        await bot.send_message(chat_id, f"<pre>{await asyncio.to_thread(subprocess.run, text[1:], shell=True, capture_output=True, text=True).stdout}</pre>", parse_mode='HTML')
        return

    if chat_id not in ai_manager.active_chats:
        await bot.reply_to(message, "⚠️ /gemini")
        return

    await set_status(chat_id, "🤖 <b>Принято...</b>", show_abort=True)
    msg_id = STATUS_MSG[chat_id]

    if message.content_type == 'voice':
        vf = await bot.get_file(message.voice.file_id)
        tp = f"temp_v_{msg_id}.ogg"
        with open(tp, 'wb') as f: f.write(await bot.download_file(vf.file_path))
        gem_v = await asyncio.to_thread(genai.upload_file, path=tp, mime_type="audio/ogg")
        os.remove(tp)
        await process_user_input(chat_id, [gem_v, text or "Слушай аудио."], msg_id)
    elif message.content_type == 'photo':
        pf = await bot.get_file(message.photo[-1].file_id)
        tp = f"temp_p_{msg_id}.jpg"
        with open(tp, 'wb') as f: f.write(await bot.download_file(pf.file_path))
        gem_p = await asyncio.to_thread(genai.upload_file, path=tp)
        os.remove(tp)
        await process_user_input(chat_id, [gem_p, text or "Опиши фото."], msg_id)
    else:
        await process_user_input(chat_id, text, msg_id)

async def main():
    await ai_manager.connect_mcp()
    task.init_scheduler(execute_scheduled_task)
    print("Бот запущен. Все функции активны.")
    await bot.polling(non_stop=True)

if __name__ == '__main__':
    asyncio.run(main())
