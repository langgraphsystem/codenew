#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Telegram Codegen Bot (ЕДИНЫЙ ФАЙЛ, ГОТОВ ДЛЯ RAILWAY)

❖ Назначение
   Телеграм-бот, который принимает ТЕКСТ или .TXT файл с промптом,
   отправляет запрос в OpenAI Responses API и возвращает ПОЛНЫЙ готовый файл
   с кодом. Поддерживает итеративные правки: при следующем промпте бот
   «читает» предыдущую (latest) версию файла и добавляет её в промпт с чёткими
   маркерами, чтобы модель изменила ИМЕННО этот код. Сохраняет версии, шлёт diff.

───────────────────────────────────────────────────────────────────────────────
УСТАНОВКА ЗАВИСИМОСТЕЙ (локально)
    pip install python-telegram-bot==20.7 openai>=1.40.0 python-dotenv>=1.0.1

МИНИМАЛЬНОЕ .env (Railway → Variables)
    TELEGRAM_TOKEN=xxx:yyyyyyyyyyyyyyyyyyyyyyyyyyyy
    OPENAI_API_KEY=sk-................................
    DEFAULT_MODEL=gpt-5                 # (опционально)
    OUTPUT_DIR=/data/out               # (опционально, по умолчанию /data/out)
    OPENAI_REQUEST_TIMEOUT=300         # (опционально, таймаут клиента OpenAI, сек)

СТАРТ (локально)
    TELEGRAM_TOKEN=... OPENAI_API_KEY=... python3 bot.py

ДЕПЛОЙ НА RAILWAY
    • Репозиторий должен содержать этот bot.py и (по желанию) start.sh с:
        #!/usr/bin/env bash
        set -euo pipefail
        python3 bot.py
      (в Railway → Start Command можно указать просто: python3 bot.py)
    • Добавьте переменные в Variables: TELEGRAM_TOKEN, OPENAI_API_KEY (+ доп. при желании).
───────────────────────────────────────────────────────────────────────────────
КАК ПОЛЬЗОВАТЬСЯ
    1) Отправьте боту ТЕКСТ или .TXT с промптом.
       ─ Первая строка может задать имя файла:
            filename: my_app.py
       ─ Вторая/третья строка — подсказка языка:
            language: python
       ─ Можно внизу промпта вложить базовый код в блоке из трёх обратных кавычек:
            ```
            ... код ...
            ```
          Тогда правки будут применены ИМЕННО к этому коду (приоритет над latest).

    2) Бот сгенерирует/обновит файл, сохранит в:
            /data/out/<chat_id>/<timestamp>-<filename>
       и перезапишет latest-<filename>. Пришлёт файл и diff (если было с чем сравнить).

ЛОГИКА ОДНИМ БЛОКОМ
    ✓ Получаем промпт (или читаем .txt).
    ✓ Определяем имя файла и язык (из первых строк).
    ✓ Выбираем базовый код: приоритет — вложенный в промпт блок ```...```,
      иначе latest-<filename> из папки чата (если есть).
    ✓ Собираем «композитный» промпт:
          <ваш текст>
          Применить изменения к коду ниже.
          <<BEGIN_BASE_CODE filename=... version=latest>>
          ```<language>
          ...код...
          ```
          <<END_BASE_CODE>>
    ✓ Вызываем OpenAI Responses API.
    ✓ Сохраняем новую версию и latest, отправляем файл и diff.

ВНИМАНИЕ
    • Модель должна вернуть ТОЛЬКО ПОЛНЫЙ файл внутри fenced-блока ```...```.
    • Таймаут клиента OpenAI задаётся переменной OPENAI_REQUEST_TIMEOUT (по умолчанию 300 cек).
"""

from __future__ import annotations

import os
import re
import time
import difflib
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ─────────── Попытка импортировать OpenAI SDK (v1+) ───────────
try:
    from openai import OpenAI
except ImportError as e:
    raise SystemExit(
        "Не найдены зависимости. Установите:\n"
        "  pip install python-telegram-bot==20.7 openai>=1.40.0 python-dotenv>=1.0.1"
    ) from e


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                         1) ЗАГРУЗКА ОКРУЖЕНИЯ                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DEFAULT_MODEL  = os.getenv("DEFAULT_MODEL", "gpt-5").strip()
OUT_DIR        = Path(os.getenv("OUTPUT_DIR", "/data/out"))
OA_TIMEOUT     = float(os.getenv("OPENAI_REQUEST_TIMEOUT", "300"))  # сек

if not TELEGRAM_TOKEN:
    raise SystemExit("В окружении отсутствует TELEGRAM_TOKEN")
if not OPENAI_API_KEY:
    raise SystemExit("В окружении отсутствует OPENAI_API_KEY")

OUT_DIR.mkdir(parents=True, exist_ok=True)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                   2) НАСТРОЙКИ OPENAI (Responses API)                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝
CLIENT = OpenAI(api_key=OPENAI_API_KEY, timeout=OA_TIMEOUT)

# Жёсткий системный промпт — просим возвращать ТОЛЬКО код в одном fenced-блоке
SYSTEM = (
    "You are a strict, production-grade code generator.\n"
    "Return only the COMPLETE code in a single fenced block. No explanations."
)

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                         3) УТИЛИТЫ/ПАРСИНГ                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# Регэксп для поиска первого блока ```...```
FENCE_RE     = re.compile(r"```[a-zA-Z0-9_+\-]*\n([\s\S]*?)```", re.M)
# Первая строка «filename: my_app.py»
FILENAME_RE  = re.compile(r"^\s*filename\s*:\s*([A-Za-z0-9._/\-]+)\s*$", re.I)
# Подсказка языка: «language: python»
LANG_HINT_RE = re.compile(r"^\s*lang(uage)?\s*:\s*([A-Za-z0-9_\-]+)\s*$", re.I)

def extract_code_block(text: str) -> str:
    """
    Возвращает содержимое первого fenced-блока ```...```, если есть,
    иначе — весь текст. Нужен на ответе модели.
    """
    m = FENCE_RE.search(text or "")
    return (m.group(1) if m else (text or "")).strip()

def parse_prompt(text: str) -> Tuple[str, Optional[str]]:
    """
    Возвращает пару (prompt_text, injected_code_or_None).
    Если пользователь приложил базовый код в НИЖНЕМ блоке ```...```,
    мы выделяем его и удаляем из текста промпта.
    """
    m = FENCE_RE.search(text or "")
    if not m:
        return (text.strip(), None)
    code = (m.group(1) or "").strip()
    prompt = (text[:m.start()] + text[m.end():]).strip()
    return (prompt, code if code else None)

def pick_filename_and_lang(prompt: str) -> Tuple[str, str]:
    """
    Правила выбора:
      • filename: берём из 1-й строки (если есть), иначе code.py
      • language: берём из первых 3 строк (если есть), иначе python
      • грубая эвристика: если внутри текста видим 'javascript' и filename оканчивается на .py,
        меняем на app.js / javascript
    """
    lines = prompt.splitlines()
    filename = "code.py"
    language = "python"

    if lines:
        m = FILENAME_RE.match(lines[0] or "")
        if m:
            filename = m.group(1).strip() or filename

    for i in range(min(3, len(lines))):
        m2 = LANG_HINT_RE.match(lines[i] or "")
        if m2:
            language = (m2.group(2) or "").strip().lower() or language
            break

    if language == "python" and "javascript" in prompt.lower() and filename.endswith(".py"):
        filename = "app.js"
        language = "javascript"

    return filename, language

def chat_dir(chat_id: int) -> Path:
    """Папка текущего чата: /data/out/<chat_id>"""
    d = OUT_DIR / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d

def latest_path(chat_id: int, filename: str) -> Path:
    """Путь к последней версии: /data/out/<chat_id>/latest-<filename>"""
    return chat_dir(chat_id) / f"latest-{Path(filename).name}"

def new_version_path(chat_id: int, filename: str) -> Path:
    """Новая версия с меткой времени: /data/out/<chat_id>/<stamp>-<filename>"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return chat_dir(chat_id) / f"{stamp}-{Path(filename).name}"

def build_composite_prompt(user_prompt: str, language: str, filename: str, base_code: str | None) -> str:
    """
    Сборка ЕДИНОГО «композитного» промпта для LLM.
    Если base_code отсутствует — просим сгенерировать полный файл (create).
    Если base_code есть — просим ОБНОВИТЬ код согласно задаче (edit), добавляя
    чёткие маркеры начала/конца базового кода.
    """
    rules = (
        "Rules:\n"
        "- Return ONLY code in one fenced block ```...```.\n"
        "- Deterministic, self-contained; no external secrets.\n"
        "- If details are missing, choose sensible production defaults.\n"
    )

    if not base_code:
        # Первый заход: генерация «с нуля».
        return f"""Language: {language}
Task: Generate a single, production-ready code file strictly matching the specification.
{rules}
Specification:
{user_prompt}
"""

    # Повторный заход: вносим правки в существующий код.
    return f"""Language: {language}
Task: Update the existing code according to the specification. Produce the FULL UPDATED FILE.
{rules}
Specification:
{user_prompt}

Применить указанные изменения и дополнения к коду ниже.

<<BEGIN_BASE_CODE filename={Path(filename).name} version=latest>>
```{language}
{base_code}

<<END_BASE_CODE>>
“””

def make_diff(before: str, after: str, ext_hint: str) -> str:
“”“Строим unified-diff между базовым и новым кодом (для информирования в чате).”””
return “\n”.join(
difflib.unified_diff(
before.splitlines(),
after.splitlines(),
fromfile=f”before{ext_hint}”,
tofile=f”after{ext_hint}”,
lineterm=””,
)
)

╔══════════════════════════════════════════════════════════════════════════╗

║            4) ВЫЗОВ МОДЕЛИ (Responses API) И СОХРАНЕНИЕ ВЕРСИЙ          ║

╚══════════════════════════════════════════════════════════════════════════╝

async def call_llm(full_prompt: str, model: str) -> str:
“””
Вызов OpenAI Responses API.
Важно: модель должна вернуть ПОЛНЫЙ файл внутри одного fenced-блока.
“””
resp = CLIENT.responses.create(model=model, instructions=SYSTEM, input=full_prompt)
text = getattr(resp, “output_text”, None) or str(resp)
code = extract_code_block(text)
if not code.strip():
raise RuntimeError(“Модель вернула пустой ответ.”)
return code

async def process_any_prompt(chat_id: int, raw_prompt: str, model: str) -> tuple[Path, Optional[str]]:
“””
ЕДИНАЯ функция обработки:
• Распарсить промпт и вытащить вложенный код (если есть).
• Определить имя файла/язык.
• Выбрать базовый код (приоритет — вложенный; иначе latest-).
• Сформировать композитный промпт и вызвать модель.
• Сохранить новую версию и latest.
• Вернуть путь к файлу и diff (если было «с чем» сравнить).
“””
prompt, injected_code = parse_prompt(raw_prompt)
if not prompt:
raise RuntimeError(“Промпт пуст.”)

filename, language = pick_filename_and_lang(prompt)
ext_hint = f".{Path(filename).suffix.lstrip('.') or 'txt'}"

# Источник правок: вложенный код → latest
base_code = injected_code
lp = latest_path(chat_id, filename)
if base_code is None and lp.exists():
    try:
        base_code = lp.read_text(encoding="utf-8")
    except Exception:
        base_code = None

composite = build_composite_prompt(prompt, language, filename, base_code)
code = await call_llm(composite, model=model)

vpath = new_version_path(chat_id, filename)
vpath.write_text(code, encoding="utf-8")
lp.write_text(code, encoding="utf-8")

udiff = None
if base_code:
    try:
        udiff = make_diff(base_code, code, ext_hint)
    except Exception:
        udiff = None

return vpath, udiff

╔══════════════════════════════════════════════════════════════════════════╗

║                          5) TELEGRAM-ОБРАБОТЧИКИ                         ║

╚══════════════════════════════════════════════════════════════════════════╝

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
“Отправьте ТЕКСТ или .TXT — верну ПОЛНЫЙ файл с кодом.\n”
“• Первая строка может задать имя файла:  filename: my_app.py\n”
“• Вторая строка — язык:  language: python\n”
“• Для правок просто пришлите новый промпт — я подложу последнюю версию кода.\n”
“• Можно вложить базовый код в конце промпта в блоке … — правки будут к нему.\n\n”
“/model  — выбрать модель (по умолчанию gpt-5)”
)

async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not ctx.args:
await update.message.reply_text(
f"Текущая модель: {ctx.chat_data.get('model', DEFAULT_MODEL)}\n"
"Использование: /model gpt-5"
)
return
ctx.chat_data[“model”] = “ “.join(ctx.args).strip()
await update.message.reply_text(f”Ок. Модель для этого чата: {ctx.chat_data[‘model’]}”)

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
chat_id = update.effective_chat.id
prompt = (update.message.text or “”).strip()
if not prompt:
await update.message.reply_text(“Промпт пуст. Пришлите текст или .txt.”)
return

model = ctx.chat_data.get("model", DEFAULT_MODEL)
await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

try:
    vpath, udiff = await process_any_prompt(chat_id, prompt, model)
except Exception as e:
    await update.message.reply_text(f"Ошибка генерации: {e}")
    return

# Файл
with vpath.open("rb") as f:
    await update.message.reply_document(
        document=InputFile(f, filename=vpath.name.split("-", 1)[-1]),
        caption="✅ Готово",
    )

# Diff, если был базовый код
if udiff:
    if len(udiff) <= 3500:
        await update.message.reply_text(f"Изменения:\n{udiff[:3900]}")
    else:
        dpath = vpath.with_suffix(".diff.txt")
        dpath.write_text(udiff, encoding="utf-8")
        with dpath.open("rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=dpath.name),
                caption="Изменения (diff)",
            )

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
chat_id = update.effective_chat.id
doc = update.message.document
if not doc:
return

# Принимаем ТОЛЬКО .txt
if not (doc.file_name or "").lower().endswith(".txt"):
    await update.message.reply_text("Пришлите .txt-документ с промптом.")
    return

fobj = await ctx.bot.get_file(doc.file_id)
blob = await fobj.download_as_bytearray()
try:
    prompt = bytes(blob).decode("utf-8")
except UnicodeDecodeError:
    prompt = bytes(blob).decode("utf-8", errors="ignore")

if not prompt.strip():
    await update.message.reply_text("Файл пуст.")
    return

model = ctx.chat_data.get("model", DEFAULT_MODEL)
await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

try:
    vpath, udiff = await process_any_prompt(chat_id, prompt, model)
except Exception as e:
    await update.message.reply_text(f"Ошибка генерации: {e}")
    return

with vpath.open("rb") as f:
    await update.message.reply_document(
        document=InputFile(f, filename=vpath.name.split("-", 1)[-1]),
        caption="✅ Готово",
    )

if udiff:
    if len(udiff) <= 3500:
        await update.message.reply_text(f"Изменения:\n{udiff[:3900]}")
    else:
        dpath = vpath.with_suffix(".diff.txt")
        dpath.write_text(udiff, encoding="utf-8")
        with dpath.open("rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=dpath.name),
                caption="Изменения (diff)",
            )

╔══════════════════════════════════════════════════════════════════════════╗

║                               6) MAIN                                    ║

╚══════════════════════════════════════════════════════════════════════════╝

def main():
app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler(“start”, cmd_start))
app.add_handler(CommandHandler(“help”,  cmd_start))
app.add_handler(CommandHandler(“model”, cmd_model))
app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,     handle_text))
app.run_polling(allowed_updates=Update.ALL_TYPES)

if name == “main”:
main()

