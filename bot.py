import os
import logging
import time
import random
import re
from typing import Dict, List

import telebot
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")
MODEL_NAME     = "llama3-70b-8192"
MAX_HISTORY    = 10
MIN_DELAY      = 30.0
MAX_DELAY      = 90.0

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise ValueError("Не найдены переменные окружения TELEGRAM_TOKEN или GROQ_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)

user_histories: Dict[int, List[dict]] = {}
user_facts: Dict[int, dict] = {}

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

def get_history(user_id: int) -> List[dict]:
    return user_histories.get(user_id, [])

def update_history(user_id: int, role: str, content: str):
    history = user_histories.get(user_id, [])
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    user_histories[user_id] = history

def clear_history(user_id: int):
    if user_id in user_histories:
        del user_histories[user_id]
    if user_id in user_facts:
        del user_facts[user_id]

def update_facts(user_id: int, user_message: str):
    facts = user_facts.get(user_id, {})
    if not facts.get('profession'):
        professions = ['программист', 'менеджер', 'дизайнер', 'бухгалтер', 'инженер', 'учитель', 'водитель', 'юрист', 'маркетолог', 'строитель', 'врач']
        for p in professions:
            if p in user_message.lower():
                facts['profession'] = p.capitalize()
                break
    if not facts.get('age'):
        ages = re.findall(r'\b([1-9][0-9]?)\b', user_message)
        for a in ages:
            age = int(a)
            if 18 <= age <= 99:
                facts['age'] = age
                break
    if not facts.get('nationality'):
        nations = ['русский', 'украинец', 'белорус', 'армянин', 'грузин', 'татарин', 'немец', 'француз', 'итальянец', 'испанец', 'китаец', 'американец', 'казах']
        for n in nations:
            if n in user_message.lower():
                facts['nationality'] = n.capitalize()
                break
    if facts.get('agreed') is None:
        if any(w in user_message.lower() for w in ['да', 'пойду', 'хочу', 'конечно', 'согласен', 'давай']):
            facts['agreed'] = True
        elif any(w in user_message.lower() for w in ['нет', 'не пойду', 'не хочу', 'отказ', 'не могу']):
            facts['agreed'] = False
    user_facts[user_id] = facts

def get_facts_context(user_id: int) -> str:
    facts = user_facts.get(user_id, {})
    known = []
    if facts.get('profession'):
        known.append(f"профессия: {facts['profession']}")
    if facts.get('age'):
        known.append(f"возраст: {facts['age']}")
    if facts.get('nationality'):
        known.append(f"национальность: {facts['nationality']}")
    if facts.get('agreed') is not None:
        known.append("согласие на выпивку: " + ("да" if facts['agreed'] else "нет"))
    if known:
        return "Известная информация о собеседнике: " + ", ".join(known) + "."
    return ""

def get_groq_response(user_id: int, user_message: str) -> str:
    update_facts(user_id, user_message)
    update_history(user_id, "user", user_message)
    facts_context = get_facts_context(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if facts_context:
        messages.append({"role": "system", "content": facts_context})
    messages.extend(get_history(user_id))
    try:
        completion = groq_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.8,
            max_tokens=250,
        )
        reply = completion.choices[0].message.content
        update_history(user_id, "assistant", reply)
        return reply
    except Exception as e:
        logging.error(f"Ошибка Groq: {e}")
        return "😅 Что-то пошло не так… Давай попробуем ещё раз?"

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    clear_history(user_id)
    welcome_text = (
        "Привет! 👋 Я Алина, 26 лет, бухгалтер. "
        "Ищу компанию, чтобы сегодня вечером выпить. Расскажи немного о себе 😉"
    )
    bot.reply_to(message, welcome_text)
    update_history(user_id, "assistant", welcome_text)

@bot.message_handler(commands=['reset'])
def reset_dialog(message):
    user_id = message.from_user.id
    clear_history(user_id)
    bot.reply_to(message, "Диалог сброшен. Давай начнём заново! 👋")

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    user_id = message.from_user.id
    user_text = message.text
    if user_id not in user_histories or len(user_histories[user_id]) == 0:
        send_welcome(message)
    reply = get_groq_response(user_id, user_text)
    delay = random.uniform(MIN_DELAY, MAX_DELAY)
    time.sleep(delay)
    bot.reply_to(message, reply)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Бот Алина запущен...")
    bot.infinity_polling()
