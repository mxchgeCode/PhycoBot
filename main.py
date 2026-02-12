import logging
import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, PollAnswerHandler, CallbackQueryHandler, ContextTypes

# Включаем логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

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

    # Добавляем столбец run_id если его нет (миграция)
    try:
        cursor.execute('SELECT run_id FROM answers LIMIT 1')
    except sqlite3.OperationalError:
        cursor.execute('ALTER TABLE answers ADD COLUMN run_id INTEGER DEFAULT 1')
        logger.info("Added run_id column to answers table")

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
poll_id_mapping = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запуск опросов с первого вопроса (всегда)"""
    user_id = update.message.from_user.id

    polls = get_polls()
    if not polls:
        await update.message.reply_text("Опросы не найдены в базе данных")
        return

    # Вычисляем номер прохождения
    run_id = get_user_runs(user_id)

    # Сохраняем контекст
    context.user_data['polls'] = polls
    context.user_data['run_id'] = run_id
    context.user_data['current_poll_index'] = 0

    await update.message.reply_text(f"Прохождение #{run_id}. Начинаем!")

    logger.info(f"User {user_id} started run #{run_id} from first poll")

    # Запускаем первый опрос
    await send_poll(update.message.chat_id, 0, context.bot, polls)


async def send_poll(chat_id: int, poll_index: int, bot, polls: list) -> None:
    """Отправляет опрос по индексу"""
    if poll_index >= len(polls):
        # Все опросы пройдены
        keyboard = [[InlineKeyboardButton("📊 Статистика", callback_data='stats')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await bot.send_message(
            chat_id=chat_id,
            text="Опрос пройден успешно!",
            reply_markup=reply_markup
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

    logger.info(f"Sent poll {poll_index}: {poll_data['question']}")


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает ответы на опросы"""
    poll_answer = update.poll_answer
    real_poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id

    poll_info = poll_id_mapping.get(real_poll_id)
    if poll_info is None:
        logger.warning(f"Unknown poll_id: {real_poll_id}")
        return

    db_id = poll_info["db_id"]
    poll_index = poll_info["index"]
    run_id = context.user_data.get('run_id', 1)

    # Сохраняем ответ в БД
    for option in poll_answer.option_ids:
        save_answer(db_id, user_id, option, run_id)

    logger.info(f"User {user_id} voted on poll {db_id}, run #{run_id}")

    # Переходим к следующему опросу
    polls = context.user_data.get('polls', [])
    next_index = poll_index + 1
    context.user_data['current_poll_index'] = next_index

    chat_id = update.poll_answer.user.id
    await send_poll(chat_id, next_index, context.bot, polls)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать статистику всех опросов (все прохождения)"""
    polls = get_polls()

    text = "📊 **Статистика опросов**\n\n"

    for i, poll_data in enumerate(polls):
        stats = get_poll_stats(poll_data["id"])
        text += f"**{i+1}. {poll_data['question']}**\n"
        text += "| Вариант | Голосов |\n"
        text += "|---------|---------|\n"

        total_votes = 0
        for j, option in enumerate(poll_data["options"]):
            count = stats.get(j, 0)
            total_votes += count
            text += f"| {option} | {count} |\n"

        text += f"**Всего голосов: {total_votes}**\n\n"

    await update.message.reply_text(text, parse_mode='Markdown')


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на кнопки"""
    query = update.callback_query
    await query.answer()

    if query.data == 'stats':
        polls = get_polls()

        text = "📊 **Статистика опросов**\n\n"

        for i, poll_data in enumerate(polls):
            stats = get_poll_stats(poll_data["id"])
            text += f"**{i+1}. {poll_data['question']}**\n"
            text += "| Вариант | Голосов |\n"
            text += "|---------|---------|\n"

            total_votes = 0
            for j, option in enumerate(poll_data["options"]):
                count = stats.get(j, 0)
                total_votes += count
                text += f"| {option} | {count} |\n"

            text += f"**Всего голосов: {total_votes}**\n\n"

        await query.edit_message_text(text=text, parse_mode='Markdown')


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
    logger.info(f"Added poll: {question}")


def clear_all_answers():
    """Очистить все ответы (для тестирования)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM answers')
    conn.commit()
    conn.close()


def main():
    # Инициализируем БД
    init_db()

    # Если опросов нет, добавляем примеры
    polls = get_polls()
    if not polls:
        add_poll("Какая ваша любимая еда?", ["Пицца", "Суши", "Бургеры", "Салат"])
        add_poll("Как вы оцениваете сервис?", ["Отлично", "Хорошо", "Удовлетворительно", "Плохо"])
        logger.info("Created default polls")

    TOKEN = os.getenv('BOT_TOKEN')

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
