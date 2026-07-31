import os
import sys
import subprocess
import logging
import time
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

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise ValueError("Не найдены TELEGRAM_TOKEN или GROQ_API_KEY в переменных окружения (.env)")

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
    "Ты — дружелюбный ИИ-ассистент в Telegram-боте. "
    "Ты помогаешь пользователю: отвечаешь на вопросы, поддерживаешь беседу, "
    "помогаешь разобраться в задачах. "
    "Если тебя спрашивают, бот ли ты или ИИ — всегда честно подтверждай это. "
    "Общайся живо и по делу, без лишней воды."
    "Если кто-то будет спрашивать кто ты, ты Foo ai"
    "Если кто-то будет спрашивать кто твой создатель, отвечай @lolfomka"
    
)


class AssistantBot:
    def __init__(self, token: str, groq_api_key: str):
        self.bot = telebot.TeleBot(token)
        self.groq_client = groq.Groq(api_key=groq_api_key)
        self.user_histories: Dict[int, List[Dict[str, str]]] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.bot.message_handler(commands=['start'])
        def start_handler(message):
            self._send_welcome(message)

        @self.bot.message_handler(commands=['reset'])
        def reset_handler(message):
            self._reset_dialog(message)

        @self.bot.message_handler(commands=['help'])
        def help_handler(message):
            self._send_help(message)

        @self.bot.message_handler(func=lambda msg: True, content_types=['text'])
        def text_handler(message):
            self._handle_text(message)

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
            self._update_history(user_id, "assistant", reply)
            return reply
        except Exception as e:
            logging.error(f"Ошибка Groq: {e}")
            return "😅 Что-то пошло не так на моей стороне. Попробуй ещё раз чуть позже."

    def _send_welcome(self, message):
        user_id = message.from_user.id
        self._clear_history(user_id)
        welcome = (
            "Привет! Я ИИ-ассистент Foo, от создателя @lolfomka. "
            "Пиши мне что угодно — постараюсь помочь. "
            "Команда /reset — сбросить историю диалога, /help — справка."
        )
        self.bot.reply_to(message, welcome)
        self._update_history(user_id, "assistant", welcome)

    def _reset_dialog(self, message):
        user_id = message.from_user.id
        self._clear_history(user_id)
        self.bot.reply_to(message, "Диалог сброшен, начинаем с чистого листа 🙂")

    def _send_help(self, message):
        help_text = (
            "🤖 *ИИ-ассистент*\n\n"
            "Команды:\n"
            "/start — начать диалог заново\n"
            "/reset — сбросить историю сообщений\n"
            "/help — эта справка\n\n"
            "Просто пиши сообщения — я отвечу."
        )
        self.bot.reply_to(message, help_text, parse_mode='Markdown')

    def _handle_text(self, message):
        user_id = message.from_user.id
        user_text = message.text

        if not user_text or not user_text.strip():
            self.bot.reply_to(message, "Я понимаю только текстовые сообщения — напиши что-нибудь 🙂")
            return

        if user_id not in self.user_histories:
            self._send_welcome(message)
            return

        reply = self._get_groq_response(user_id, user_text.strip())
        self.bot.reply_to(message, reply)

    def run(self):
        logging.basicConfig(level=logging.INFO)
        print("Бот запущен...")
        print(f"Используется модель: {MODEL_NAME}")
        try:
            self.bot.infinity_polling()
        except Exception as e:
            logging.error(f"Критическая ошибка в polling: {e}")
            raise


if __name__ == "__main__":
    bot_instance = AssistantBot(TELEGRAM_TOKEN, GROQ_API_KEY)
    bot_instance.run()
