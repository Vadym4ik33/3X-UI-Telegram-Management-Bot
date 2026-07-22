import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import requests
import psutil
import urllib3
import time
import uuid
import string
import random
import json
import re
import html
import io

# Отключаем предупреждения InsecureRequestWarning из-за verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# 1. КОНСТАНТЫ И КОНФИГУРАЦИЯ
# ==========================================
BOT_TOKEN = ""
ADMIN_CHAT_ID = 123456789  # ЗАМЕНИТЕ НА ВАШ TELEGRAM ID (целое число)

# Настройки 3X-UI Панели
PANEL_BASE_URL = "https://127.0.0.1:52313/subpath" 
PANEL_USERNAME = "admin"  # Логин от панели
PANEL_PASSWORD = "your_password"  # Пароль от панели

# Внешние настройки для ссылок
EXTERNAL_HOST = "your-domain-or-ip.com"
EXTERNAL_PORT = 12345  # Порт для ссылки на подписку
SUB_BASE_URL = f"https://{EXTERNAL_HOST}:{EXTERNAL_PORT}/bE3wmKP63g"  # Базовый URL для подписок

INBOUND_IDS = [1, 2, 3]

# ==========================================
# 2. API-КЛИЕНТ 3X-UI
# ==========================================
class XUIAPI:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False
        self.csrf_token = None

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Origin": f"https://{EXTERNAL_HOST}",
            "Referer": f"https://{EXTERNAL_HOST}/",
        }
        self.session.headers.update(self.headers)
        self.login()

    def fetch_csrf_token(self):
        try:
            res = self.session.get(f"{PANEL_BASE_URL}/", timeout=10)
            match = re.search(r'name="csrf-token"\s+content="([^"]+)"', res.text)
            if match:
                self.csrf_token = match.group(1)
                self.session.headers.update({"X-CSRF-Token": self.csrf_token})
                print("CSRF-токен получен.")
                return True
            print("Не удалось найти CSRF-токен на странице панели.")
            return False
        except Exception as e:
            print(f"Ошибка при получении CSRF-токена: {e}")
            return False

    def login(self):
        if not self.fetch_csrf_token():
            return False

        url = f"{PANEL_BASE_URL}/login"
        payload = {"username": PANEL_USERNAME, "password": PANEL_PASSWORD}
        try:
            res = self.session.post(url, data=payload, timeout=10)
            data = res.json()
            if res.status_code == 200 and data.get('success'):
                print("Успешная авторизация в панели.")
                return True
            print(f"Ошибка авторизации. Статус: {res.status_code}, Тело: {res.text}")
            return False
        except Exception as e:
            print(f"Ошибка при попытке логина: {e}")
            return False

    def request(self, method, endpoint, **kwargs):
        url = f"{PANEL_BASE_URL}{endpoint}"
        try:
            res = self.session.request(method, url, timeout=15, **kwargs)

            # Если вернулся код 401/403 или панель редиректнула на страницу входа
            is_html_login = "login" in res.url or "text/html" in res.headers.get("Content-Type", "")
            if res.status_code in (401, 403) or is_html_login:
                print("Сессия истекла или сброшена, выполняем повторный вход...")
                if self.login():
                    res = self.session.request(method, url, timeout=15, **kwargs)

            return res.json()

        except json.JSONDecodeError:
            # Если пришел HTML вместо JSON (например, форма логина), пробуем повторно авторизоваться
            print("Ошибка парсинга JSON (возможно, слетела сессия). Пробуем re-login...")
            if self.login():
                try:
                    retry_res = self.session.request(method, url, timeout=15, **kwargs)
                    return retry_res.json()
                except Exception as e:
                    print(f"Повторный запрос после авторизации не удался: {e}")
            return None

        except requests.exceptions.RequestException as e:
            print(f"Сетевая ошибка при запросе к {endpoint}: {e}")
            return None

api = XUIAPI()
bot = telebot.TeleBot(BOT_TOKEN)

# ==========================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def is_admin(message):
    return message.chat.id == ADMIN_CHAT_ID

def generate_random_string(length=16):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def get_main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("📊 Статус VPN"), KeyboardButton("👥 Список юзеров"))
    markup.row(KeyboardButton("➕ Добавить юзера"), KeyboardButton("❌ Удалить юзера"))
    markup.row(KeyboardButton("⏳ Продлить подписку"))
    return markup

def format_traffic(up, down):
    """Считает только фактически потраченный трафик и переводит в читаемый вид (MB/GB)."""
    used = (up or 0) + (down or 0)
    
    if used >= 1024 ** 3: return f"{used / (1024 ** 3):.2f} GB"
    if used >= 1024 ** 2: return f"{used / (1024 ** 2):.2f} MB"
    if used >= 1024: return f"{used / 1024:.2f} KB"
    return f"{used} B"

def format_days_left(expiry_time_ms):
    """Считает оставшиеся дни. Возвращает 'Бессрочно', если лимита нет."""
    if not expiry_time_ms or expiry_time_ms == 0:
        return "♾ Бессрочно"
    
    now_ms = int(time.time() * 1000)
    diff_ms = expiry_time_ms - now_ms
    
    if diff_ms <= 0:
        return "❌ Истёк"
        
    days = diff_ms // (86400 * 1000)
    if days == 0:
        hours = diff_ms // (3600 * 1000)
        return f"{hours} ч."
        
    return f"{days} дн."

def collect_all_clients():
    """Получает базу клиентов и подтягивает к ним реальный трафик из инбаундов."""
    # 1. Получаем базу всех клиентов
    clients_res = api.request("GET", "/panel/api/clients/list")
    if not clients_res or not clients_res.get("success"):
        return None
        
    raw_clients = clients_res.get("obj", [])
    
    clients_dict = {}
    for c in raw_clients:
        email = c.get("email")
        if email:
            c["real_up"] = 0
            c["real_down"] = 0
            clients_dict[email] = c

    # 2. Получаем актуальную статистику трафика
    inbounds_res = api.request("GET", "/panel/api/inbounds/list")
    if inbounds_res and inbounds_res.get("success"):
        for inbound in inbounds_res.get("obj", []):
            for stat in inbound.get("clientStats", []):
                email = stat.get("email")
                if email in clients_dict:
                    # ВАЖНО: Мы просто перезаписываем значение (ставим = вместо +=).
                    # Так как общая статистика дублируется в каждом инбаунде, 
                    # нам достаточно просто сохранить её последнее полученное значение.
                    clients_dict[email]["real_up"] = stat.get("up", 0)
                    clients_dict[email]["real_down"] = stat.get("down", 0)

    return list(clients_dict.values())

def build_clients_list_text(clients):
    """Формирует красивый HTML текст со списком пользователей."""
    if not clients:
        return "<b>👥 Список пользователей VPN</b>\n\n<i>Пользователей пока нет.</i>"

    sorted_clients = sorted(clients, key=lambda c: c.get("email", "").lower())
    lines = [f"<b>👥 Список пользователей VPN ({len(sorted_clients)})</b>\n"]

    for idx, client in enumerate(sorted_clients, 1):
        email = client.get("email", "Unknown")
        status = "" if client.get("enable", True) else " 🚫"
        
        # Теперь мы передаем ключи real_up и real_down, которые собрали из инбаундов
        used_traffic = format_traffic(client.get("real_up", 0), client.get("real_down", 0))
        days_left = format_days_left(client.get("expiryTime", 0))
        
        lines.append(
            f"<b>{idx}.</b> <code>{html.escape(email)}</code>{status}\n"
            f"   📊 Использовано: <code>{used_traffic}</code>\n"
            f"   ⏳ Окончание: <code>{days_left}</code>\n"
        )

    return "\n".join(lines)

def send_long_message(chat_id, text, parse_mode="HTML"):
    """Telegram ограничивает сообщение 4096 символами — режем на части."""
    max_len = 4000
    if len(text) <= max_len:
        bot.send_message(chat_id, text, parse_mode=parse_mode)
        return

    parts = text.split("\n\n")
    chunk = ""
    for part in parts:
        candidate = f"{chunk}\n\n{part}".strip() if chunk else part
        if len(candidate) > max_len:
            if chunk:
                bot.send_message(chat_id, chunk, parse_mode=parse_mode)
            chunk = part
        else:
            chunk = candidate
    if chunk:
        bot.send_message(chat_id, chunk, parse_mode=parse_mode)

def find_client_by_email(email):
    """Использует родной метод панели для получения клиента по email."""
    res = api.request("GET", f"/panel/api/clients/get/{email}")
    if res and res.get("success"):
        return res.get("obj")
    return None

# ==========================================
# 4. ОБРАБОТЧИКИ ТЕЛЕГРАМ-БОТА
# ==========================================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    if not is_admin(message): return
    bot.send_message(message.chat.id, "Добро пожаловать в панель управления 3X-UI.\nВыберите действие в меню ниже.", reply_markup=get_main_keyboard())

# --- СТАТУС СЕРВЕРА ---
@bot.message_handler(func=lambda m: m.text == "📊 Статус VPN")
def status_cmd(message):
    if not is_admin(message): return
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        ram_mb = ram.used / (1024 * 1024)
        ram_total = ram.total / (1024 * 1024)

        xray_running = any("xray" in p.name().lower() for p in psutil.process_iter(['name']))

        text = (f"<b>📊 Статус сервера</b>\n\n"
                f"💻 CPU: <code>{cpu}%</code>\n"
                f"🧠 RAM: <code>{ram_mb:.0f} MB / {ram_total:.0f} MB</code> ({ram.percent}%)\n"
                f"🚀 Xray запущен: <code>{'✅ Да' if xray_running else '❌ Нет'}</code>")

        bot.send_message(message.chat.id, text, parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка получения статуса: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

# --- СПИСОК ПОЛЬЗОВАТЕЛЕЙ ---
@bot.message_handler(func=lambda m: m.text == "👥 Список юзеров")
def list_users_cmd(message):
    if not is_admin(message): return

    bot.send_chat_action(message.chat.id, 'typing')
    try:
        clients = collect_all_clients()
        if clients is None:
            bot.send_message(message.chat.id, "❌ Не удалось получить список пользователей из панели.", parse_mode="HTML")
            return

        text = build_clients_list_text(clients)
        send_long_message(message.chat.id, text)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка получения списка: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

# --- ДОБАВЛЕНИЕ КЛИЕНТА ---
@bot.message_handler(func=lambda m: m.text == "➕ Добавить юзера")
def add_user_cmd(message):
    if not is_admin(message): return
    msg = bot.send_message(message.chat.id, "Шаг 1/2: Введите email для нового пользователя (без пробелов):")
    bot.register_next_step_handler(msg, process_add_user_email)

def process_add_user_email(message):
    if not is_admin(message): return
    email = message.text.strip()
    
    if not email:
        bot.send_message(message.chat.id, "❌ Email не может быть пустым. Отмена.")
        return

    msg = bot.send_message(message.chat.id, f"Шаг 2/2: Введите количество дней действия для <code>{html.escape(email)}</code>\n(Например: 30, 90. Или введите 0 для безлимита):", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_add_user_days, email)

def process_add_user_days(message, email):
    if not is_admin(message): return
    try:
        days = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: Вы должны ввести целое число дней цифрами. Создание отменено.")
        return

    expiry_time = int((time.time() + days * 86400) * 1000) if days > 0 else 0

    client_uuid = str(uuid.uuid4())
    sub_id = generate_random_string(16)
    auth_str = generate_random_string(16)
    password_str = generate_random_string(16)

    payload = {
        "client": {
            "id": client_uuid,
            "subId": sub_id,
            "auth": auth_str,
            "email": email,
            "enable": True,
            "expiryTime": expiry_time,
            "flow": "",
            "group": "",
            "limitIp": 0,
            "password": password_str,
            "reset": 0,
            "security": "auto",
            "tgId": 0,
            "totalGB": 0
        },
        "inboundIds": INBOUND_IDS
    }

    bot.send_chat_action(message.chat.id, 'typing')
    try:
        res = api.request("POST", "/panel/api/clients/add", json=payload)
        if res and res.get("success"):
            sub_link = f"{SUB_BASE_URL}/sub/{sub_id}"
            
            links_res = api.request("GET", f"/panel/api/clients/links/{email}")
            links_text = ""
            
            if links_res and links_res.get("success") and links_res.get("obj"):
                for idx, link in enumerate(links_res.get("obj", []), 1):
                    links_text += f"<b>{idx}️⃣ Конфиг:</b>\n<code>{html.escape(str(link))}</code>\n\n"
            else:
                links_text = "⚠️ <i>Не удалось автоматически подтянуть конфигурации из API панели.</i>"

            msg_text = (f"<b>✅ Пользователь <code>{html.escape(email)}</code> успешно добавлен!</b>\n"
                        f"⏱ Срок: <code>{f'{days} дн.' if days > 0 else 'Безлимит'}</code>\n\n"
                        f"<b>🔗 Ссылка на подписку:</b>\n<code>{html.escape(sub_link)}</code>\n\n"
                        f"{links_text}")

            bot.send_message(message.chat.id, msg_text, parse_mode="HTML")
        else:
            err_msg = res.get('msg') if res else 'Нет ответа от API'
            bot.send_message(message.chat.id, f"❌ Ошибка добавления в панели: {html.escape(str(err_msg))}\nПолный ответ: <code>{res}</code>", parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Системная ошибка Python: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

# --- УДАЛЕНИЕ КЛИЕНТА ---
@bot.message_handler(func=lambda m: m.text == "❌ Удалить юзера")
def del_user_cmd(message):
    if not is_admin(message): return
    msg = bot.send_message(message.chat.id, "Введите email для удаления:")
    bot.register_next_step_handler(msg, process_del_user)

def process_del_user(message):
    if not is_admin(message): return
    email = message.text.strip()

    bot.send_chat_action(message.chat.id, 'typing')
    try:
        res = api.request("POST", f"/panel/api/clients/del/{email}")
        if res and res.get("success"):
            bot.send_message(message.chat.id, f"✅ Пользователь <code>{html.escape(email)}</code> успешно удален из панели.", parse_mode="HTML")
        else:
            bot.send_message(message.chat.id, f"❌ Ошибка удаления: {html.escape(str(res.get('msg') if res else 'API недоступен'))}", parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Системная ошибка: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

# --- ПРОДЛЕНИЕ ПОДПИСКИ ---
@bot.message_handler(func=lambda m: m.text == "⏳ Продлить подписку")
def extend_user_cmd(message):
    if not is_admin(message): return
    msg = bot.send_message(message.chat.id, "Шаг 1/2: Введите email пользователя для продления:")
    bot.register_next_step_handler(msg, process_extend_user_email)

def process_extend_user_email(message):
    if not is_admin(message): return
    email = message.text.strip()

    bot.send_chat_action(message.chat.id, 'typing')
    client_data = find_client_by_email(email)

    if not client_data:
        bot.send_message(message.chat.id, f"❌ Клиент <code>{html.escape(email)}</code> не найден.", parse_mode="HTML")
        return

    msg = bot.send_message(message.chat.id, f"Шаг 2/2: На сколько дней продлить подписку для <code>{html.escape(email)}</code>?\n(Введите количество дней цифрами, например: 30 или 90):", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_extend_user_days, email)

def process_extend_user_days(message, email):
    if not is_admin(message): return
    try:
        days = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Ошибка: Нужно ввести целое число дней цифрами. Продление отменено.")
        return

    client_data = find_client_by_email(email)
    if not client_data:
        bot.send_message(message.chat.id, f"❌ Клиент <code>{html.escape(email)}</code> не найден.", parse_mode="HTML")
        return

    # ВАЖНО: Достаем вложенный словарь 'client', чтобы правильно прочитать текущие лимиты
    client_obj = client_data.get('client', client_data)
    
    current_expiry = client_obj.get('expiryTime', 0)
    now_ms = int(time.time() * 1000)

    # Если безлимит (0) или подписка уже истекла, считаем от текущего момента
    if current_expiry == 0 or current_expiry < now_ms:
        new_expiry = now_ms + (days * 86400 * 1000)
    else:
        # Если подписка активна, ДОБАВЛЯЕМ дни к оставшемуся времени!
        new_expiry = current_expiry + (days * 86400 * 1000)

    # Вытаскиваем настоящий UUID, чтобы Go не падал с ошибкой Client.id
    real_uuid = client_obj.get('uuid', '')
    if not real_uuid and isinstance(client_obj.get('id'), str):
        real_uuid = client_obj.get('id')

    # Собираем плоский payload для API панели
    payload = {
        "id": real_uuid,
        "email": email,
        "enable": client_obj.get('enable', True),
        "expiryTime": new_expiry,
        "subId": client_obj.get('subId', ""),
        "auth": client_obj.get('auth', ""),
        "password": client_obj.get('password', ""),
        "flow": client_obj.get('flow', ""),
        "limitIp": client_obj.get('limitIp', 0),
        "totalGB": client_obj.get('totalGB', client_obj.get('total', 0)),
        "reset": client_obj.get('reset', 0),
        "security": client_obj.get('security', "auto"),
        "tgId": client_obj.get('tgId', 0)
    }

    bot.send_chat_action(message.chat.id, 'typing')
    try:
        res = api.request("POST", f"/panel/api/clients/update/{email}", json=payload)
        if res and res.get("success"):
            new_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(new_expiry / 1000))
            bot.send_message(
                message.chat.id, 
                f"✅ Подписка для <code>{html.escape(email)}</code> успешно продлена!\n"
                f"➕ Добавлено дней: {days}\n"
                f"⏳ Новая дата окончания: <code>{new_date}</code>", 
                parse_mode="HTML"
            )
        else:
            err_msg = res.get('msg') if res else 'Нет ответа от API'
            bot.send_message(message.chat.id, f"❌ Ошибка продления в панели: {html.escape(str(err_msg))}\nПолный ответ панели: <code>{res}</code>", parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Системная ошибка: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

# ==========================================
# 5. ЗАПУСК БОТА
# ==========================================
if __name__ == '__main__':
    print("Бот успешно запущен и использует глобальные эндпоинты API...")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Ошибка polling: {e}")
            time.sleep(5)
