import os
import asyncio
import time
import requests
import logging
import trafilatura
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# 1. Настройка логирования (чтобы в терминале выводились системные сообщения с датой и временем)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

# 2. Получение токенов из переменных окружения (безопасно!)
# Если переменные не переданы через Docker/систему, подставится пустая строка или None
TOKEN_TELEGRAM = os.getenv("TELEGRAM_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")

# API модели для суммаризации (Hugging Face Inference Router)
API_SUMMARY = "https://router.huggingface.co/hf-inference/models/facebook/bart-large-cnn"

# Словарь языков для перевода саммари
API_TRANSLATE = {
    "en": None,  # Английский оставляем как есть (оригинал)
    "de": "https://router.huggingface.co/hf-inference/models/Helsinki-NLP/opus-mt-en-de",
    "ru": "https://router.huggingface.co/hf-inference/models/Helsinki-NLP/opus-mt-en-ru",
}
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}


def get_text_from_url_sync(url: str) -> str | None:
    """Скачивает страницу по ссылке и вытаскивает оттуда чистый текст статьи."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        return trafilatura.extract(downloaded)
    except Exception as e:
        logging.error(f"Extraction error: {e}")
        return None


def _post_with_retry(url: str, payload: dict, retries: int = 3, delay: float = 3.0) -> dict:
    """Отправляет запрос к Hugging Face с защитой от ошибки 503 (когда модель 'просыпается' на сервере)."""
    last_error = None
    for attempt in range(retries):
        response = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        if response.status_code == 503:
            last_error = "model loading (503)"
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"HF API failed after {retries} attempts: {last_error}")


def summarize_with_api_sync(text: str) -> str:
    """Отправляет текст в модель суммаризации и возвращает краткую выжимку."""
    if not HF_TOKEN:
        raise ValueError("HF_TOKEN is not set.")

    payload = {"inputs": text[:3000], "parameters": {"min_length": 40, "max_length": 140}}
    result = _post_with_retry(API_SUMMARY, payload)

    if isinstance(result, list) and len(result) > 0:
        return result[0].get("summary_text", "")

    raise RuntimeError(f"Unexpected API response format: {result}")


def translate_with_api_sync(text: str, lang: str) -> str:
    """Переводит готовое суммаризикацию на выбранный язык (немецкий или русский)."""
    if not HF_TOKEN:
        raise ValueError("HF_TOKEN is not set.")

    if lang == "en" or lang not in API_TRANSLATE or API_TRANSLATE[lang] is None:
        return text

    payload = {"inputs": text}
    result = _post_with_retry(API_TRANSLATE[lang], payload)

    if isinstance(result, list) and len(result) > 0:
        return result[0].get("translation_text", "")

    raise RuntimeError(f"Unexpected translation response format: {result}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    await update.message.reply_text(
        "Hello! Send me an article link — I will create an AI summary and translate it."
    )


async def summarize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик обычных сообщений: ждет ссылку, парсит, суммаризирует и выдает кнопки языков."""
    url = update.message.text

    if not url.startswith("http"):
        await update.message.reply_text(
            "Please send a valid link starting with http:// or https://"
        )
        return

    await update.message.reply_text(
        "⏳ Downloading the article text and processing it with AI..."
    )

    try:
        loop = asyncio.get_event_loop()

        # 1. Скачиваем текст (синхронную функцию запускаем в отдельном потоке, чтобы не вешать бота)
        article_text = await loop.run_in_executor(None, get_text_from_url_sync, url)

        if not article_text:
            await update.message.reply_text(
                "Could not extract the text from the page. "
                "The website may be protected against scraping."
            )
            return

        # 2. Получаем саммари от ИИ
        summary = await loop.run_in_executor(None, summarize_with_api_sync, article_text)

        # Сохраняем текст в сессию пользователя, чтобы потом перевести по клику на кнопку
        context.user_data["summary"] = summary

        # Создаем инлайн-клавиатуру с выбором языка
        keyboard = [
            [InlineKeyboardButton("English 🇬🇧", callback_data="en")],
            [InlineKeyboardButton("Deutsch 🇩🇪", callback_data="de")],
            [InlineKeyboardButton("Русский 🇷🇺", callback_data="ru")],
        ]

        await update.message.reply_text(
            f"✅ Summary is ready (in English):\n\n"
            f"{summary}\n\n"
            f"Which language would you like to see the final version in?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logging.exception("summarize failed")
        await update.message.reply_text(
            f"An error occurred while processing the article: {e}"
        )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на инлайн-кнопки (выбор языка перевода)."""
    query = update.callback_query
    await query.answer()

    lang = query.data
    summary = context.user_data.get("summary")

    if not summary:
        await query.edit_message_text(
            "❌ No text available for translation. Please send the link again."
        )
        return

    await query.edit_message_text("🔄 Translating...")

    try:
        loop = asyncio.get_event_loop()
        # Переводим текст через API
        translated = await loop.run_in_executor(None, translate_with_api_sync, summary, lang)

        await query.edit_message_text(
            f"📌 Final version ({lang.upper()}):\n\n{translated}"
        )

    except Exception as e:
        logging.exception("translate failed")
        await query.edit_message_text(
            f"Translation error: {e}"
        )


def main():
    """Главная точка входа для запуска бота в режиме Polling."""
    if not TOKEN_TELEGRAM:
        logging.error("TELEGRAM_TOKEN is not set.")
        exit(1)

    if not HF_TOKEN:
        logging.error("HF_TOKEN is not set.")
        exit(1)

    app = Application.builder().token(TOKEN_TELEGRAM).build()

    # Регистрация хэндлеров (маршрутизаторов событий)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, summarize))
    app.add_handler(CallbackQueryHandler(button))

    logging.info("Polling-mode (local/docker) started.")
    app.run_polling()


if __name__ == "__main__":
    main()