import logging
import os
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, PollAnswerHandler, CallbackQueryHandler, ContextTypes

# Включаем логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Список опросов
POLLS = [
    {
        "id": "poll_1",
        "question": "Какая ваша любимая еда?",
        "options": ["Пицца", "Суши", "Бургеры", "Салат"]
    },
    {
        "id": "poll_2",
        "question": "Как вы оцениваете сервис?",
        "options": ["Отлично", "Хорошо", "Удовлетворительно", "Плохо"]
    }
]

# Маппинг реальных poll_id от Telegram к нашим ID и индексам
poll_id_mapping = {}

# Статистика: poll_id -> {option_index: count}
stats = defaultdict(lambda: defaultdict(int))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запуск первого опроса при команде /start"""
    user_id = update.message.from_user.id
    # Показываем первый опрос (индекс 0)
    await send_poll(update.message.chat_id, 0, context.bot)
    # Сохраняем текущий индекс опроса для пользователя
    context.user_data['current_poll_index'] = 0
    logger.info(f"User {user_id} started polls")


async def send_poll(chat_id: int, poll_index: int, bot) -> None:
    """Отправляет опрос по индексу"""
    if poll_index >= len(POLLS):
        # Все опросы пройдены - показываем сообщение с кнопкой статистики
        keyboard = [[InlineKeyboardButton("📊 Статистика", callback_data='stats')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await bot.send_message(
            chat_id=chat_id,
            text="Опрос пройден успешно!",
            reply_markup=reply_markup
        )
        return

    poll_data = POLLS[poll_index]
    sent_poll = await bot.send_poll(
        chat_id=chat_id,
        question=poll_data["question"],
        options=poll_data["options"],
        is_anonymous=False
    )
    # Сохраняем маппинг
    poll_id_mapping[sent_poll.poll.id] = {
        "our_id": poll_data["id"],
        "index": poll_index
    }
    logger.info(f"Sent poll {poll_index}: {poll_data['id']}")


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает ответы на опросы"""
    poll_answer = update.poll_answer
    real_poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id

    # Получаем информацию о poll_id из маппинга
    poll_info = poll_id_mapping.get(real_poll_id)
    if poll_info is None:
        logger.warning(f"Unknown poll_id: {real_poll_id}")
        return

    our_id = poll_info["our_id"]
    poll_index = poll_info["index"]

    # Проверяем, что пользователь ещё не голосовал в этом опросе
    voted_key = f"{user_id}_{our_id}"
    if voted_key in context.bot_data.get('voted', set()):
        return
    if 'voted' not in context.bot_data:
        context.bot_data['voted'] = set()

    # Записываем голоса
    for option in poll_answer.option_ids:
        stats[our_id][option] += 1

    context.bot_data['voted'].add(voted_key)
    logger.info(f"User {user_id} voted on poll {our_id}, options {poll_answer.option_ids}")

    # Получаем текущий индекс опроса для пользователя
    current_index = context.user_data.get('current_poll_index', 0)

    # Проверяем, что это ответ на текущий опрос
    if current_index != poll_index:
        logger.warning(f"User {user_id} answered poll {poll_index} but current is {current_index}")
        return

    # Переходим к следующему опросу
    next_index = current_index + 1
    context.user_data['current_poll_index'] = next_index

    # Отправляем следующий опрос или сообщение о завершении
    chat_id = update.poll_answer.user.id  # Получаем chat_id из ответа
    await send_poll(chat_id, next_index, context.bot)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать статистику всех опросов"""
    text = "📊 **Статистика опросов**\n\n"

    for i, poll_data in enumerate(POLLS):
        text += f"**{i+1}. {poll_data['question']}**\n"
        text += "| Вариант | Голосов |\n"
        text += "|---------|---------|\n"
        for j, option in enumerate(poll_data["options"]):
            count = stats[poll_data["id"]][j]
            text += f"| {option} | {count} |\n"
        text += "\n"

    await update.message.reply_text(text, parse_mode='Markdown')


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на кнопки"""
    query = update.callback_query
    await query.answer()

    if query.data == 'stats':
        text = "📊 **Статистика опросов**\n\n"

        for i, poll_data in enumerate(POLLS):
            text += f"**{i+1}. {poll_data['question']}**\n"
            text += "| Вариант | Голосов |\n"
            text += "|---------|---------|\n"
            for j, option in enumerate(poll_data["options"]):
                count = stats[poll_data["id"]][j]
                text += f"| {option} | {count} |\n"
            text += "\n"

        await query.edit_message_text(text=text, parse_mode='Markdown')


def main() -> None:
    TOKEN = os.getenv('BOT_TOKEN')

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
