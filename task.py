import os
import json
import uuid
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

TASKS_DIR = "/app/downloads/tasks"
TASKS_FILE = os.path.join(TASKS_DIR, "tasks.json")
TASKS_LOG_FILE = os.path.join(TASKS_DIR, "tasks.log")

scheduler = BackgroundScheduler()
execute_callback = None

def log_task_event(event_text):
    """Пишет подробные логи в файл tasks.log"""
    os.makedirs(TASKS_DIR, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{now}] {event_text}\n"
    try:
        with open(TASKS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        print(f"Ошибка записи лога: {e}")

def init_scheduler(callback_fn):
    global execute_callback
    execute_callback = callback_fn
    
    os.makedirs(TASKS_DIR, exist_ok=True)
    if not os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
            
    load_tasks()
    scheduler.start()
    print("Планировщик задач успешно запущен.")
    log_task_event("SYSTEM: Планировщик запущен/перезапущен.")

def load_tasks():
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            tasks = json.load(f)
        for t in tasks:
            _schedule_job(t)
    except Exception as e:
        print(f"Ошибка загрузки задач: {e}")

def _schedule_job(task):
    try:
        trigger = CronTrigger.from_crontab(task["cron"])
        scheduler.add_job(
            execute_callback,
            trigger=trigger,
            args=[task["chat_id"], task["prompt"], task["model"], task["id"]],
            id=task["id"],
            replace_existing=True
        )
    except Exception as e:
        print(f"Не удалось запланировать задачу {task['id']}: {e}")

def add_task(chat_id, cron_expr, prompt, model, original_user_request):
    task_id = str(uuid.uuid4())[:8]
    task_data = {
        "id": task_id,
        "chat_id": chat_id,
        "cron": cron_expr,
        "prompt": prompt,
        "model": model
    }
    
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)
        
    tasks.append(task_data)
    
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=4)
        
    _schedule_job(task_data)
    
    # Подробное логирование создания
    log_task_event(f"CREATE: Chat={chat_id} | ID={task_id} | CRON={cron_expr} | Model={model}\n"
                   f"    USER REQUEST: {original_user_request}\n"
                   f"    AI PROMPT: {prompt}")
    return task_id

def get_all_tasks(chat_id):
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    return [t for t in tasks if t["chat_id"] == chat_id]

def delete_task(chat_id, task_id, deleted_by="USER"):
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)
        
    initial_len = len(tasks)
    tasks = [t for t in tasks if not (t["id"] == task_id and t["chat_id"] == chat_id)]
    
    if len(tasks) < initial_len:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=4)
        try:
            scheduler.remove_job(task_id)
        except:
            pass
        log_task_event(f"DELETE: Chat={chat_id} | ID={task_id} | Deleted_by={deleted_by}")
        return True
    return False
        
