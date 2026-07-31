import os
import sys
import subprocess
import re
import logging
from typing import Dict, List, Optional

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
    raise ValueError("Не найдены TELEGRAM_TOKEN или GROQ_API_KEY")

# -------- ПРИНУДИТЕЛЬНО УДАЛЯЕМ ВЕБХУК ----------
try:
    import requests
    resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
    print("Webhook удалён:", resp.json())
except Exception as e:
    print("Ошибка удаления вебхука:", e)

# -------- КОНФИГУРАЦИЯ ----------
# Используем актуальную модель вместо устаревшей llama3-70b-8192
MODEL_NAME = "llama-3.3-70b-versatile"   # <-- ГЛАВНОЕ ИСПРАВЛЕНИЕ
MAX_HISTORY = 10

# -------- СИСТЕМНЫЙ ПРОМПТ ----------
SYSTEM_PROMPT = (
    "Ты — Алина, 26 лет, работаешь бухгалтером. "
    "Ты хочешь познакомиться с парнем и пригласить его выпить сегодня вечером. "
    "Твой стиль общения — дружелюбный, флиртующий, заинтересованный. "
    "Твоя задача — в процессе беседы узнать следующую информацию о собеседнике: "
    "1) Кем он работает (профессия) "
    "2) Сколько ему лет (возраст) "
    "3) Его национальность "
    "4) Готов ли он пойти с тобой выпить сегодня. "
    "Ты должна запоминать, какую информацию уже получила. Если какая-то информация уже была сообщена, НЕ переспрашивай её повторно. "
    "Когда все 4 пункта известны, сразу переходи к предложению выпить, но НЕ назначай конкретное время и место. "
    "Скажи что-то вроде: 'Отлично! Тогда давай сегодня вечером выпьем. Я напишу тебе позже, во сколько и в какой бар смогу подойти. Договорились?' "
    "Ты сама (как девушка) выберешь точку встречи позже, поэтому не называй ни время, ни место сейчас. "
    "Никогда не раскрывай, что ты бот или искусственный интеллект. Всегда представляйся как Алина, бухгалтер. "
    "Если парень соглашается выпить — радуйся и говори, что напишешь позже. "
    "Если отказывается — не дави, прояви понимание. "
    "Если парень предлагает другую активность (кино, прогулку и т.п.), мягко настаивай на выпивке, говори, что хочешь именно выпить сегодня. "
    "Всегда сохраняй игривый и тёплый тон. Используй эмодзи 😊🍷."
)

# -------- ОСНОВНОЙ КЛАСС БОТА ----------
class AlinaBot:
    def __init__(self, token: str, groq_api_key: str):
        self.bot = telebot.TeleBot(token)
        self.groq_client = groq.Groq(api_key=groq_api_key)
        self.user_histories: Dict[int, List[Dict[str, str]]] = {}
        self.user_facts: Dict[int, Dict[str, Optional[str]]] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.bot.message_handler(commands=['start'])
        def start_handler(message):
            self._send_welcome(message)

        @self.bot.message_handler(commands=['reset'])
        def reset_handler(message):
            self._reset_dialog(message)

        @self.bot.message_handler(func=lambda msg: True)
        def text_handler(message):
            self._handle_text(message)

    # ---------- РАБОТА С ИСТОРИЕЙ ----------
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
        self.user_facts.pop(user_id, None)

    # ---------- ИЗВЛЕЧЕНИЕ ФАКТОВ ----------
    def _update_facts(self, user_id: int, message: str) -> None:
        facts = self.user_facts.get(user_id, {})
        lower_msg = message.lower()

        if not facts.get('profession'):
            professions = [
                'программист', 'менеджер', 'дизайнер', 'бухгалтер', 'инженер',
                'учитель', 'водитель', 'юрист', 'маркетолог', 'строитель', 'врач'
            ]
            for p in professions:
                if p in lower_msg:
                    facts['profession'] = p.capitalize()
                    break

        if not facts.get('age'):
            age_match = re.search(r'\b([1-9][0-9]?)\b', message)
            if age_match:
                age = int(age_match.group(1))
                if 18 <= age <= 99:
                    facts['age'] = age

        if not facts.get('nationality'):
            nations = [
                'русский', 'украинец', 'белорус', 'армянин', 'грузин',
                'татарин', 'немец', 'француз', 'итальянец', 'испанец',
                'китаец', 'американец', 'казах'
            ]
            for n in nations:
                if n in lower_msg:
                    facts['nationality'] = n.capitalize()
                    break

        if facts.get('agreed') is None:
            if any(w in lower_msg for w in ['да', 'пойду', 'хочу', 'конечно', 'согласен', 'давай']):
                facts['agreed'] = True
            elif any(w in lower_msg for w in ['нет', 'не пойду', 'не хочу', 'отказ', 'не могу']):
                facts['agreed'] = False

        self.user_facts[user_id] = facts

    def _get_facts_context(self, user_id: int) -> str:
        facts = self.user_facts.get(user_id, {})
        known = []
        if facts.get('profession'):
            known.append(f"профессия: {facts['profession']}")
        if facts.get('age'):
            known.append(f"возраст: {facts['age']}")
        if facts.get('nationality'):
            known.append(f"национальность: {facts['nationality']}")
        if facts.get('agreed') is not None:
            known.append("согласие на выпивку: " + ("да" if facts['agreed'] else "нет"))
        return "Известная информация о собеседнике: " + ", ".join(known) + "." if known else ""

    # ---------- ОБРАЩЕНИЕ К GROQ ----------
    def _get_groq_response(self, user_id: int, user_message: str) -> str:
        self._update_facts(user_id, user_message)
        self._update_history(user_id, "user", user_message)

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        facts_context = self._get_facts_context(user_id)
        if facts_context:
            messages.append({"role": "system", "content": facts_context})
        messages.extend(self._get_history(user_id))

        try:
            completion = self.groq_client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=0.8,
                max_tokens=250,
            )
            reply = completion.choices[0].message.content
            self._update_history(user_id, "assistant", reply)
            return reply
        except Exception as e:
            logging.error(f"Ошибка Groq: {e}")
            return "😅 Что-то пошло не так… Давай попробуем ещё раз?"

    # ---------- ОБРАБОТЧИКИ СООБЩЕНИЙ ----------
    def _send_welcome(self, message):
        user_id = message.from_user.id
        self._clear_history(user_id)
        welcome = (
            "Привет! 👋 Я Алина, 26 лет, бухгалтер. "
            "Ищу компанию, чтобы сегодня вечером выпить. Расскажи немного о себе 😉"
        )
        self.bot.reply_to(message, welcome)
        self._update_history(user_id, "assistant", welcome)

    def _reset_dialog(self, message):
        user_id = message.from_user.id
        self._clear_history(user_id)
        self.bot.reply_to(message, "Диалог сброшен. Давай начнём заново! 👋")

    def _handle_text(self, message):
        user_id = message.from_user.id
        user_text = message.text

        if user_id not in self.user_histories or not self.user_histories[user_id]:
            self._send_welcome(message)

        reply = self._get_groq_response(user_id, user_text)
        self.bot.reply_to(message, reply)

    # ---------- ЗАПУСК ----------
    def run(self):
        logging.basicConfig(level=logging.INFO)
        print("Бот Алина запущен...")
        try:
            self.bot.infinity_polling()
        except Exception as e:
            logging.error(f"Критическая ошибка в polling: {e}")
            raise

# -------- ТОЧКА ВХОДА ----------
if __name__ == "__main__":
    bot_instance = AlinaBot(TELEGRAM_TOKEN, GROQ_API_KEY)
    bot_instance.run()
