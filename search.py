import subprocess
import shlex
import os
import html
import re
import glob
from collections import defaultdict

def parse_search_query(query):
    """
    Умный парсер запросов.
    Извлекает точные фразы из квадратных скобок [...] и отдельные слова.
    Например: "Слово1 [фраза два - 43] слово3" -> ['Слово1', 'фраза два - 43', 'слово3']
    """
    pattern = r'\[(.*?)\]|"([^"]*)"|(\S+)'
    matches = re.findall(pattern, query)
    
    terms = []
    for match in matches:
        term = match[0] or match[1] or match[2]
        if term:
            terms.append(term.strip())
            
    return terms

def run_grep_search(terms, base_path="/app/downloads/база/*.csv"):
    """
    Генерирует и выполняет команду grep по переданным аргументам для обычных файлов.
    """
    if not terms: return ""
        
    cmd = f"grep -iH {shlex.quote(terms[0])} {base_path} 2>/dev/null"
    for word in terms[1:]:
        cmd += f" | grep -i {shlex.quote(word)}"
        
    # Таймаут для обычных баз оставляем 60 секунд
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    return result.stdout.strip()

def run_archive_search(terms, base_path="/app/downloads/база"):
    """
    Ищет совпадения внутри .zip и .7z архивов без их полной распаковки на диск.
    """
    if not terms: return ""
    
    # Собираем все архивы в директории
    archives = glob.glob(os.path.join(base_path, "*.zip")) + glob.glob(os.path.join(base_path, "*.7z"))
    if not archives:
        return ""
        
    all_results = []
    for arch in archives:
        arch_name = os.path.basename(arch)
        # 7z e -so извлекает содержимое в поток (stdout), а grep фильтрует на лету
        cmd = f"7z e -so {shlex.quote(arch)} 2>/dev/null"
        for word in terms:
            cmd += f" | grep -i {shlex.quote(word)}"
            
        try:
            # УВЕЛИЧИЛИ ТАЙМ-АУТ ДО 5 МИНУТ (300 СЕКУНД)
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
            if res.stdout:
                for line in res.stdout.strip().split('\n'):
                    if line:
                        # Форматируем под стандартный вывод grep (имя_файла:совпадение)
                        all_results.append(f"{arch_name}:{line}")
        except subprocess.TimeoutExpired:
            all_results.append(f"{arch_name}:[Таймаут поиска в архиве (превышен лимит 5 минут)]")
        except Exception:
            pass
            
    return "\n".join(all_results)

def format_search_results(output, terms):
    """
    Группирует сырой вывод bash по файлам и разбивает на безопасные чанки для Telegram.
    Возвращает список чанков (до 4000 символов) и полный чистый текст для файла .txt
    """
    grouped_results = defaultdict(list)
    for line in output.split('\n'):
        if not line.strip(): continue
        parts = line.split(':', 1)
        if len(parts) == 2:
            filepath, match_text = parts
            filename = os.path.basename(filepath)
            grouped_results[filename].append(match_text)
        else:
            grouped_results["Другое"].append(line)

    formatted_chunks = []
    current_chunk = ""

    clean_text_for_file = f"Результаты поиска по запросу: {' '.join(terms)}\n"
    clean_text_for_file += "=" * 50 + "\n\n"

    for filename, matches in grouped_results.items():
        header = f"📁 <b>{html.escape(filename)}:</b>\n"
        clean_text_for_file += f"=== {filename} ===\n\n"
        
        if len(current_chunk) + len(header) > 4000:
            formatted_chunks.append(current_chunk)
            current_chunk = header
        else:
            current_chunk += header
            
        for match in matches:
            clean_text_for_file += f"{match}\n\n"
            
            line = f"{html.escape(match)}\n\n" 
            if len(current_chunk) + len(line) > 4000:
                formatted_chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += line
        
        clean_text_for_file += "\n"
        
    if current_chunk.strip():
        formatted_chunks.append(current_chunk)
        
    return formatted_chunks, clean_text_for_file
    
