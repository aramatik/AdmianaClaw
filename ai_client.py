import os
import json
import time
import asyncio
import re
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import AsyncExitStack

import google.generativeai as genai
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

LIMITS_STATE_FILE = "/app/downloads/temp/api_limits.json"
PROMPTS_FILE = "prompts.txt"
MODELS_FILE = "models.txt"

class AIManager:
    def __init__(self):
        # Ключи и инициализация
        self.api_keys = {
            1: os.getenv("GEMINI_API_KEY"),
            2: os.getenv("GEMINI2_API_KEY"),
            3: os.getenv("GEMINI3_API_KEY")
        }
        self.current_key_num = 1
        if self.api_keys[self.current_key_num]:
            genai.configure(api_key=self.api_keys[self.current_key_num])

        # Лимиты и конфигурации
        self.model_rpm_limits = {}
        self.model_restricted_keys = {}
        self.model_tpm_limits = {}
        self.model_rpd_limits = {}
        self.priority_models = []
        self.prompts = {}
        
        # Хранилище состояний
        self.api_rpd_history = {1: {"date": "", "usage": {}}, 2: {"date": "", "usage": {}}, 3: {"date": "", "usage": {}}}
        self.api_request_history = {1: deque(), 2: deque(), 3: deque()}
        self.api_token_history = {1: deque(), 2: deque(), 3: deque()}
        
        # Активные сессии
        self.active_chats = {}       # chat_id -> genai.ChatSession
        self.chat_models = {}        # chat_id -> model_name
        self.chat_roles = {}         # chat_id -> role
        
        # MCP Клиент
        self.mcp_session = None
        self.mcp_exit_stack = AsyncExitStack()
        self.gemini_tools = []
        
        self._load_configs()
        self._load_limits_state()

    async def connect_mcp(self):
        """Поднимает MCP сервер как подпроцесс и забирает список доступных инструментов"""
        server_script = os.path.join(os.path.dirname(__file__), "mcp_server.py")
        server_params = StdioServerParameters(
            command="python",
            args=[server_script]
        )
        
        stdio_transport = await self.mcp_exit_stack.enter_async_context(stdio_client(server_params))
        read, write = stdio_transport
        
        self.mcp_session = await self.mcp_exit_stack.enter_async_context(ClientSession(read, write))
        await self.mcp_session.initialize()
        
        # Запрашиваем инструменты у MCP сервера
        mcp_tools_response = await self.mcp_session.list_tools()
        gemini_funcs = []
        
        for tool in mcp_tools_response.tools:
            gemini_funcs.append({
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema
            })
            
        self.gemini_tools = [{"function_declarations": gemini_funcs}]
        print(f"[MCP] Успешно подключено. Загружено инструментов: {len(gemini_funcs)}")

    async def disconnect_mcp(self):
        """Корректное завершение работы с MCP"""
        await self.mcp_exit_stack.aclose()

    # ---------------------------------------------------------
    # ЛОГИКА ЛИМИТОВ И КОНФИГОВ (Перенесено из bot.py)
    # ---------------------------------------------------------
    def _load_configs(self):
        self.prompts.clear()
        if os.path.exists(PROMPTS_FILE):
            with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
                current_key = None
                current_text = []
                for line in f:
                    match = re.match(r'^\[(.*?)\]$', line.strip())
                    if match:
                        if current_key: self.prompts[current_key] = "\n".join(current_text).strip()
                        current_key = match.group(1)
                        current_text = []
                    else: current_text.append(line.rstrip('\n'))
                if current_key: self.prompts[current_key] = "\n".join(current_text).strip()

        self.priority_models.clear()
        if os.path.exists(MODELS_FILE):
            with open(MODELS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"): continue
                    model_match = re.search(r'^([\w\-\.]+)', line)
                    if model_match:
                        model_name = model_match.group(1)
                        self.priority_models.append(model_name)
                        rpm = re.search(r'RPM:(\d+)', line)
                        tpm = re.search(r'TPM:(\d+)', line)
                        rpd = re.search(r'RPD:(\d+)', line)
                        if rpm: self.model_rpm_limits[model_name] = int(rpm.group(1))
                        if tpm: self.model_tpm_limits[model_name] = int(tpm.group(1))
                        if rpd: self.model_rpd_limits[model_name] = int(rpd.group(1))

    def _load_limits_state(self):
        os.makedirs(os.path.dirname(LIMITS_STATE_FILE), exist_ok=True)
        if os.path.exists(LIMITS_STATE_FILE):
            try:
                with open(LIMITS_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items(): self.api_rpd_history[int(k)] = v
            except: pass
        
        today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        for i in [1, 2, 3]:
            if i not in self.api_rpd_history or self.api_rpd_history[i].get("date") != today_str:
                self.api_rpd_history[i] = {"date": today_str, "usage": {}}

    def _save_limits_state(self):
        os.makedirs(os.path.dirname(LIMITS_STATE_FILE), exist_ok=True)
        try:
            with open(LIMITS_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.api_rpd_history, f, ensure_ascii=False, indent=4)
        except: pass

    def check_api_rate_limit(self, model_name):
        clean_name = model_name.replace('models/', '')
        rpd_limit = self.model_rpd_limits.get(clean_name)
        today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        key_data = self.api_rpd_history[self.current_key_num]
        
        if key_data["date"] != today_str:
            key_data["date"] = today_str
            key_data["usage"] = {}
            
        if rpd_limit and key_data["usage"].get(clean_name, 0) >= rpd_limit:
            self._switch_api_key("Достигнут дневной лимит (RPD).", clean_name)

        now = time.time()
        self.api_request_history[self.current_key_num].append(now)
        key_data["usage"][clean_name] = key_data["usage"].get(clean_name, 0) + 1
        self._save_limits_state()

    def _switch_api_key(self, reason, clean_name):
        keys = [1, 2, 3]
        idx = keys.index(self.current_key_num)
        for i in range(1, 3):
            next_key = keys[(idx + i) % 3]
            target_key = self.api_keys.get(next_key)
            if not target_key: continue
            
            rpd_limit = self.model_rpd_limits.get(clean_name)
            if rpd_limit:
                today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
                key_data = self.api_rpd_history.get(next_key, {"date": today_str, "usage": {}})
                if key_data["date"] == today_str and key_data["usage"].get(clean_name, 0) >= rpd_limit:
                    continue
            
            self.current_key_num = next_key
            genai.configure(api_key=target_key)
            return True
        raise Exception("Все доступные ключи исчерпали дневной лимит (RPD) для этой модели.")

    # ---------------------------------------------------------
    # УПРАВЛЕНИЕ СЕССИЯМИ И ВЫПОЛНЕНИЕ ЗАПРОСОВ
    # ---------------------------------------------------------
    def init_chat(self, chat_id, model_name, role="admin"):
        self.chat_models[chat_id] = model_name
        self.chat_roles[chat_id] = role
        is_gemma = "gemma" in model_name.lower()

        if is_gemma or role == "chat":
            sys_instruction = self.prompts.get("CHAT_BOT", "") if role == "chat" else (self.prompts.get("GEMMA4_ADMIN_REACT", "") + "\nЕсли нужно подождать, напиши <SLEEP>секунды</SLEEP>.")
            model = genai.GenerativeModel(model_name=model_name, system_instruction=sys_instruction)
            self.active_chats[chat_id] = model.start_chat()
        else:
            sys_prompt = self.prompts.get("GEMINI_ADMIN", "") + "\n\n[IMPORTANT] Return EXACTLY ONE tool call per response."
            # Передаем схему инструментов от MCP
            model = genai.GenerativeModel(
                model_name=model_name,
                tools=self.gemini_tools,
                system_instruction=sys_prompt
            )
            self.active_chats[chat_id] = model.start_chat(enable_automatic_function_calling=False)

    async def execute_mcp_tool(self, tool_name: str, args: dict, chat_id: int) -> str:
        """Проксирует вызов инструмента в MCP сервер"""
        # Инжектим chat_id, если он требуется для задач
        if tool_name in ["delete_scheduled_task_tool", "list_my_tasks_tool"]:
            args["chat_id"] = chat_id
            
        try:
            mcp_result = await self.mcp_session.call_tool(tool_name, arguments=args)
            return mcp_result.content[0].text if mcp_result.content else "Success (No output)"
        except Exception as e:
            return f"Error executing tool {tool_name}: {str(e)}"

    async def process_message(self, chat_id: int, user_text: str, update_status_cb=None):
        """Основной цикл обработки сообщений и инструментов"""
        if chat_id not in self.active_chats:
            raise Exception("Модель не выбрана. Используйте /gemini")

        chat = self.active_chats[chat_id]
        model_name = self.chat_models[chat_id]
        is_gemma = "gemma" in model_name.lower()
        
        self.check_api_rate_limit(model_name)
        
        # Отправляем сообщение асинхронно через executor, так как SDK Gemini синхронный
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, chat.send_message, user_text)

        # -----------------------------------------------------
        # ОБРАБОТКА GEMMA (Regex парсинг)
        # -----------------------------------------------------
        if is_gemma:
            response_text = response.text or ""
            action = None
            
            bash_match = re.search(r'<BASH>(.*?)</BASH>', response_text, re.DOTALL | re.IGNORECASE)
            search_match = re.search(r'<SEARCH>(.*?)</SEARCH>', response_text, re.DOTALL | re.IGNORECASE)
            sleep_match = re.search(r'<SLEEP>(.*?)</SLEEP>', response_text, re.DOTALL | re.IGNORECASE)
            
            if bash_match: action = ("execute_bash", {"command": bash_match.group(1).strip()})
            elif search_match: action = ("search_web_tool", {"query": search_match.group(1).strip()})
            elif sleep_match: action = ("sleep_tool", {"seconds": int(sleep_match.group(1).strip() or 5)})
                
            if action:
                if update_status_cb: await update_status_cb(f"⚙️ Выполняю (Gemma): {action[0]}")
                result = await self.execute_mcp_tool(action[0], action[1], chat_id)
                followup_prompt = f"РЕЗУЛЬТАТ ВЫПОЛНЕНИЯ ({action[0]}):\n{result}\nДай финальный ответ или вызови новый тег."
                return await self.process_message(chat_id, followup_prompt, update_status_cb)
            return response_text

        # -----------------------------------------------------
        # ОБРАБОТКА GEMINI (Native Function Calling Loop)
        # -----------------------------------------------------
        while response.function_call:
            fc = response.function_call
            tool_name = fc.name
            
            # Извлекаем аргументы из Protobuf объекта
            tool_args = type(fc).to_dict(fc).get("args", {})
            
            if update_status_cb: 
                await update_status_cb(f"⚙️ Выполняю: <b>{tool_name}</b>")
            
            # 1. Вызываем инструмент через MCP
            result_text = await self.execute_mcp_tool(tool_name, tool_args, chat_id)
            
            # Возвращаем маркер загрузки файла обратно клиенту телеграма (если нужно)
            if result_text.startswith("__MCP_SEND_FILE_TRIGGER__"):
                return result_text 
            
            if update_status_cb: 
                await update_status_cb("🧠 Анализирую результат...")
                
            self.check_api_rate_limit(model_name)
            
            # 2. Возвращаем результат обратно в Gemini
            func_response = {
                "function_response": {
                    "name": tool_name,
                    "response": {"result": result_text}
                }
            }
            response = await loop.run_in_executor(None, chat.send_message, func_response)

        return response.text
      
