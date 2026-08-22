import os
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai
from deep_translator import GoogleTranslator
import pypdf
from fpdf import FPDF

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8841505744:AAE410CMsOjBneT3uP6XGuJ_vgfjk60I_Lk"
GEMINI_KEYS = [
    "AQ.Ab8RN6JBEgZq9YGr8Q0RD2AmT07C5YrOfZWRdsBDxSE5b-vixw",
]

# --- СОСТОЯНИЯ (FSM) ---
class TranslateState(StatesGroup):
    waiting_for_lang = State()
    waiting_for_file = State()

# --- КАCКАДНЫЙ ПЕРЕВОДЧИК ---
class CascadingTranslator:
    def __init__(self, gemini_keys: list[str]):
        self.gemini_clients = [genai.Client(api_key=key) for key in gemini_keys]

    async def translate(self, text: str, target_lang_name: str, target_lang_code: str) -> str:
        # Промпт принудительно заставляет переводить абсолютно весь текст
        prompt = (
            f"Переведи весь следующий текст полностью на {target_lang_name} язык. "
            f"Если в тексте встречаются разные языки (например, смесь английского и русского), всё равно переведи КАЖДОЕ слово и предложение на {target_lang_name}. "
            f"Сохраняй исходную структуру и форматирование абзацев:\n\n{text}"
        )
        
        # 1. Пробуем ключи Gemini
        for i, client in enumerate(self.gemini_clients):
            try:
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt
                )
                if response.text:
                    return response.text
            except Exception as e:
                print(f"[Предупреждение] Gemini ключ #{i+1} ошибка/лимит: {e}")
                continue

        # 2. Резервный переводчик (Google Translate fallback)
        print("[Инфо] Переключение на Google Translate...")
        google_translator = GoogleTranslator(source='auto', target=target_lang_code)
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        translated_chunks = []
        for chunk in chunks:
            translated = google_translator.translate(chunk)
            translated_chunks.append(translated)
            await asyncio.sleep(0.5)
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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def extract_text_from_pdf(file_path: str) -> str:
    reader = pypdf.PdfReader(file_path)
    text = ""
    for page in reader.pages:
        extracted = page.extract_text()
        if extracted:
            text += extracted + "\n"
    return text

def create_pdf(text: str, output_path: str):
    pdf = FPDF()
    pdf.add_page()
    pdf.add_font('DejaVu', '', 'DejaVuSans.ttf', uni=True)
    pdf.set_font('DejaVu', size=11)
    clean_text = text.encode('utf-8', 'replace').decode('utf-8')
    pdf.multi_cell(0, 8, txt=clean_text)
    pdf.output(output_path)

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ С ИНТЕРАКТИВОМ ---
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
        f"Отлично! Выбран язык перевода: **{lang_name.capitalize()}**.\n\nТеперь отправь мне PDF-файл.",
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

    status_msg = await message.answer("Файл получен, начинаю обработку...")
    input_pdf = f"input_{message.from_user.id}.pdf"
    output_pdf = f"translated_{message.from_user.id}.pdf"

    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, input_pdf)
        
        await status_msg.edit_text("Извлекаю текст и перевожу...")
        text = extract_text_from_pdf(input_pdf)
        
        if not text.strip():
            await status_msg.edit_text("Не удалось прочитать текст из PDF (возможно, это сканированные картинки).")
            return

        translated_text = await translator.translate(text, lang_name, lang_code)

        await status_msg.edit_text("Генерирую новый PDF...")
        create_pdf(translated_text, output_pdf)

        doc_file = types.FSInputFile(output_pdf)
        await message.answer_document(doc_file, caption=f"Готово! Переведено на {lang_name} язык.")
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"Произошла ошибка при обработке: {e}")
    
    finally:
        if os.path.exists(input_pdf): os.remove(input_pdf)
        if os.path.exists(output_pdf): os.remove(output_pdf)
        # Возвращаем меню выбора языка для следующего документа
        await state.set_state(TranslateState.waiting_for_lang)
        await message.answer("Хочешь перевести ещё один файл? Выбери язык:", reply_markup=get_lang_keyboard())

# Резервный обработчик, если пользователь скинул файл без выбора языка
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