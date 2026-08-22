import os
import gc
import asyncio
import urllib.request
import pymupdf as fitz
from google import genai
from deep_translator import GoogleTranslator
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8841505744:AAE410CMsOjBneT3uP6XGuJ_vgfjk60I_Lk")
GEMINI_KEY = os.getenv("GEMINI_KEY", "AQ.Ab8RN6JBEgZq9YGr8Q0RD2AmT07C5YrOfZWRdsBDxSE5b-vixw")

# Инициализация Gemini
client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

FONT_PATH = "DejaVuSans.ttf"

def ensure_font_exists():
    if not os.path.exists(FONT_PATH):
        print("[INFO] Скачивание шрифта DejaVuSans...")
        font_url = "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf"
        urllib.request.urlretrieve(font_url, FONT_PATH)

# --- СОСТОЯНИЯ (FSM) ---
class TranslateState(StatesGroup):
    waiting_for_lang = State()
    waiting_for_file = State()

# --- КАСКАДНЫЙ ПЕРЕВОДЧИК ---
class CascadingTranslator:
    async def translate_batch(self, texts: list[str], target_lang_name: str, target_lang_code: str) -> list[str]:
        if not texts:
            return []
            
        separator = "\n---BLOCK_DELIMITER---\n"
        combined_text = separator.join(texts)
        
        prompt = (
            f"Переведи следующие фрагменты текста на {target_lang_name} язык.\n"
            f"КРИТИЧЕСКИ ВАЖНО: Разделяй фрагменты ровно такой же строкой-разделителем: '---BLOCK_DELIMITER---'.\n"
            f"Не изменяй количество фрагментов и не добавляй от себя никакого текста:\n\n"
            f"{combined_text}"
        )

        # 1. Перевод через Gemini
        if client:
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model='gemini-2.5-flash',
                    contents=prompt
                )
                if response and response.text:
                    cleaned = response.text.strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned.split("\n", 1)[-1]
                    if cleaned.endswith("```"):
                        cleaned = cleaned.rsplit("```", 1)[0]
                    
                    result_blocks = cleaned.strip().split("---BLOCK_DELIMITER---")
                    if len(result_blocks) == len(texts):
                        return [b.strip() for b in result_blocks]
            except Exception as e:
                print(f"[Предупреждение] Ошибка Gemini: {e}")

        # 2. Резервный перевод через GoogleTranslator
        translated_list = []
        try:
            translator = GoogleTranslator(source='auto', target=target_lang_code)
            for t in texts:
                if t.strip():
                    translated = await asyncio.to_thread(translator.translate, t)
                    translated_list.append(translated)
                else:
                    translated_list.append("")
                await asyncio.sleep(0.05)
            return translated_list
        except Exception as e:
            print(f"[Ошибка] GoogleTranslator: {e}")
            return texts

translator = CascadingTranslator()
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

# --- ОБРАБОТКА PDF C СОХРАНЕНИЕМ КАРТИНОК ---
async def process_pdf_in_place(input_path: str, output_path: str, target_lang_name: str, target_lang_code: str):
    ensure_font_exists()
    doc = fitz.open(input_path)

    for page in doc:
        # Регистрируем шрифт на уровне страницы через прямой путь к файлу
        font_ref = page.insert_font(fontname="DejaVu", fontfile=FONT_PATH)
        blocks = page.get_text("blocks")
        
        valid_blocks = []
        texts_to_translate = []
        
        for block in blocks:
            if block[6] == 0:  # Текстовый блок
                text = block[4].strip()
                if text:
                    valid_blocks.append(block)
                    texts_to_translate.append(text)

        if not texts_to_translate:
            continue

        translated_texts = await translator.translate_batch(texts_to_translate, target_lang_name, target_lang_code)

        for block, translated in zip(valid_blocks, translated_texts):
            rect = fitz.Rect(block[:4])
            
            page.add_redact_annot(rect, fill=(1, 1, 1))
            page.apply_redactions(images=0)

            # Передаем fontfile напрямую в insert_textbox
            page.insert_textbox(
                rect, 
                translated, 
                fontname="DejaVu", 
                fontfile=FONT_PATH,
                fontsize=8,
                color=(0, 0, 0)
            )

        gc.collect()

    doc.save(output_path, garbage=4, deflate=True, clean=True)
    doc.close()

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
    if not message.document.file_name.lower().endswith('.pdf'):
        await message.answer("Пожалуйста, отправь файл именно в формате PDF.")
        return

    data = await state.get_data()
    lang_name = data.get("target_lang_name", "русский")
    lang_code = data.get("target_lang_code", "ru")

    status_msg = await message.answer("📥 Файл получен, обрабатываю макет...")
    input_pdf = f"input_{message.from_user.id}.pdf"
    output_pdf = f"translated_{message.from_user.id}.pdf"

    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, input_pdf)
        
        await status_msg.edit_text("🔄 Перевожу текст и сохраняю изображения/таблицы...")
        
        await process_pdf_in_place(input_pdf, output_pdf, lang_name, lang_code)

        doc_file = types.FSInputFile(output_pdf)
        await message.answer_document(doc_file, caption=f"✅ Готово! Перевод на {lang_name} язык с сохранением структуры.")
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

@dp.message(F.document)
async def no_lang_selected(message: types.Message, state: FSMContext):
    await state.set_state(TranslateState.waiting_for_lang)
    await message.answer(
        "Сначала выбери язык, на который нужно перевести файл:",
        reply_markup=get_lang_keyboard()
    )

async def main():
    ensure_font_exists()
    await bot.delete_webhook(drop_pending_updates=True)
    print("[INFO] Бот успешно запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())