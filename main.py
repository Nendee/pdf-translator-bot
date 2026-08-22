import os
import asyncio
import urllib.request
import fitz  # PyMuPDF
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai
from deep_translator import GoogleTranslator

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8841505744:AAE410CMsOjBneT3uP6XGuJ_vgfjk60I_Lk"
GEMINI_KEYS = [
    "AQ.Ab8RN6JBEgZq9YGr8Q0RD2AmT07C5YrOfZWRdsBDxSE5b-vixw",
]

# --- НАСТРОЙКА КИРИЛЛИЧЕСКОГО ШРИФТА ДЛЯ REPORTLAB ---
FONT_PATH = "DejaVuSans.ttf"
if not os.path.exists(FONT_PATH):
    print("[INFO] Скачивание шрифта DejaVuSans для поддержки кириллицы...")
    font_url = "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf"
    urllib.request.urlretrieve(font_url, FONT_PATH)

pdfmetrics.registerFont(TTFont('DejaVuSans', FONT_PATH))

# --- СОСТОЯНИЯ (FSM) ---
class TranslateState(StatesGroup):
    waiting_for_lang = State()
    waiting_for_file = State()

# --- КАCКАДНЫЙ ПЕРЕВОДЧИК ---
class CascadingTranslator:
    def __init__(self, gemini_keys: list[str]):
        self.gemini_clients = [genai.Client(api_key=key) for key in gemini_keys]

    async def translate_text(self, text: str, target_lang_name: str, target_lang_code: str) -> str:
        prompt = (
            f"Переведи следующий текст полностью на {target_lang_name} язык.\n"
            f"КРИТИЧЕСКИ ВАЖНО: Сохраняй структуру абзацев и разделение на строки.\n"
            f"Переведи абсолютно весь текст и не добавляй никаких пояснений от себя.\n\n"
            f"{text}"
        )

        # 1. Gemini
        for i, client in enumerate(self.gemini_clients):
            try:
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt
                )
                if response.text:
                    cleaned = response.text.strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned.split("\n", 1)[-1]
                    if cleaned.endswith("```"):
                        cleaned = cleaned.rsplit("```", 1)[0]
                    return cleaned.strip()
            except Exception as e:
                print(f"[Предупреждение] Gemini ключ #{i+1} ошибка: {e}")
                continue

        # 2. Резервный переводчик (Google Translate)
        print("[Инфо] Переключение на Google Translate...")
        translator = GoogleTranslator(source='auto', target=target_lang_code)
        
        chunks = [text[i:i+3000] for i in range(0, len(text), 3000)]
        translated_chunks = []
        for chunk in chunks:
            translated = translator.translate(chunk)
            translated_chunks.append(translated)
            await asyncio.sleep(0.3)
        return "\n".join(translated_chunks)

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

# --- ФУНКЦИИ ОБРАБОТКИ И СБОРКИ PDF ---
def pdf_to_text(input_path: str) -> str:
    """Извлекает текст из PDF с сохранением форматирования абзацев"""
    doc = fitz.open(input_path)
    full_text = ""
    for page in doc:
        full_text += page.get_text("text") + "\n\n"
    return full_text

def text_to_pdf(text_content: str, output_path: str):
    """Генерирует PDF-файл с поддержкой кириллицы через DejaVuSans"""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=40, leftMargin=40,
        topMargin=40, bottomMargin=40
    )
    
    styles = getSampleStyleSheet()
    normal_style = ParagraphStyle(
        'CustomNormal',
        parent=styles['Normal'],
        fontName='DejaVuSans',
        fontSize=10,
        leading=14,
        spaceAfter=8
    )
    
    story = []
    paragraphs = text_content.split('\n\n')
    for p in paragraphs:
        clean_p = p.replace('\n', ' ').strip()
        if clean_p:
            safe_text = clean_p.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            story.append(Paragraph(safe_text, normal_style))
            story.append(Spacer(1, 4))
            
    doc.build(story)

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

    status_msg = await message.answer("📥 Файл получен, считываю текст...")
    input_pdf = f"input_{message.from_user.id}.pdf"
    output_pdf = f"translated_{message.from_user.id}.pdf"

    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, input_pdf)
        
        await status_msg.edit_text("🔄 Перевожу документ...")
        
        # 1. Извлекаем текст
        text_data = pdf_to_text(input_pdf)
        
        if not text_data.strip():
            await status_msg.edit_text("❌ Не удалось извлечь текст из PDF (возможно, это сканированные изображения).")
            return

        # 2. Переводим текст
        translated_text = await translator.translate_text(text_data, lang_name, lang_code)

        await status_msg.edit_text("⚙️ Собираю итоговый PDF-документ...")
        
        # 3. Собираем PDF с кириллическим шрифтом
        text_to_pdf(translated_text, output_pdf)

        doc_file = types.FSInputFile(output_pdf)
        await message.answer_document(doc_file, caption=f"✅ Готово! Документ переведен на {lang_name} язык.")
        await status_msg.delete()

    except Exception as e:
        print(f"[ОШИБКА] {e}")
        await message.answer(f"❌ Ошибка при обработке: {e}")
    
    finally:
        if os.path.exists(input_pdf): 
            os.remove(input_pdf)
        if os.path.exists(output_pdf): 
            os.remove(output_pdf)
        
        await state.set_state(TranslateState.waiting_for_lang)
        await message.answer("Хочешь перевести ещё один файл? Выбери язык:", reply_markup=get_lang_keyboard())

# Резервный обработчик
@dp.message(F.document)
async def no_lang_selected(message: types.Message, state: FSMContext):
    await state.set_state(TranslateState.waiting_for_lang)
    await message.answer(
        "Сначала выбери язык, на который нужно перевести файл:",
        reply_markup=get_lang_keyboard()
    )

# --- ЗАПУСК ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    print("[INFO] Бот успешно запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())