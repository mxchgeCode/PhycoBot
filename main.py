import os
import signal
import sqlite3
import sys

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, PollAnswerHandler, CallbackQueryHandler, ContextTypes

DB_PATH = "bot.db"


def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Таблица опросов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            options TEXT NOT NULL,
            poll_type TEXT DEFAULT 'general'
        )
    ''')

    # Таблица ответов (все прохождения накапливаются)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            option_index INTEGER NOT NULL,
            run_id INTEGER DEFAULT 1,
            FOREIGN KEY (poll_id) REFERENCES polls(id)
        )
    ''')

    conn.commit()
    conn.close()


def get_polls():
    """Получить все опросы из БД"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, question, options FROM polls ORDER BY id')
    rows = cursor.fetchall()
    conn.close()

    polls = []
    for row in rows:
        polls.append({
            "id": row[0],
            "question": row[1],
            "options": row[2].split("|||")
        })
    return polls


def save_answer(poll_id: int, user_id: int, option_index: int, run_id: int = 1):
    """Сохранить ответ в БД"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO answers (poll_id, user_id, option_index, run_id) VALUES (?, ?, ?, ?)',
        (poll_id, user_id, option_index, run_id)
    )
    conn.commit()
    conn.close()


def get_user_runs(user_id: int) -> int:
    """Получить количество прохождений пользователя"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT COUNT(DISTINCT run_id) FROM answers WHERE user_id = ?',
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] + 1  # +1 потому что текущее прохождение ещё не сохранено


def get_poll_stats(poll_id: int) -> dict:
    """Получить статистику опроса (все прохождения)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT option_index, COUNT(*) FROM answers WHERE poll_id = ? GROUP BY option_index',
        (poll_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    stats = {}
    for row in rows:
        stats[row[0]] = row[1]
    return stats


# Маппинг реальных poll_id от Telegram к нашим ID
poll_id_mapping: dict[str, dict[str, int]] = {}


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors: log Conflict briefly, others with traceback."""
    if isinstance(context.error, Conflict):
        print(
            "Conflict: another bot instance is polling (getUpdates). "
            "Stop other runs of this bot or wait for them to exit."
        )
        return
    print(f"Update {update} caused error: {context.error}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запуск опросов с первого вопроса (всегда)"""
    if update.message is None or update.message.from_user is None:
        return
    
    user_id = update.message.from_user.id

    polls = get_polls()
    if not polls:
        await update.message.reply_text("Опросы не найдены в базе данных")
        return

    # Вычисляем номер прохождения
    run_id = get_user_runs(user_id)

    # Сохраняем контекст
    user_data = context.user_data
    if user_data is not None:
        user_data['polls'] = polls
        user_data['run_id'] = run_id
        user_data['current_poll_index'] = 0

    await update.message.reply_text(f"Прохождение #{run_id}. Начинаем!")

    # Запускаем первый опрос
    await send_poll(update.message.chat_id, 0, context.bot, polls)


async def send_poll(chat_id: int, poll_index: int, bot, polls: list) -> None:
    """Отправляет опрос по индексу"""
    if poll_index >= len(polls):
        # Все опросы пройдены
        await bot.send_message(
            chat_id=chat_id,
            text="Опрос пройден успешно!",
            reply_markup=keyboard_finish()
        )
        return

    poll_data = polls[poll_index]

    # Сохраняем маппинг poll_id Telegram к нашему ID
    sent_poll = await bot.send_poll(
        chat_id=chat_id,
        question=poll_data["question"],
        options=poll_data["options"],
        is_anonymous=False
    )

    poll_id_mapping[sent_poll.poll.id] = {
        "db_id": poll_data["id"],
        "index": poll_index
    }


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает ответы на опросы"""
    poll_answer = update.poll_answer
    if poll_answer is None or poll_answer.user is None:
        return
    
    real_poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id

    poll_info = poll_id_mapping.get(real_poll_id)
    if poll_info is None:
        return

    db_id = poll_info["db_id"]
    poll_index = poll_info["index"]
    
    user_data = context.user_data
    run_id = 1
    if user_data is not None:
        run_id = user_data.get('run_id', 1)

    # Сохраняем ответ в БД
    if poll_answer.option_ids is not None:
        for option in poll_answer.option_ids:
            save_answer(db_id, user_id, option, run_id)

    # Переходим к следующему опросу
    polls = []
    if user_data is not None:
        polls = user_data.get('polls', [])
    next_index = poll_index + 1
    if user_data is not None:
        user_data['current_poll_index'] = next_index

    chat_id = user_id
    await send_poll(chat_id, next_index, context.bot, polls)


async def stats_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать статистику всех опросов (все прохождения)"""
    if update.message is None:
        return
    text = get_stats_text()
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard_stats())


async def restart_survey(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Перезапуск опроса с первого вопроса (как /start)."""
    polls = get_polls()
    if not polls:
        await bot.send_message(chat_id=chat_id, text="Опросы не найдены в базе данных")
        return

    run_id = get_user_runs(user_id)
    user_data = context.user_data
    if user_data is not None:
        user_data['polls'] = polls
        user_data['run_id'] = run_id
        user_data['current_poll_index'] = 0

    await bot.send_message(chat_id=chat_id, text=f"Прохождение #{run_id}. Начинаем!")
    await send_poll(chat_id, 0, bot, polls)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на кнопки"""
    query = update.callback_query
    if query is None or query.from_user is None or query.message is None:
        return
    
    await query.answer()

    if query.data == 'restart':
        user_id = query.from_user.id
        chat_id = query.message.chat.id
        await restart_survey(chat_id, user_id, context, context.bot)
        return

    if query.data == 'stats':
        await query.edit_message_text(text=get_stats_text(), parse_mode='Markdown', reply_markup=keyboard_stats())
        return

    # Запрос подтверждения сброса (от экрана завершения или от статистики)
    if query.data in ('reset_ask_finish', 'reset_ask_stats'):
        no_callback = 'reset_no_finish' if query.data == 'reset_ask_finish' else 'reset_no_stats'
        confirm_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Да", callback_data='reset_yes'), InlineKeyboardButton("Нет", callback_data=no_callback)],
        ])
        await query.edit_message_text(
            text="Вы уверены? Это действие удалит данные статистики.",
            reply_markup=confirm_markup
        )
        return

    if query.data == 'reset_yes':
        clear_all_answers()
        await query.edit_message_text(text="Данные сброшены.", reply_markup=keyboard_finish())
        return
    if query.data == 'reset_no_finish':
        await query.edit_message_text(text="Опрос пройден успешно!", reply_markup=keyboard_finish())
        return
    if query.data == 'reset_no_stats':
        await query.edit_message_text(text=get_stats_text(), parse_mode='Markdown', reply_markup=keyboard_stats())


def add_poll(question: str, options: list, poll_type: str = 'general'):
    """Добавить опрос в БД"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO polls (question, options, poll_type) VALUES (?, ?, ?)',
        (question, "|||".join(options), poll_type)
    )
    conn.commit()
    conn.close()


def clear_all_answers():
    """Очистить все ответы (для тестирования)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM answers')
    conn.commit()
    conn.close()


def get_stats_text() -> str:
    """Сформировать текст статистики опросов."""
    polls = get_polls()
    text = "📊 **Статистика опросов**\n\n"
    for i, poll_data in enumerate(polls):
        stats = get_poll_stats(poll_data["id"])
        text += f"**{i+1}. {poll_data['question']}**\n"
        text += "| Вариант | Голосов |\n"
        text += "|---------|--------|\n"
        total_votes = 0
        for j, option in enumerate(poll_data["options"]):
            count = stats.get(j, 0)
            total_votes += count
            text += f"| {option} | {count} |\n"
        text += f"**Всего голосов: {total_votes}**\n\n"
    return text


def keyboard_finish():
    """Клавиатура после завершения опроса."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Запустить снова", callback_data='restart')],
        [InlineKeyboardButton("📊 Статистика", callback_data='stats')],
        [InlineKeyboardButton("🗑 Сброс данных", callback_data='reset_ask_finish')],
    ])


def keyboard_stats():
    """Клавиатура под сообщением со статистикой."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Запустить снова", callback_data='restart')],
        [InlineKeyboardButton("🗑 Сброс данных", callback_data='reset_ask_stats')],
    ])


def main():
    # Инициализируем БД
    init_db()

    load_dotenv()
    token = os.getenv('BOT_TOKEN')

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_error_handler(error_handler)

    def signal_handler(_sig, _frame):
        print('\nОстановка бота...')
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
