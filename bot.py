import os
import asyncio
import html
import tempfile
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Импорт наших модулей
from ai_client import AIManager
import search
import task
import markdown # Используем твой парсер для оформления

# Инициализация
TG_TOKEN = os.getenv("TG_TOKEN")
ADMIN_IDS = set()
for env_var in ["ADMIN_ID", "ADMIN2_ID", "ADMIN3_ID"]:
    val = os.getenv(env_var)
    if val and val.strip().lstrip('-').isdigit():
        ADMIN_IDS.add(int(val.strip()))

bot = AsyncTeleBot(TG_TOKEN)
ai_manager = AIManager()

# Хранилище для результатов поиска
PENDING_SEARCH_RESULTS = {}

# ─────────────────────────────────────────────
#  UI И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────

async def update_status(chat_id, msg_id, text):
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode='HTML')
    except Exception as e:
        if "is not modified" not in str(e).lower():
            print(f"Status update error: {e}")

async def send_response(chat_id, text, model_name="AI"):
    """Оформляет и отправляет ответ частями через твой markdown.py"""
    formatted_text = f"<b>{model_name}:</b>\n\n{markdown.md_to_html(text)}"
    chunks = markdown.split_text_safely(formatted_text)
    for chunk in chunks:
        await bot.send_message(chat_id, chunk, parse_mode='HTML')

# ─────────────────────────────────────────────
#  МОСТ-АДАПТЕР ДЛЯ TASK.PY
# ─────────────────────────────────────────────

def execute_scheduled_task(chat_id, prompt, model_name, task_id):
    """
    Этот мост вызывается синхронным APScheduler. 
    Он создает новый цикл событий для выполнения асинхронной логики ИИ.
    """
    async def _async_bridge():
        print(f"[Task] Запуск задачи {task_id}...")
        try:
            # Инициализируем чат для задачи, если его нет
            if chat_id not in ai_manager.active_chats:
                ai_manager.init_chat(chat_id, model_name, role="admin")
            
            # Добавляем системный контекст для задачи
            task_prompt = f"[SYSTEM: SCHEDULED TASK {task_id}]\n{prompt}"
            
            # Выполняем запрос
            response = await ai_manager.process_message(chat_id, task_prompt)
            
            # Отправляем отчет
            header = f"⏰ <b>Фоновая задача:</b> <code>{task_id}</code>\n"
            await send_response(chat_id, response, model_name=f"{header}{model_name}")
            
        except Exception as e:
            await bot.send_message(chat_id, f"❌ Ошибка задачи {task_id}:\n{e}")

    # Запускаем асинхронную задачу в текущем или новом цикле
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(_async_bridge(), loop)
        else:
            loop.run_until_complete(_async_bridge())
    except RuntimeError:
        asyncio.run(_async_bridge())

# ─────────────────────────────────────────────
#  ОБРАБОТЧИКИ КОМАНД (TELEGRAM)
# ─────────────────────────────────────────────

@bot.message_handler(commands=['start', 'help'])
async def send_help(message):
    if message.from_user.id not in ADMIN_IDS: return
    help_text = (
        "🤖 <b>MCP AI-Админ (v2)</b>\n\n"
        "🔸 /gemini — Выбрать модель\n"
        "🔸 /clear — Очистить память\n"
        "🔸 /status — Лимиты API\n"
        "🔸 /search — Конфиденциальный поиск\n"
        "🔸 /tasks — Список задач\n\n"
        "<i>Плагины: Bash, WebSearch, Downloads подключены через MCP.</i>"
    )
    await bot.reply_to(message, help_text, parse_mode='HTML')

@bot.message_handler(commands=['gemini'])
async def change_model(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    for mod in ai_manager.priority_models:
        markup.add(InlineKeyboardButton(text=mod, callback_data=f"mod_{mod}"))
    await bot.reply_to(message, "Выберите модель:", reply_markup=markup)

@bot.message_handler(commands=['search'])
async def search_menu(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("📂 Базы (.csv)", callback_data="st_reg"), 
        InlineKeyboardButton("🗄 Архивы (.zip/.7z)", callback_data="st_arch")
    )
    await bot.reply_to(message, "Режим безопасного поиска:", reply_markup=markup)

@bot.message_handler(commands=['tasks'])
async def list_tasks(message):
    if message.from_user.id not in ADMIN_IDS: return
    tasks = task.get_all_tasks(message.chat.id)
    if not tasks:
        await bot.reply_to(message, "📂 Список задач пуст.")
        return
    res = "📋 <b>Активные задачи:</b>\n\n"
    for t in tasks:
        res += f"ID: <code>{t['id']}</code> | Cron: <code>{t['cron']}</code>\nPrompt: <i>{t['prompt'][:50]}...</i>\n\n"
    await bot.reply_to(message, res, parse_mode='HTML')

# ─────────────────────────────────────────────
#  CALLBACKS
# ─────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data.startswith("mod_"))
async def mod_callback(call):
    model_name = call.data.replace("mod_", "")
    ai_manager.init_chat(call.message.chat.id, f"models/{model_name}", role="admin")
    await bot.answer_callback_query(call.id, f"Модель {model_name} готова")
    await bot.send_message(call.message.chat.id, f"✅ Активна: <b>{model_name}</b>", parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith("st_"))
async def search_type_callback(call):
    stype = "archive" if "arch" in call.data else "regular"
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    msg = await bot.send_message(call.message.chat.id, "🔍 Введите запрос для поиска:")
    bot.register_next_step_handler(msg, process_manual_search, search_type=stype)

async def process_manual_search(message, search_type):
    if not message.text: return
    words = search.parse_search_query(message.text)
    msg_wait = await bot.send_message(message.chat.id, "⌛ <i>Выполняю поиск в потоке...</i>", parse_mode='HTML')
    
    # Запускаем тяжелый поиск в отдельном потоке, чтобы не вешать бота
    if search_type == "archive":
        output = await asyncio.to_thread(search.run_archive_search, words)
    else:
        output = await asyncio.to_thread(search.run_grep_search, words)
    
    if not output:
        await update_status(message.chat.id, msg_wait.message_id, "🤷‍♂️ Ничего не найдено.")
        return

    formatted, full_txt = search.format_search_results(output, words)
    await bot.delete_message(message.chat.id, msg_wait.message_id)
    
    for chunk in formatted[:3]:
        await bot.send_message(message.chat.id, f"<pre>{chunk}</pre>", parse_mode='HTML')
    
    if len(formatted) > 3:
        PENDING_SEARCH_RESULTS[message.chat.id] = full_txt
        markup = InlineKeyboardMarkup().add(InlineKeyboardButton("📥 Скачать .txt", callback_data="dl_search"))
        await bot.send_message(message.chat.id, "Показана часть результатов.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "dl_search")
async def dl_search_callback(call):
    full_text = PENDING_SEARCH_RESULTS.get(call.message.chat.id)
    if not full_text: return
    with tempfile.NamedTemporaryFile(mode='w', suffix=".txt", delete=False, encoding='utf-8') as tf:
        tf.write(full_text)
        fname = tf.name
    with open(fname, "rb") as f:
        await bot.send_document(call.message.chat.id, f, caption="Результаты поиска")
    os.remove(fname)

# ─────────────────────────────────────────────
#  ГЛАВНЫЙ ЦИКЛ
# ─────────────────────────────────────────────

@bot.message_handler(content_types=['text'])
async def handle_message(message):
    if message.from_user.id not in ADMIN_IDS: return
    chat_id = message.chat.id
    
    if chat_id not in ai_manager.active_chats:
        await bot.reply_to(message, "⚠️ Выберите модель: /gemini")
        return

    status_msg = await bot.send_message(chat_id, "🤖 <b>Думаю...</b>", parse_mode='HTML')
    
    async def status_updater(text):
        await update_status(chat_id, status_msg.message_id, text)

    try:
        response = await ai_manager.process_message(chat_id, message.text, update_status_cb=status_updater)
        
        # Триггер на отправку файла
        if response.startswith("__MCP_SEND_FILE_TRIGGER__"):
            fpath = response.split(":", 1)[1]
            await bot.delete_message(chat_id, status_msg.message_id)
            with open(fpath, 'rb') as f:
                await bot.send_document(chat_id, f)
            return

        await bot.delete_message(chat_id, status_msg.message_id)
        model_display = ai_manager.chat_models[chat_id].replace("models/", "")
        await send_response(chat_id, response, model_name=model_display)
        
    except Exception as e:
        await update_status(chat_id, status_msg.message_id, f"❌ Ошибка:\n{e}")

async def main():
    # Подключаем MCP и планировщик
    await ai_manager.connect_mcp()
    task.init_scheduler(execute_scheduled_task)
    
    print("Бот запущен...")
    await bot.polling(non_stop=True)

if __name__ == '__main__':
    asyncio.run(main())
