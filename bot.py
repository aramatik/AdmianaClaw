import os
import asyncio
import html
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Импортируем наш новый менеджер ИИ, который держит логику Gemini и MCP
from ai_client import AIManager

TG_TOKEN = os.getenv("TG_TOKEN")
ADMIN_IDS = set()
for env_var in ["ADMIN_ID", "ADMIN2_ID", "ADMIN3_ID"]:
    val = os.getenv(env_var)
    if val and val.strip().lstrip('-').isdigit():
        ADMIN_IDS.add(int(val.strip()))

if not ADMIN_IDS:
    print("ВНИМАНИЕ: Не задано ни одного ADMIN_ID! Бот никого не пустит.")

# Инициализируем АСИНХРОННОГО бота и наш AIManager
bot = AsyncTeleBot(TG_TOKEN)
ai_manager = AIManager()

# ─────────────────────────────────────────────
#  UI ФУНКЦИИ
# ─────────────────────────────────────────────

async def update_status(chat_id, msg_id, text):
    """Безопасное обновление статусного сообщения"""
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode='HTML')
    except Exception as e:
        err_str = str(e).lower()
        if "is not modified" not in err_str:
            print(f"Status update error: {e}")

def split_text_safely(text, max_len=4000):
    """Простая разбивка длинных сообщений (можно вернуть твой markdown.py)"""
    return [text[i:i+max_len] for i in range(0, len(text), max_len)]

# ─────────────────────────────────────────────
#  ОБРАБОТЧИКИ КОМАНД
# ─────────────────────────────────────────────

@bot.message_handler(commands=['start', 'help'])
async def send_help(message):
    if message.from_user.id not in ADMIN_IDS: return
    help_text = (
        "🤖 <b>MCP AI-Админ (v2)</b>\n\n"
        "🔸 /gemini — Выбрать модель\n"
        "🔸 /clear — Очистить память\n"
        "🔸 /status — Посмотреть загрузку лимитов\n\n"
        "<i>Плагины подключены через Model Context Protocol.</i>"
    )
    await bot.reply_to(message, help_text, parse_mode='HTML')

@bot.message_handler(commands=['gemini'])
async def change_model(message):
    if message.from_user.id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    # Берем модели прямо из кеша ai_manager
    for mod in ai_manager.priority_models:
        markup.add(InlineKeyboardButton(text=mod, callback_data=f"mod_{mod}"))
    await bot.reply_to(message, "Выберите модель для работы:", reply_markup=markup)

@bot.message_handler(commands=['clear'])
async def clear_cmd(message):
    if message.from_user.id not in ADMIN_IDS: return
    chat_id = message.chat.id
    if chat_id in ai_manager.chat_models:
        model = ai_manager.chat_models[chat_id]
        role = ai_manager.chat_roles.get(chat_id, "admin")
        ai_manager.init_chat(chat_id, model, role=role)
        await bot.reply_to(message, "🧹 Контекст и память ИИ успешно очищены!")
    else:
        await bot.reply_to(message, "⚠️ Модель еще не выбрана. Память пуста.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("mod_"))
async def handle_model_select(call):
    if call.from_user.id not in ADMIN_IDS: return
    
    model_name = call.data.replace("mod_", "")
    # Назначаем модель и роль (по умолчанию admin с доступом к тулзам)
    ai_manager.init_chat(call.message.chat.id, f"models/{model_name}", role="admin")
    
    try:
        await bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass
    
    await bot.send_message(call.message.chat.id, f"✅ Выбрана модель: <b>{model_name}</b>\nТеперь вы можете отправлять запросы.", parse_mode='HTML')

# ─────────────────────────────────────────────
#  ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ─────────────────────────────────────────────

@bot.message_handler(content_types=['text'])
async def handle_text(message):
    if message.from_user.id not in ADMIN_IDS: return
    
    chat_id = message.chat.id
    if chat_id not in ai_manager.active_chats:
        await bot.reply_to(message, "⚠️ Сначала выберите модель: /gemini")
        return

    # Заглушка для UI-статуса
    status_msg = await bot.send_message(chat_id, "🤖 <b>Обрабатываю запрос...</b>", parse_mode='HTML')
    
    # Callback для передачи в AIManager, чтобы он мог обновлять статус изнутри
    async def status_updater(text):
        await update_status(chat_id, status_msg.message_id, text)

    try:
        # Вся магия вызовов, парсинга и MCP-инструментов теперь скрыта здесь
        response_text = await ai_manager.process_message(chat_id, message.text, update_status_cb=status_updater)
        
        # Перехват системного триггера на отправку локального файла в Telegram
        if response_text.startswith("__MCP_SEND_FILE_TRIGGER__:"):
            filepath = response_text.split(":", 1)[1]
            await bot.delete_message(chat_id, status_msg.message_id)
            with open(filepath, 'rb') as doc:
                await bot.send_document(chat_id, doc)
            return
            
        # Удаляем временное сообщение со статусом
        await bot.delete_message(chat_id, status_msg.message_id)
        
        # Отправляем финальный длинный текст
        chunks = split_text_safely(response_text)
        for chunk in chunks:
            await bot.send_message(chat_id, chunk, parse_mode='HTML')
            
    except Exception as e:
        await update_status(chat_id, status_msg.message_id, f"❌ Ошибка ИИ:\n{html.escape(str(e))}")

# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────

async def main():
    print("Инициализация MCP сервера...")
    await ai_manager.connect_mcp()
    
    print("Запуск Telegram бота...")
    # non_stop=True в AsyncTeleBot заменен на request_timeout и т.д.
    await bot.polling(allowed_updates=["message", "callback_query"])

if __name__ == '__main__':
    # Корректный запуск асинхронного цикла
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
  
