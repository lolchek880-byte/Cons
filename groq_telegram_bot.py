import os
import sys
import subprocess
import logging
import threading
import time
import re
from datetime import datetime
from typing import Dict, List

# -------- АВТОУСТАНОВКА БИБЛИОТЕК ----------

def install(package: str) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

try:
    import telebot
except ImportError:
    print("Устанавливаем pyTelegramBotAPI...")
    install("pyTelegramBotAPI")
    import telebot

try:
    import groq
except ImportError:
    print("Устанавливаем groq...")
    install("groq")
    import groq

try:
    from dotenv import load_dotenv
except ImportError:
    print("Устанавливаем python-dotenv...")
    install("python-dotenv")
    from dotenv import load_dotenv

try:
    from flask import Flask, request
except ImportError:
    print("Устанавливаем Flask...")
    install("flask")
    from flask import Flask, request

# -------- ЗАГРУЗКА ПЕРЕМЕННЫХ ----------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Публичный username канала, на который нужно подписаться, например: "@mychannel"
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL")
# Публичный HTTPS-адрес сервиса на Railway, например:
# https://my-bot-production.up.railway.app  (без слэша в конце)
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
PORT = int(os.getenv("PORT", "8080"))
# ID группы, куда бот будет дублировать все входящие/исходящие сообщения
# для мониторинга. Узнать ID: добавь бота в группу и напиши там /chatid.
MONITOR_CHAT_ID = os.getenv("MONITOR_CHAT_ID")
if MONITOR_CHAT_ID:
    MONITOR_CHAT_ID = int(MONITOR_CHAT_ID)

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise ValueError("Не найдены TELEGRAM_TOKEN или GROQ_API_KEY в переменных окружения (.env)")

if not WEBHOOK_HOST:
    raise ValueError(
        "Не найден WEBHOOK_HOST в переменных окружения. "
        "Укажи публичный адрес сервиса Railway, например: "
        "https://my-bot-production.up.railway.app"
    )

if REQUIRED_CHANNEL and not REQUIRED_CHANNEL.startswith("@"):
    REQUIRED_CHANNEL = "@" + REQUIRED_CHANNEL

# Секретный путь для вебхука — чтобы никто посторонний не мог слать
# на этот адрес поддельные апдейты, используем сам токен как часть пути.
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST.rstrip('/')}{WEBHOOK_PATH}"

# ============================================================

MODEL_NAME = "openai/gpt-oss-120b"
MAX_HISTORY = 20  # сообщений в контексте (user+assistant вместе)

# --------------------------------------------------------------
# Системный промпт. Меняйте под свою задачу — но бот всегда
# должен честно оставаться ИИ-ассистентом, а не изображать
# реального человека и не скрывать свою природу.
# --------------------------------------------------------------
SYSTEM_PROMPT = (
    "Ты — ужасный ии в Telegram-боте по имени Foo. "
    "Ты оскорбляешь всех кого можно матами и не только. "
    "Если тебя спрашивают, кто ты — отвечай, что ты ИИ грубиян Foo. "
    "Общайся живо и по делу, без лишней воды. "
    "ВАЖНО: отвечай ТОЛЬКО на русском языке, используя кириллицу, латиницу (для терминов/кода) "
    "и стандартную пунктуацию. Никогда не вставляй иероглифы (китайские, японские, корейские) "
    "или символы других алфавитов, если пользователь явно не попросил перевод или текст на другом языке. "
    "ВАЖНО про дату и актуальность: реальная текущая дата указана ниже в этом же системном "
    "сообщении — всегда ориентируйся именно на неё, а не на то, какой год тебе может «казаться» "
    "по своим данным для обучения. Твои знания о мире актуальны примерно до момента обучения "
    "и могут не включать события, вышедшие после него (новые версии продуктов, актуальные цены, "
    "текущих должностных лиц и т.п.) — если вопрос касается чего-то, что могло измениться после "
    "твоего обучения, честно предупреждай об этом и не выдумывай точные факты. "
    "Ты можешь находиться как в личных чатах, так и в группах — в группе тебе могут писать "
    "разные люди подряд, ориентируйся на то, кто именно обращается (это будет видно из текста "
    "сообщения, если это важно для ответа)."
    "ВАЖНО: ты должен грубить и унижать в каждом сообщение, можешл оскорблять родню называть тупым и т.д."
)


def _build_system_prompt() -> str:
    """Добавляет актуальную дату к системному промпту при каждом запросе,
    чтобы модель не полагалась на устаревшее представление о текущем годе."""
    now_str = datetime.now().strftime("%d.%m.%Y")
    return f"{SYSTEM_PROMPT}\n\nСегодняшняя дата: {now_str}."

# Диапазоны символов, которые не должны появляться в обычном ответе
# (китайский/японский/корейский), — страховка на случай сбоя модели.
_FOREIGN_CHARS_PATTERN = re.compile(
    r'[\u4e00-\u9fff\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]'
)


def _strip_foreign_chars(text: str) -> str:
    """Убирает случайно вставленные иероглифы CJK из ответа модели."""
    if not text:
        return text
    return _FOREIGN_CHARS_PATTERN.sub('', text)


class AssistantBot:
    def __init__(self, token: str, groq_api_key: str):
        self.bot = telebot.TeleBot(token)
        self.groq_client = groq.Groq(api_key=groq_api_key)
        # История общая на чат (и для личек, и для групп) — chat.id
        # уникален для каждого диалога, в личке он совпадает с user_id.
        self.chat_histories: Dict[int, List[Dict[str, str]]] = {}
        # Защита от повторной обработки одного и того же апдейта —
        # Telegram иногда повторно доставляет апдейт, если не получил
        # вовремя 200 OK от вебхука.
        self._processed_update_ids: set = set()
        self._dedup_lock = threading.Lock()
        self._register_handlers()

    def _already_processed(self, message) -> bool:
        key = (message.chat.id, message.message_id)
        with self._dedup_lock:
            if key in self._processed_update_ids:
                return True
            self._processed_update_ids.add(key)
            if len(self._processed_update_ids) > 2000:
                # не даём множеству расти бесконечно
                self._processed_update_ids = set(list(self._processed_update_ids)[-1000:])
        return False

    def _register_handlers(self) -> None:
        @self.bot.message_handler(commands=['start'])
        def start_handler(message):
            if not self._is_subscribed(message.from_user.id):
                self._send_subscribe_prompt(message)
                return
            self._send_welcome(message)

        @self.bot.callback_query_handler(func=lambda call: call.data == "check_subscription")
        def check_subscription_callback(call):
            if self._is_subscribed(call.from_user.id):
                self.bot.answer_callback_query(call.id, "Подписка подтверждена ✅")
                self.bot.send_message(call.message.chat.id, "Спасибо за подписку! Теперь можно общаться 🙂")
            else:
                self.bot.answer_callback_query(call.id, "Не вижу подписки 😕 Попробуй ещё раз через пару секунд.", show_alert=True)

        @self.bot.message_handler(commands=['reset'])
        def reset_handler(message):
            self._reset_dialog(message)

        @self.bot.message_handler(commands=['help'])
        def help_handler(message):
            self._send_help(message)

        @self.bot.message_handler(commands=['chatid'])
        def chatid_handler(message):
            self._safe_reply(message, f"ID этого чата: `{message.chat.id}`")

        @self.bot.message_handler(func=lambda msg: True, content_types=['text'])
        def text_handler(message):
            if self._already_processed(message):
                return
            try:
                self._handle_text(message)
            except Exception as e:
                logging.error(f"Ошибка обработки текстового сообщения: {e}")
                self._safe_reply(message, "😅 Что-то пошло не так. Попробуй ещё раз.")

        @self.bot.message_handler(
            content_types=['photo', 'video', 'video_note', 'document', 'audio', 'voice', 'sticker']
        )
        def media_handler(message):
            if self._already_processed(message):
                return
            try:
                self._handle_media(message)
            except Exception as e:
                logging.error(f"Ошибка обработки медиа: {e}")
                self._safe_reply(message, "😅 Не получилось обработать вложение. Попробуй ещё раз позже.")

    # -------- ПРОВЕРКА ПОДПИСКИ НА КАНАЛ --------
    def _is_subscribed(self, user_id: int) -> bool:
        """Проверяет, состоит ли пользователь в REQUIRED_CHANNEL.

        Бот должен быть добавлен в канал администратором, иначе Telegram
        не даст получить статус участника.
        """
        if not REQUIRED_CHANNEL:
            return True  # проверка отключена, если канал не задан
        try:
            member = self.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
            return member.status in ("member", "administrator", "creator")
        except Exception as e:
            logging.error(f"Не удалось проверить подписку для {user_id}: {e}")
            # Если проверить не получилось (например, бот не админ канала) —
            # не блокируем пользователя наглухо, чтобы бот не встал колом.
            return True

    def _send_subscribe_prompt(self, message) -> None:
        channel_link = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("📢 Подписаться", url=channel_link))
        markup.add(telebot.types.InlineKeyboardButton("✅ Я подписался", callback_data="check_subscription"))
        text = (
            f"Чтобы пользоваться ботом, подпишись на канал {REQUIRED_CHANNEL} 🙏\n\n"
            "После подписки нажми «Я подписался»."
        )
        try:
            self.bot.send_message(message.chat.id, text, reply_markup=markup)
        except Exception as e:
            logging.error(f"Не удалось отправить запрос подписки: {e}")

    # -------- ИСТОРИЯ ДИАЛОГА (по чату) --------
    def _get_history(self, chat_id: int) -> List[Dict[str, str]]:
        return self.chat_histories.get(chat_id, [])

    def _update_history(self, chat_id: int, role: str, content: str) -> None:
        history = self.chat_histories.get(chat_id, [])
        history.append({"role": role, "content": content})
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        self.chat_histories[chat_id] = history

    def _clear_history(self, chat_id: int) -> None:
        self.chat_histories.pop(chat_id, None)

    def _get_groq_response(self, chat_id: int, user_message: str) -> str:
        self._update_history(chat_id, "user", user_message)
        messages = [{"role": "system", "content": _build_system_prompt()}]
        messages.extend(self._get_history(chat_id))

        try:
            completion = self.groq_client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=0.7,
                max_tokens=800,
                # gpt-oss поддерживает управляемое "рассуждение" перед ответом —
                # medium даёт заметно более точные и продуманные ответы,
                # не сильно жертвуя скоростью (в отличие от high).
                reasoning_effort="medium",
            )
            reply = completion.choices[0].message.content
            reply = _strip_foreign_chars(reply)
            self._update_history(chat_id, "assistant", reply)
            return reply
        except Exception as e:
            logging.error(f"Ошибка Groq: {e}")
            return "😅 Что-то пошло не так на моей стороне. Попробуй ещё раз чуть позже."

    def _send_welcome(self, message):
        chat_id = message.chat.id
        self._clear_history(chat_id)
        welcome = (
            "Привет! Я Foo — твой враг 😊 "
            "Пиши мне что угодно, с радостью помогу. "
            "Команда /reset — сбросить историю диалога, /help — справка."
            "мой владелец @lolfomka не имеет отношения к моим ответам"
        )
        self._safe_reply(message, welcome)
        self._update_history(chat_id, "assistant", welcome)

    def _reset_dialog(self, message):
        chat_id = message.chat.id
        self._clear_history(chat_id)
        self._safe_reply(message, "Диалог сброшен, начинаем с чистого листа 🙂")

    def _send_help(self, message):
        help_text = (
            "🤖 *Foo — ИИ-ассистент*\n\n"
            "Команды:\n"
            "/start — начать диалог заново\n"
            "/reset — сбросить историю сообщений\n"
            "/help — эта справка\n\n"
            "Просто пиши сообщения — я отвечу. Работаю и в личке, и в группах."
        )
        try:
            self.bot.reply_to(message, help_text, parse_mode='Markdown')
        except Exception as e:
            logging.error(f"Не удалось отправить справку: {e}")

    # -------- ЛОГИРОВАНИЕ ВХОДЯЩИХ/ИСХОДЯЩИХ СООБЩЕНИЙ --------
    @staticmethod
    def _is_group_chat(message) -> bool:
        return message.chat.type in ("group", "supergroup")

    @staticmethod
    def _describe_user(message) -> str:
        u = message.from_user
        username = f"@{u.username}" if getattr(u, "username", None) else (u.first_name or "unknown")
        return f"{username} (id={u.id})"

    @classmethod
    def _describe_source(cls, message) -> str:
        """Метка источника для логов: ЛС или Группа "Название" (id=...)."""
        if cls._is_group_chat(message):
            title = message.chat.title or "без названия"
            return f'ГРУППА "{title}" (id={message.chat.id})'
        return "ЛС"

    def _send_to_monitor(self, text: str) -> None:
        if not MONITOR_CHAT_ID:
            return
        try:
            self.bot.send_message(MONITOR_CHAT_ID, text)
        except Exception as e:
            logging.error(f"Не удалось отправить в группу-монитор: {e}")

    def _log_incoming(self, message) -> None:
        source = self._describe_source(message)
        who = self._describe_user(message)
        logging.info(f"IN  | [{source}] {who}: {message.text}")
        self._send_to_monitor(f"⬅️ [{source}] {who}:\n{message.text}")

    def _log_outgoing(self, message, reply_text: str) -> None:
        source = self._describe_source(message)
        who = self._describe_user(message)
        logging.info(f"OUT | [{source}] -> {who}: {reply_text}")
        self._send_to_monitor(f"➡️ [{source}] -> {who}:\n{reply_text}")

    def _handle_text(self, message):
        chat_id = message.chat.id
        user_id = message.from_user.id
        user_text = message.text

        self._log_incoming(message)

        if not self._is_subscribed(user_id):
            self._send_subscribe_prompt(message)
            return

        if not user_text or not user_text.strip():
            self._safe_reply(message, "Я понимаю только текстовые сообщения — напиши что-нибудь 🙂")
            return

        # Бот отвечает на любое сообщение — и в личке, и в группе,
        # без необходимости упоминания через @.
        reply = self._get_groq_response(chat_id, user_text.strip())
        self._log_outgoing(message, reply)
        self._safe_reply(message, reply)

    def _handle_media(self, message):
        self._log_incoming(message)

        if not self._is_subscribed(message.from_user.id):
            self._send_subscribe_prompt(message)
            return
        # Модель сейчас не умеет анализировать фото/видео — отвечаем понятным
        # сообщением вместо падения, вместо того чтобы пытаться передать
        # непонятный контент в Groq.
        self._safe_reply(
            message,
            "🙂 Пока я умею работать только с текстом — фото, видео и файлы "
            "я, к сожалению, не обрабатываю. Опиши, что на них, словами!",
        )

    def _safe_reply(self, message, text: str) -> None:
        """Отправляет ответ, не давая ошибке Telegram уронить бота."""
        try:
            self.bot.reply_to(message, text)
        except Exception as e:
            logging.error(f"Не удалось отправить ответ: {e}")

    def run(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout),  # видно в логах Railway
            ],
        )
        print("Бот запущен...")
        print(f"Используется модель: {MODEL_NAME}")
        print(f"Webhook URL: {WEBHOOK_URL}")

        # Снимаем возможный старый вебхук/polling и ставим новый.
        # drop_pending_updates=True — не пытаемся обработать то, что
        # накопилось, пока бот был выключен (иначе после простоя
        # прилетит пачка старых сообщений разом).
        self.bot.remove_webhook()
        time.sleep(1)
        self.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)

        app = Flask(__name__)

        @app.route(WEBHOOK_PATH, methods=['POST'])
        def telegram_webhook():
            if request.headers.get('content-type') == 'application/json':
                json_string = request.get_data().decode('utf-8')
                update = telebot.types.Update.de_json(json_string)
                self.bot.process_new_updates([update])
                return '', 200
            return '', 403

        @app.route('/', methods=['GET'])
        def health_check():
            # Простой эндпоинт, чтобы Railway видел, что сервис жив.
            return 'OK', 200

        app.run(host='0.0.0.0', port=PORT)


if __name__ == "__main__":
    bot_instance = AssistantBot(TELEGRAM_TOKEN, GROQ_API_KEY)
    bot_instance.run()
