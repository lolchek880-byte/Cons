import os
import sys
import subprocess
import logging
import time
import re
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

# -------- ЗАГРУЗКА ПЕРЕМЕННЫХ ----------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Публичный username канала, на который нужно подписаться, например: "@mychannel"
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise ValueError("Не найдены TELEGRAM_TOKEN или GROQ_API_KEY в переменных окружения (.env)")

if REQUIRED_CHANNEL and not REQUIRED_CHANNEL.startswith("@"):
    REQUIRED_CHANNEL = "@" + REQUIRED_CHANNEL

# -------- УДАЛЯЕМ ВЕБХУК ПЕРЕД POLLING ----------
try:
    import requests
    for _ in range(3):
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
        if resp.status_code == 200:
            print("Webhook удалён:", resp.json())
            break
        time.sleep(1)
except Exception as e:
    print("Ошибка удаления вебхука:", e)

# ============================================================

MODEL_NAME = "llama-3.3-70b-versatile"
MAX_HISTORY = 20  # сообщений в контексте (user+assistant вместе)

# --------------------------------------------------------------
# Системный промпт. Меняйте под свою задачу — но бот всегда
# должен честно оставаться ИИ-ассистентом, а не изображать
# реального человека и не скрывать свою природу.
# --------------------------------------------------------------
SYSTEM_PROMPT = (
    "Ты — дружелюбный ИИ-ассистент в Telegram-боте по имени Foo. "
    "Ты помогаешь пользователю: отвечаешь на вопросы, поддерживаешь беседу, "
    "помогаешь разобраться в задачах. "
    "Если тебя спрашивают, кто ты — отвечай, что ты ИИ-ассистент Foo. "
    "Общайся живо и по делу, без лишней воды. "
    "ВАЖНО: отвечай ТОЛЬКО на русском языке, используя кириллицу, латиницу (для терминов/кода) "
    "и стандартную пунктуацию. Никогда не вставляй иероглифы (китайские, японские, корейские) "
    "или символы других алфавитов, если пользователь явно не попросил перевод или текст на другом языке."
)

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
        self.user_histories: Dict[int, List[Dict[str, str]]] = {}
        self._register_handlers()

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

        @self.bot.message_handler(func=lambda msg: True, content_types=['text'])
        def text_handler(message):
            try:
                self._handle_text(message)
            except Exception as e:
                logging.error(f"Ошибка обработки текстового сообщения: {e}")
                self._safe_reply(message, "😅 Что-то пошло не так. Попробуй ещё раз.")

        @self.bot.message_handler(
            content_types=['photo', 'video', 'video_note', 'document', 'audio', 'voice', 'sticker']
        )
        def media_handler(message):
            try:
                self._handle_media(message)
            except Exception as e:
                logging.error(f"Ошибка обработки медиа: {e}")
                self._safe_reply(message, "😅 Не получилось обработать вложение. Попробуй ещё раз позже.")

        # -------- ОБРАБОТЧИКИ TELEGRAM BUSINESS (Автоматизация чатов) --------
        # Сообщения из подключённого бизнес-аккаунта приходят отдельным типом
        # апдейта business_message, а не message — их ловит отдельный
        # декоратор business_message_handler.
        @self.bot.business_connection_handler()
        def business_connection_handler(business_connection):
            logging.info(
                f"Business connection: id={business_connection.id}, "
                f"is_enabled={getattr(business_connection, 'is_enabled', None)}"
            )

        @self.bot.business_message_handler(content_types=['text'])
        def business_text_handler(message):
            try:
                self._handle_text(message, business=True)
            except Exception as e:
                logging.error(f"Ошибка обработки business-сообщения: {e}")
                self._safe_reply(message, "😅 Что-то пошло не так. Попробуй ещё раз.", business=True)

        @self.bot.business_message_handler(
            content_types=['photo', 'video', 'video_note', 'document', 'audio', 'voice', 'sticker']
        )
        def business_media_handler(message):
            try:
                self._handle_media(message, business=True)
            except Exception as e:
                logging.error(f"Ошибка обработки business-медиа: {e}")
                self._safe_reply(message, "😅 Не получилось обработать вложение.", business=True)

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

    def _get_history(self, user_id: int) -> List[Dict[str, str]]:
        return self.user_histories.get(user_id, [])

    def _update_history(self, user_id: int, role: str, content: str) -> None:
        history = self.user_histories.get(user_id, [])
        history.append({"role": role, "content": content})
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        self.user_histories[user_id] = history

    def _clear_history(self, user_id: int) -> None:
        self.user_histories.pop(user_id, None)

    def _get_groq_response(self, user_id: int, user_message: str) -> str:
        self._update_history(user_id, "user", user_message)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self._get_history(user_id))

        try:
            completion = self.groq_client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=0.7,
                max_tokens=500,
            )
            reply = completion.choices[0].message.content
            reply = _strip_foreign_chars(reply)
            self._update_history(user_id, "assistant", reply)
            return reply
        except Exception as e:
            logging.error(f"Ошибка Groq: {e}")
            return "😅 Что-то пошло не так на моей стороне. Попробуй ещё раз чуть позже."

    def _send_welcome(self, message):
        user_id = message.from_user.id
        self._clear_history(user_id)
        welcome = (
            "Привет! Я Foo — твой ИИ-ассистент 😊 "
            "Пиши мне что угодно, с радостью помогу. "
            "Команда /reset — сбросить историю диалога, /help — справка."
        )
        self._safe_reply(message, welcome)
        self._update_history(user_id, "assistant", welcome)

    def _reset_dialog(self, message):
        user_id = message.from_user.id
        self._clear_history(user_id)
        self._safe_reply(message, "Диалог сброшен, начинаем с чистого листа 🙂")

    def _send_help(self, message):
        help_text = (
            "🤖 *Foo — ИИ-ассистент*\n\n"
            "Команды:\n"
            "/start — начать диалог заново\n"
            "/reset — сбросить историю сообщений\n"
            "/help — эта справка\n\n"
            "Просто пиши сообщения — я отвечу."
        )
        try:
            self.bot.reply_to(message, help_text, parse_mode='Markdown')
        except Exception as e:
            logging.error(f"Не удалось отправить справку: {e}")

    # -------- ЛОГИРОВАНИЕ ВХОДЯЩИХ/ИСХОДЯЩИХ СООБЩЕНИЙ --------
    @staticmethod
    def _describe_user(message) -> str:
        u = message.from_user
        username = f"@{u.username}" if getattr(u, "username", None) else (u.first_name or "unknown")
        return f"{username} (id={u.id})"

    def _log_incoming(self, message, business: bool) -> None:
        tag = "BUSINESS" if business else "CHAT"
        who = self._describe_user(message)
        logging.info(f"[{tag}] IN  | {who}: {message.text}")

    def _log_outgoing(self, message, reply_text: str, business: bool) -> None:
        tag = "BUSINESS" if business else "CHAT"
        who = self._describe_user(message)
        logging.info(f"[{tag}] OUT | -> {who}: {reply_text}")

    def _handle_text(self, message, business: bool = False):
        user_id = message.from_user.id
        user_text = message.text

        self._log_incoming(message, business)

        if not business and not self._is_subscribed(user_id):
            self._send_subscribe_prompt(message)
            return

        if not user_text or not user_text.strip():
            self._safe_reply(message, "Я понимаю только текстовые сообщения — напиши что-нибудь 🙂", business=business)
            return

        # Для бизнес-чатов приветствие при первом сообщении не шлём —
        # это выглядело бы странно в контексте общения с клиентом бизнеса.
        if not business and user_id not in self.user_histories:
            self._send_welcome(message)
            return

        reply = self._get_groq_response(user_id, user_text.strip())
        self._log_outgoing(message, reply, business)
        self._safe_reply(message, reply, business=business)

    def _handle_media(self, message, business: bool = False):
        self._log_incoming(message, business)

        if not business and not self._is_subscribed(message.from_user.id):
            self._send_subscribe_prompt(message)
            return
        # Модель сейчас не умеет анализировать фото/видео — отвечаем понятным
        # сообщением вместо падения, вместо того чтобы пытаться передать
        # непонятный контент в Groq.
        self._safe_reply(
            message,
            "🙂 Пока я умею работать только с текстом — фото, видео и файлы "
            "я, к сожалению, не обрабатываю. Опиши, что на них, словами!",
            business=business,
        )

    def _safe_reply(self, message, text: str, business: bool = False) -> None:
        """Отправляет ответ, не давая ошибке Telegram уронить бота.

        Для business-сообщений отправка идёт через send_message с
        business_connection_id — обычный reply_to для таких чатов не работает,
        так как сообщение нужно отправить от имени подключённого бизнес-аккаунта.
        """
        try:
            if business:
                connection_id = getattr(message, 'business_connection_id', None)
                self.bot.send_message(
                    message.chat.id,
                    text,
                    business_connection_id=connection_id,
                )
            else:
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

        # non_stop + собственный retry: если polling всё же упадёт
        # (обрыв сети и т.п.), бот перезапустится сам, а не умрёт насовсем.
        while True:
            try:
                self.bot.infinity_polling(timeout=30, long_polling_timeout=30)
            except Exception as e:
                logging.error(f"Ошибка в polling, перезапуск через 5 секунд: {e}")
                time.sleep(5)


if __name__ == "__main__":
    bot_instance = AssistantBot(TELEGRAM_TOKEN, GROQ_API_KEY)
    bot_instance.run()
