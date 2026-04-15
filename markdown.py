import html
import re

def split_text_safely(text, max_len=3500):
    """
    Аккуратно режет текст по переносам строк для обхода 
    лимитов Telegram (максимум 4096 символов на сообщение).
    """
    if len(text) <= max_len:
        return [text]
    
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
            
        # Ищем последний перенос строки в пределах допустимого лимита
        split_idx = text.rfind('\n', 0, max_len)
        
        # Если переносов строк нет (сплошной монолит), ищем пробел
        if split_idx == -1:
            split_idx = text.rfind(' ', 0, max_len)
            
            # Если и пробелов нет (очень длинное слово/ссылка), рубим жестко
            if split_idx == -1:
                split_idx = max_len
                
        chunks.append(text[:split_idx])
        text = text[split_idx:].lstrip()
        
    return chunks

def md_to_html(text):
    """
    Конвертирует базовый Markdown от LLM-моделей (особенно Gemma) 
    в безопасный HTML, который переваривает Telegram.
    """
    if not text: 
        return ""
        
    # Сначала экранируем все опасные символы (<, >, &)
    text = html.escape(text)
    
    # Заголовки: превращаем # Заголовок -> <b>Заголовок</b>
    text = re.sub(r'(?m)^#{1,6}\s+(.*?)$', r'<b>\1</b>', text)
    
    # Блоки кода (многострочные).
    # Используем `{3}` вместо трех обратных кавычек подряд, чтобы не ломать парсер чата
    text = re.sub(r'`{3}(?:.*?)\n(.*?)(?:`{3}|$)', r'<pre>\1</pre>', text, flags=re.DOTALL)
    
    # Инлайн код: `код` -> <code>код</code>
    text = re.sub(r'`(.*?)`', r'<code>\1</code>', text)
    
    # Жирный шрифт: **текст** -> <b>текст</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # Курсив: *текст* -> <i>текст</i> (защита от совпадений со списками)
    text = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    
    # Маркированные списки: заменяем звездочки/тире в начале строки на точки-буллиты
    text = re.sub(r'(?m)^(\s*)[\*\-]\s+', r'\1• ', text)
    
    return text
