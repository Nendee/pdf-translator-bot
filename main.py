import os
import asyncio
import fitz  # PyMuPDF
from xhtml2pdf import pisa
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai
from deep_translator import GoogleTranslator

# --- НАСТРОЙКИ ---
BOT_TOKEN = "ВАШ_BOT_TOKEN"
GEMINI_KEYS = [
    "ВАШ_GEMINI_KEY",
]

# --- СОСТОЯНИЯ (FSM) ---
class TranslateState(StatesGroup):
    waiting_for_lang = State()
    waiting_for_file = State()

# --- КАCКАДНЫЙ ПЕРЕВОДЧИК С СОХРАНЕНИЕМ РАЗМЕТКИ ---
class CascadingTranslator:
    def __init__(self, gemini_keys: list[str]):
        self.gemini_clients = [genai.Client(api_key=key) for key in gemini_keys]

    async def translate_html(self, html_content: str, target_lang_name: str, target_lang_code: str) -> str:
        prompt = (
            f"Переведи весь текст в данном HTML-коде на {target_lang_name} язык.\n"
            f"КРИТИЧЕСКИ ВАЖНО: Сохраняй абсолютно все HTML-теги, структуры таблиц, стили, форматирование и блоки нетронутыми!\n"
            f"Переводи ТОЛЬКО отображаемый текст внутри тегов.\n"
            f"Не добавляй никакие пояснения от себя, верни ТОЛЬКО готовый HTML-код.\n\n"
            f"{html_content}"
        )

        # 1. Gemini
        for i, client in enumerate(self.gemini_clients):
            try:
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt
                )
                if response.text:
                    # Очистка от markdown-оберток ```html ... ```
                    cleaned = response.text.strip()
                    if cleaned.startswith("```html"):
                        cleaned = cleaned[7:]
                    if cleaned.startswith("```"):
                        cleaned = cleaned[3:]
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3]
                    return cleaned.strip()
            except Exception as e:
                print(f"[Предупреждение] Gemini ключ #{i+1} ошибка: {e}")
                continue

        # 2. Резервный переводчик (Google Translate)
        print("[Инфо] Переключение на Google Translate...")
        translator = GoogleTranslator(source='auto', target=target_lang_code)
        return translator.translate(html_content)

translator = CascadingTranslator(GEMINI_KEYS)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- КЛАВИАТУРА ВЫБОРА ЯЗЫКА ---
def get_lang_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="🇷🇺 На русский", callback_data="lang_русский_ru"),
            InlineKeyboardButton(text="🇬🇧 На английский", callback_data="lang_английский_en"),
        ],
        [
            InlineKeyboardButton(text="🇵🇱 На польский", callback_data="lang_польский_pl"),
            InlineKeyboardButton(text="🇩🇪 На немецкий", callback_data="lang_немецкий_de"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ФУНКЦИИ ОБРАБОТКИ PDF И HTML ---
def pdf_to_html(input_path: str) -> str:
    """Извлекает макет PDF с сохранением HTML-тегов и стилей"""
    doc = fitz.open(input_path)
    full_html = "<html><head><meta charset='utf-8'></head><body>"
    
    for page in doc:
        full_html += page.get_text("html")
        full_html += "<br style='page-break-after:always;' />"
        
    full_html += "</body></html>"
    return full_html

def html_to_pdf(html_content: str, output_path: str):
    """Генерирует PDF-файл из HTML полностью средствам Python (xhtml2pdf)"""
    with open(output_path, "wb") as pdf_file:
        pisa.CreatePDF(
            src=html_content, 
            dest=pdf_file, 
            encoding='utf-8'
        )

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.set_state(TranslateState.waiting_for_lang)
    await message.answer(
        "Привет! Выбери язык, на который нужно перевести PDF-документ:",
        reply_markup=get_lang_keyboard()
    )

@dp.callback_query(F.data.startswith("lang_"))
async def set_language(call: types.CallbackQuery, state: FSMContext):
    _, lang_name, lang_code = call.data.split("_")
    await state.update_data(target_lang_name=lang_name, target_lang_code=lang_code)
    await state.set_state(TranslateState.waiting_for_file)
    
    await call.message.edit_text(
        f"Выбран язык перевода: **{lang_name.capitalize()}**.\n\nОтправь мне PDF-файл!",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.message(TranslateState.waiting_for_file, F.document)
async def handle_document(message: types.Message, state: FSMContext):
    if not message.document.file_name.endswith('.pdf'):
        await message.answer("Пожалуйста, отправь файл именно в формате PDF.")
        return

    data = await state.get_data()
    lang_name = data.get("target_lang_name", "русский")
    lang_code = data.get("target_lang_code", "ru")

    status_msg = await message.answer("📥 Файл получен, извлекаю макет...")
    input_pdf = f"input_{message.from_user.id}.pdf"
    output_pdf = f"translated_{message.from_user.id}.pdf"

    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, input_pdf)
        
        await status_msg.edit_text("🔄 Перевожу документ с сохранением структуры...")
        
        # 1. Превращаем PDF в HTML
        html_data = pdf_to_html(input_pdf)
        
        # 2. Переводим текст внутри HTML-тегов
        translated_html = await translator.translate_html(html_data, lang_name, lang_code)

        await status_msg.edit_text("⚙️ Собираю итоговый PDF...")
        
        # 3. Собираем переведенный PDF
        html_to_pdf(translated_html, output_pdf)

        doc_file = types.FSInputFile(output_pdf)
        await message.answer_document(doc_file, caption=f"✅ Готово! Документ переведен на {lang_name} язык.")
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ Произошла ошибка при обработке: {e}")
    
    finally:
        if os.path.exists(input_pdf): os.remove(input_pdf)
        if os.path.exists(output_pdf): os.remove(output_pdf)
        
        # Сброс состояния для выбора следующего языка
        await state.set_state(TranslateState.waiting_for_lang)
        await message.answer("Хочешь перевести ещё один файл? Выбери язык:", reply_markup=get_lang_keyboard())

# Резервный обработчик, если файл отправлен без выбора языка
@dp.message(F.document)
async def no_lang_selected(message: types.Message, state: FSMContext):
    await state.set_state(TranslateState.waiting_for_lang)
    await message.answer(
        "Сначала выбери язык, на который нужно перевести файл:",
        reply_markup=get_lang_keyboard()
    )

# --- ЗАПУСК ---
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())