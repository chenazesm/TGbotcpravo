import os
import json
import logging
import threading
import time
import random
import requests
import re
from typing import Dict, Any, List

import telebot
from telebot import types
from flask import Flask, request, jsonify, send_from_directory
import redis

TG_TOKEN = os.getenv("TG_BOT_TOKEN")
CALLBACK_SECRET = os.getenv("CALLBACK_SECRET", "secret")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
AI_KEY = os.getenv("DEEPSEEK_API_KEY")

WEBHOOK_URL = "https://cybertutor.ru"

SCENARIOS_PATH = os.getenv("SCENARIOS_PATH", "/app/scenarios.json")
HOST = "0.0.0.0"
PORT = 8080

if not TG_TOKEN: raise RuntimeError("TG_BOT_TOKEN required")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("cyber_sim")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
bot = telebot.TeleBot(TG_TOKEN, parse_mode="HTML")
flask_app = Flask(__name__)

SCENARIOS = []
try:
    with open(SCENARIOS_PATH, "r", encoding="utf-8") as f:
        SCENARIOS = json.load(f)
    logger.info(f"Загружено {len(SCENARIOS)} сценариев.")
except Exception as e:
    logger.error(f"Ошибка загрузки сценариев: {e}")

def evaluate_with_ai(scenario_text, threat, correct_actions, user_answer):
    clean_key = str(AI_KEY).strip() if AI_KEY else None
    
    if not clean_key or clean_key == "None":
        return {"is_correct": False, "ai_comment": "Ошибка: API ключ не считан из .env."}

    prompt = f"""
    Ситуация: {scenario_text}
    Тип угрозы: {threat}
    Пользователь ответил: {user_answer}
    Верни ответ СТРОГО в формате JSON:
    {{"is_correct": true/false, "ai_comment": "короткое пояснение на русском"}}
    """

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json"
    }
    
    data = {
        "model": "llama-3.1-8b-instant", 
        "messages": [
            {
                "role": "user", 
                "content": prompt
            }
        ],
        "temperature": 0.2
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"GROQ ERROR: {response.status_code} - {response.text}")
            return {
                "is_correct": False, 
                "ai_comment": f"Ошибка {response.status_code}. Проверьте правильность ключа в .env"
            }
            
        res_json = response.json()
        content = res_json['choices'][0]['message']['content']
        
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return json.loads(match.group())
        return json.loads(content)

    except Exception as e:
        logger.error(f"AI ERROR: {str(e)}")
        return {"is_correct": False, "ai_comment": "Техническая ошибка связи."}

def get_player(chat_id):
    key = f"player:{chat_id}"
    raw = redis_client.get(key)
    if raw: return json.loads(raw)
    p = {"hp": 100, "xp": 0, "idx": 0, "mistakes": {}, "ai_mode": True}
    save_player(chat_id, p)
    return p

def save_player(chat_id, p):
    redis_client.set(f"player:{chat_id}", json.dumps(p), ex=604800)

def send_level(chat_id):
    p = get_player(chat_id)
    idx = p.get("idx", 0)

    if idx >= len(SCENARIOS):
        bot.send_message(chat_id, f"🎉 <b>Игра окончена! Ты научился распознавать угрозы в сети. </b>\n⭐ XP: {p['xp']} | ❤️ HP: {p['hp']}")
        return

    task = SCENARIOS[idx]
    game_mode = task.get("game_mode", "native")

    text = f"<b>Уровень {idx+1}</b>\n\n{task['text']}"
    img_url = task.get("media")
    voice_file = task.get("voice") 

    try:
        if game_mode == "webapp":
            markup = types.InlineKeyboardMarkup()
            specific_path = task.get("webapp_path") 
            
            base_url = WEBHOOK_URL.strip('/')
            clean_path = specific_path.lstrip('/')
            full_url = f"{base_url}/{clean_path}"
                
            logger.info(f"Opening WebApp: {full_url}") #  отладкa
            
            webapp_info = types.WebAppInfo(full_url)
            markup.add(types.InlineKeyboardButton("Открыть мини-приложение", web_app=webapp_info))
            if img_url:
                bot.send_photo(chat_id, img_url, caption=text, reply_markup=markup)
            else:
                bot.send_message(chat_id, text, reply_markup=markup)

        else:
            markup = types.InlineKeyboardMarkup(row_width=1)
            
            if p.get("ai_mode", True):
                markup.add(types.InlineKeyboardButton("💡 Подсказка", callback_data=f"hint:{idx}"))
                instructions = "\n\n✍️ <i>Напиши текстом в чат, как ты поступишь в этой ситуации:</i>"
                display_text = text + instructions
            
            else:
                action_buttons = []
                if "btn_trust" in task:
                    action_buttons.append(types.InlineKeyboardButton(task["btn_trust"], callback_data=f"ans:{idx}:trust"))
                if "btn_check" in task:
                    action_buttons.append(types.InlineKeyboardButton(task["btn_check"], callback_data=f"ans:{idx}:check"))
                if "btn_ban" in task:
                    action_buttons.append(types.InlineKeyboardButton(task["btn_ban"], callback_data=f"ans:{idx}:ban"))
                
                random.shuffle(action_buttons)
                for btn in action_buttons:
                    markup.add(btn)
                
                markup.add(types.InlineKeyboardButton("💡 Подсказка", callback_data=f"hint:{idx}"))
                display_text = text

            if voice_file:
                voice_path = os.path.join(WEBAPP_DIR, voice_file)
                try:
                    with open(voice_path, 'rb') as v_file:
                        bot.send_voice(chat_id, v_file, caption=display_text, reply_markup=markup, parse_mode="HTML")
                except Exception as e:
                    if "VOICE_MESSAGES_FORBIDDEN" in str(e):
                        bot.send_message(chat_id, f"🎧 <i>[Голосовое сообщение заблокировано]</i>\n\n{display_text}", reply_markup=markup, parse_mode="HTML")
                    else: raise e
            elif img_url:
                bot.send_photo(chat_id, img_url, caption=display_text, reply_markup=markup, parse_mode="HTML")
            else:
                bot.send_message(chat_id, display_text, reply_markup=markup, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error sending level: {e}")
        bot.send_message(chat_id, f"⚠️ Ошибка отправки уровня: {e}")

@bot.message_handler(func=lambda msg: msg.text == "Смена режима ИИ")
def toggle_ai_mode(message):
    p = get_player(message.chat.id)
    p["ai_mode"] = not p.get("ai_mode", True)
    save_player(message.chat.id, p)    
    send_level(message.chat.id)

@bot.message_handler(commands=['theory'])
@bot.message_handler(func=lambda msg: msg.text == "Теория")
def handle_theory(message):
    markup = types.InlineKeyboardMarkup()
    web_app_info = types.WebAppInfo(f"{WEBHOOK_URL}/theory.html")
    markup.add(types.InlineKeyboardButton("📖 Читать теорию", web_app=web_app_info))
    
    bot.send_message(
        message.chat.id,
        "<b>База знаний</b>\n\nЗдесь собрана информация об основных киберугрозах. Изучи её, чтобы проходить уровни без ошибок!",
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    p = get_player(call.message.chat.id)
    parts = call.data.split(":")
    
    if parts[0] == "hint":
        idx = int(parts[1])
        task = SCENARIOS[idx]
        bot.answer_callback_query(call.id, task.get("hint", "Подсказки нет"), show_alert=True)
        return

    if parts[0] == "ans":
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

        idx = int(parts[1])
        action = parts[2]
        
        if idx != p["idx"]:
            bot.answer_callback_query(call.id, "Уровень устарел!")
            send_level(call.message.chat.id)
            return

        task = SCENARIOS[idx]
        correct_answers = task.get("correct",[])
        
        if isinstance(correct_answers, str):
            correct_answers = [correct_answers]
        is_correct = (action in correct_answers)
        
        if is_correct:
            p["xp"] += 25
            res_text = "✅ Верно!"
        else:
            p["hp"] -= 20
            res_text = "❌ Ошибка!"
            threat_name = task.get("threat", "Неизвестная угроза")
            p.setdefault("mistakes", {})
            p["mistakes"][threat_name] = p["mistakes"].get(threat_name, 0) + 1

        bot.answer_callback_query(call.id, "Ответ принят")
        
        feedback = f"{res_text}\n\n{task.get('feedback')}\n\n❤️ HP: {p['hp']} | ⭐ XP: {p['xp']}"
        bot.send_message(call.message.chat.id, feedback)

        p["idx"] += 1
        save_player(call.message.chat.id, p)
        
        if p["hp"] <= 0:
            game_over_msg = "💀 <b>Game Over</b>\n\nНапиши /start чтобы начать заново."
            
            if p["idx"] >= 4 and p.get("mistakes"):
                worst_threat = max(p["mistakes"], key=p["mistakes"].get)
                game_over_msg = f"💀 <b>Game Over</b>\n\nТы часто допускал ошибки в угрозе под названием <b>«{worst_threat}»</b>. Используй кнопку «Теория», чтобы изучить эту угрозу!\n\nНапиши /start чтобы начать заново."

            bot.send_message(call.message.chat.id, game_over_msg, parse_mode="HTML")
            
            p["hp"] = 100
            p["idx"] = 0
            p["mistakes"] = {}
            save_player(call.message.chat.id, p)
        else:
            time.sleep(0.5)
            send_level(call.message.chat.id)


@bot.message_handler(func=lambda msg: msg.text not in ["Теория", "Рестарт", "/start", "/theory"])
def handle_user_text_answer(message):
    chat_id = message.chat.id
    user_text = message.text

    p = get_player(chat_id)
    idx = p.get("idx", 0)
    if not p.get("ai_mode", True):
        return 

    if idx >= len(SCENARIOS):
        bot.send_message(chat_id, "Игра завершена. Нажми «Рестарт» для новой игры.")
        return

    task = SCENARIOS[idx]
    game_mode = task.get("game_mode", "native")

    if game_mode == "webapp":
        bot.send_message(chat_id, "В этом уровне необходимо взаимодействовать с интерфейсом. Нажми кнопку «Открыть мини-приложение».")
        return

    bot.send_chat_action(chat_id, 'typing')

    ai_result = evaluate_with_ai(
        scenario_text=task['text'],
        threat=task.get('threat', 'Неизвестно'),
        correct_actions=task.get('correct', []),
        user_answer=user_text
    )

    is_correct = ai_result.get("is_correct", False)
    ai_comment = ai_result.get("ai_comment", "Действие не распознано.")

    if is_correct:
        p["xp"] += 25
        res_text = "✅ Верно!"
    else:
        p["hp"] -= 20
        res_text = "❌ Ошибка!"
        threat_name = task.get("threat", "Неизвестная угроза")
        p.setdefault("mistakes", {})
        p["mistakes"][threat_name] = p["mistakes"].get(threat_name, 0) + 1

    feedback = f"{res_text}\n\n{ai_comment}\n\n{task.get('feedback', '')}\n\n❤️ HP: {p['hp']} | ⭐ XP: {p['xp']}"
    
    bot.send_message(chat_id, feedback, parse_mode="HTML")

    p["idx"] += 1
    save_player(chat_id, p)
    
    if p["hp"] <= 0:
        game_over_msg = "💀 <b>Game Over</b>\n\nНапиши /start чтобы начать заново."
        if p["idx"] >= 4 and p.get("mistakes"):
            worst_threat = max(p["mistakes"], key=p["mistakes"].get)
            game_over_msg = f"💀 <b>Game Over</b>\n\nТы часто допускал ошибки в угрозе под названием <b>«{worst_threat}»</b>. Используй кнопку «Теория», чтобы изучить эту угрозу!\n\nНапиши /start чтобы начать заново."

        bot.send_message(chat_id, game_over_msg, parse_mode="HTML")
        p["hp"] = 100
        p["idx"] = 0
        p["mistakes"] = {}
        save_player(chat_id, p)
    else:
        time.sleep(0.5)
        send_level(chat_id)

@bot.message_handler(commands=['start'])
@bot.message_handler(func=lambda msg: msg.text == "Рестарт")
def handle_start(message):
    old_p = get_player(message.chat.id)
    ai_mode = old_p.get("ai_mode", True)
    
    p = {"hp": 100, "xp": 0, "idx": 0, "mistakes": {}, "ai_mode": ai_mode}
    save_player(message.chat.id, p)

    menu_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    btn_theory = types.KeyboardButton("Теория")
    btn_restart = types.KeyboardButton("Рестарт")
    mode_text = "Смена режима ИИ"
    btn_mode = types.KeyboardButton(mode_text)
    
    menu_markup.row(btn_theory, btn_mode)
    menu_markup.row(btn_restart)

    github_url = "https://github.com/chenazesm/TGbotcpravo" 
    welcome_text = (
        "<b>Добро пожаловать в CSGame!</b>\n\n"
        "❤️ <b>HP</b> — твое здоровье.\n"
        "⭐ <b>XP</b> — опыт.\n\n"
        "🚀 <b>Внимание!</b> Бот был обновлен до новой версии, в которую встроен ИИ для анализа ответов. "
        f"Подробнее о разработке на <a href='{github_url}'>GitHub</a>.\n\n"
        "Если вы хотите вернуться к классическим кнопкам выбора для этого игрового сеанса, нажмите кнопку <b>«Смена режима ИИ»</b> в меню."
    )

    bot.send_message(message.chat.id, welcome_text, parse_mode="HTML", reply_markup=menu_markup, disable_web_page_preview=True)
    time.sleep(1) 
    send_level(message.chat.id)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEBAPP_DIR = os.path.join(BASE_DIR, "webapp")

@flask_app.route('/')
def serve_index():
    if not os.path.exists(WEBAPP_DIR):
        return f"Ошибка: Папка {WEBAPP_DIR} не найдена!", 404
    return send_from_directory(WEBAPP_DIR, 'main.html')

@flask_app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory(WEBAPP_DIR, filename)

@flask_app.route("/api/get_state", methods=["POST"])
def get_state():
    chat_id = request.json.get("chat_id")
    if not chat_id: return jsonify({"error": "no chat_id"}), 400
    
    p = get_player(chat_id)
    idx = p.get("idx", 0)
    
    if idx >= len(SCENARIOS): return jsonify({"game_over": True})
    
    return jsonify({
        "scenario": SCENARIOS[idx], 
        "hp": p["hp"], 
        "xp": p["xp"]
    })

@flask_app.route("/api/submit_answer", methods=["POST"])
def submit_answer():
    data = request.json
    chat_id = data.get("chat_id")
    is_win = data.get("is_win", False)

    p = get_player(chat_id)
    idx = p.get("idx", 0) 
    
    feedback_text = ""
    if idx < len(SCENARIOS):
        feedback_text = SCENARIOS[idx].get("feedback", "")
    
    if is_win:
        p["xp"] += 50
        res_text = "✅ Верно! (+50 XP)"
    else:
        p["hp"] -= 30
        res_text = "❌ Ошибка! (-30 HP)"
        threat_name = SCENARIOS[idx].get("threat", "Неизвестная угроза") if idx < len(SCENARIOS) else "Неизвестная угроза"
        p.setdefault("mistakes", {})
        p["mistakes"][threat_name] = p["mistakes"].get(threat_name, 0) + 1

    result_msg = f"{res_text}\n\n{feedback_text}\n\n❤️ HP: {p['hp']} | ⭐ XP: {p['xp']}"

    p["idx"] += 1
    save_player(chat_id, p)

    bot.send_message(chat_id, result_msg)
    
    if p["hp"] > 0:
        time.sleep(1)
        send_level(chat_id)
    else:
        game_over_msg = "💀 <b>Game Over</b>\n\nНапиши /start чтобы начать заново."
        
        if p["idx"] >= 4 and p.get("mistakes"):
            worst_threat = max(p["mistakes"], key=p["mistakes"].get)
            game_over_msg = f"💀 <b>Game Over</b>\n\nТы часто допускал ошибки в угрозе под названием <b>«{worst_threat}»</b>. Используй кнопку «Теория», чтобы изучить эту угрозу!\n\nНапиши /start чтобы начать заново."

        bot.send_message(chat_id, game_over_msg, parse_mode="HTML")
        
        p["hp"] = 100
        p["idx"] = 0
        p["mistakes"] = {}
        save_player(chat_id, p)

    return jsonify({"status": "ok"})

def run_flask():
    flask_app.run(host=HOST, port=PORT, use_reloader=False)
    
@flask_app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    else:
        abort(403)

if __name__ == "__main__":
    bot.remove_webhook()
    time.sleep(1)
    
    bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    
    logger.info(f"--- Production Mode ---")
    logger.info(f"Webhook set to: {WEBHOOK_URL}/webhook")
    flask_app.run(host=HOST, port=PORT)