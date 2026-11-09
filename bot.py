import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}

# Дни недели, которые пропускаем: 5=сб, 6=вс. По умолчанию только вс.
SKIP_WEEKDAYS = {
    int(x) for x in os.getenv("SKIP_WEEKDAYS", "6").split(",") if x.strip().isdigit()
}
ANCHOR = date(2024, 1, 1)  # Пн, точка отсчёта ротации

DATA_FILE = Path("data.json")
_lock = asyncio.Lock()

WEEKDAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]
MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


def fmt_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}, {WEEKDAYS[d.weekday()]}"


def is_working_day(d: date) -> bool:
    return d.weekday() not in SKIP_WEEKDAYS


def count_workdays(start: date, end: date) -> int:
    """Сколько не-выходных дней в интервале [start, end)."""
    if end <= start:
        return 0
    total = (end - start).days
    per_week = 7 - len(SKIP_WEEKDAYS)
    count = (total // 7) * per_week
    rest = total % 7
    cursor = end - timedelta(days=rest)
    for i in range(rest):
        if (cursor + timedelta(days=i)).weekday() not in SKIP_WEEKDAYS:
            count += 1
    return count


# ---------- storage ----------
def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"queue": [], "absences": {}}


def save_data(data):
    cutoff = date.today() - timedelta(days=7)
    data["absences"] = {
        k: v for k, v in data["absences"].items() if date.fromisoformat(k) >= cutoff
    }
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_duty(data):
    """(дежурный, индекс) с учётом выходных и отсутствий на сегодня."""
    queue = data["queue"]
    if not queue:
        return None, -1
    today = date.today()
    if not is_working_day(today):
        return None, -1
    absent = set(data["absences"].get(str(today), []))
    base = count_workdays(ANCHOR, today) % len(queue)
    for i in range(len(queue)):
        idx = (base + i) % len(queue)
        if queue[idx] not in absent:
            return queue[idx], idx
    return None, -1


def is_admin(user_id: int) -> bool:
    return not ADMINS or user_id in ADMINS


# ---------- keyboards ----------
def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Кто дежурит?", callback_data="duty:show")],
        [InlineKeyboardButton(text="📋 Вся очередь", callback_data="queue:show")],
    ]
    if is_admin(user_id):
        rows.append(
            [
                InlineKeyboardButton(text="➕ Добавить", callback_data="queue:add"),
                InlineKeyboardButton(text="➖ Убрать", callback_data="queue:remove"),
            ]
        )
    rows.append([InlineKeyboardButton(text="❓ Помощь", callback_data="help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main")]
        ]
    )


# ---------- FSM ----------
class AddPerson(StatesGroup):
    name = State()


# ---------- helpers ----------
async def safe_edit(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def render_duty(data) -> tuple[str, InlineKeyboardMarkup]:
    today = date.today()
    today_str = str(today)
    absent_today = data["absences"].get(today_str, [])

    if not data["queue"]:
        text = (
            "🧹 <b>Дежурный</b>\n\n"
            "Очередь пока пуста 🤷\n"
            "Добавьте участников кнопкой <b>«➕ Добавить»</b>."
        )
        return text, InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить", callback_data="queue:add")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main")],
            ]
        )

    if not is_working_day(today):
        text = (
            f"🏖 <b>Сегодня выходной!</b>\n\n"
            f"📅 {fmt_date(today)}\n\n"
            "Никто не дежурит — все отдыхают 😌"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Вся очередь", callback_data="queue:show"
                    )
                ],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main")],
            ]
        )
        return text, kb

    name, _ = get_duty(data)
    if name is None:
        text = (
            "😴 <b>Все отсутствуют!</b>\n\n"
            "На сегодня не осталось никого в очереди. "
            "Кто-то точно прогуливает 🤨"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="duty:show")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main")],
            ]
        )
        return text, kb

    queue = data["queue"]
    total = len(queue)
    pos = queue.index(name) + 1

    text = (
        f"🧹 <b>Сегодня дежурит:</b>\n\n"
        f"👤 <b>{name}</b>\n\n"
        f"📅 {fmt_date(today)}\n"
        f"📍 В очереди: {pos} из {total}"
    )
    if absent_today:
        text += f"\n😷 Сегодня отсутствуют: {', '.join(absent_today)}"

    rows = []
    if name not in absent_today:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"😷 {name} отсутствует", callback_data="duty:skip"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="duty:show")])
    rows.append([InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def render_queue(data) -> str:
    if not data["queue"]:
        return "📋 <b>Очередь пуста</b>\n\nДобавьте участников через меню."
    today = date.today()
    name, _ = get_duty(data)
    today_absent = set(data["absences"].get(str(today), []))
    lines = ["📋 <b>Очередь</b>\n"]
    if not is_working_day(today):
        lines.append("🏖 Сегодня выходной, дежурных нет\n")
    for i, p in enumerate(data["queue"], 1):
        mark = ""
        if name and p == name:
            mark = " 👈 <b>дежурит</b>"
        elif p in today_absent:
            mark = " <i>(отсутствует)</i>"
        lines.append(f"<code>{i:>2}.</code> {p}{mark}")
    lines.append(f"\n👥 Всего: <b>{len(data['queue'])}</b>")
    return "\n".join(lines)


# ---------- bot setup ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ---------- /start ----------
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    name = message.from_user.first_name or "друг"
    text = (
        f"👋 Привет, <b>{name}</b>!\n\n"
        "Я бот-дежурный. Слежу, чья сегодня очередь наводить порядок, "
        "и помогаю, если кто-то заболел или отлынивает.\n\n"
        "<b>Что умею:</b>\n"
        "• 👤 Показать, кто дежурит сегодня\n"
        "• 😷 Отметить дежурного как отсутствующего\n"
        "• 📋 Показать всю очередь\n"
        "• ➕/➖ Добавлять и убирать участников\n\n"
        f"<i>Ваш ID: <code>{message.from_user.id}</code></i>"
    )
    await message.answer(
        text, reply_markup=main_menu_kb(message.from_user.id), parse_mode="HTML"
    )


# ---------- меню ----------
@dp.callback_query(F.data == "menu:main")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🏠 <b>Главное меню</b>\n\nВыберите действие:"
    await safe_edit(call, text, main_menu_kb(call.from_user.id))
    await call.answer()


@dp.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    text = (
        "❓ <b>Помощь</b>\n\n"
        "<b>👤 Кто дежурит</b> — показывает дежурного на сегодня. "
        "Если кто-то отсутствует — очередь сдвигается на следующего.\n\n"
        "<b>😷 Отсутствует</b> — нажмите, если дежурный заболел. "
        "Он выпадет из очереди до конца дня, а дежурить будет следующий. "
        "На следующий день отметка сбрасывается.\n\n"
        "<b>🏖 Выходные</b> — по воскресеньям дежурных нет, очередь стоит на месте. "
        "В понедельник дежурит следующий по счёту — без сдвигов.\n\n"
        "<b>📋 Вся очередь</b> — кто за кем стоит.\n\n"
        "<b>➕ / ➖</b> — управление участниками (для админов).\n\n"
        "<b>Команды:</b>\n"
        "/start — меню\n"
        "/duty — кто дежурит\n"
        "/queue — вся очередь\n"
        "/add Имя — добавить\n"
        "/remove Имя — убрать\n"
        "/myid — узнать свой ID"
    )
    await safe_edit(call, text, back_kb())
    await call.answer()


# ---------- дежурный ----------
@dp.callback_query(F.data == "duty:show")
async def cb_duty(call: CallbackQuery):
    data = load_data()
    text, kb = render_duty(data)
    await safe_edit(call, text, kb)
    await call.answer()


@dp.callback_query(F.data == "duty:skip")
async def cb_skip(call: CallbackQuery):
    if not is_working_day(date.today()):
        await call.answer("Сегодня выходной — некого отмечать 😌", show_alert=True)
        return

    async with _lock:
        data = load_data()
        name, _ = get_duty(data)
        if name is None:
            await call.answer(
                "Нет дежурного — некому отсутствовать 😅", show_alert=True
            )
            return

        today = str(date.today())
        data["absences"].setdefault(today, []).append(name)
        save_data(data)

        new_name, _ = get_duty(data)

    if new_name is None:
        text = (
            f"😷 <b>{name}</b> отмечен(а) как отсутствующий.\n\n"
            "⚠️ Больше некому дежурить — все разбежались!"
        )
    else:
        text = (
            f"😷 <b>{name}</b> отмечен(а) как отсутствующий.\n\n"
            f"👤 Теперь дежурит: <b>{new_name}</b>"
        )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="duty:show")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main")],
        ]
    )
    await safe_edit(call, text, kb)
    await call.answer("Готово ✅")


# ---------- очередь ----------
@dp.callback_query(F.data == "queue:show")
async def cb_queue(call: CallbackQuery):
    data = load_data()
    await safe_edit(call, render_queue(data), back_kb())
    await call.answer()


# ---------- добавление ----------
@dp.callback_query(F.data == "queue:add")
async def cb_add(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Только админ может добавлять 👮", show_alert=True)
        return

    await state.set_state(AddPerson.name)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ]
    )
    await safe_edit(
        call,
        "✏️ <b>Добавление участника</b>\n\n"
        "Отправьте имя нового человека сообщением:",
        kb,
    )
    await call.answer()


@dp.message(AddPerson.name, F.text)
async def add_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Имя не может быть пустым. Попробуйте снова:")
        return
    if len(name) > 50:
        await message.answer("❌ Слишком длинное имя (макс. 50 символов):")
        return

    async with _lock:
        data = load_data()
        if name in data["queue"]:
            await state.clear()
            await message.answer(
                f"⚠️ <b>{name}</b> уже в очереди",
                reply_markup=main_menu_kb(message.from_user.id),
                parse_mode="HTML",
            )
            return
        data["queue"].append(name)
        save_data(data)

    await state.clear()
    await message.answer(
        f"✅ <b>{name}</b> добавлен(а) в очередь!",
        reply_markup=main_menu_kb(message.from_user.id),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        call,
        "🏠 <b>Главное меню</b>\n\nВыберите действие:",
        main_menu_kb(call.from_user.id),
    )
    await call.answer("Отменено")


# ---------- удаление ----------
@dp.callback_query(F.data == "queue:remove")
async def cb_remove_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Только админ может убирать 👮", show_alert=True)
        return

    data = load_data()
    if not data["queue"]:
        await call.answer("Очередь и так пуста 🤷", show_alert=True)
        return

    rows = [
        [InlineKeyboardButton(text=f"❌ {name}", callback_data=f"rm:{i}")]
        for i, name in enumerate(data["queue"])
    ]
    rows.append([InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_edit(
        call,
        "🗑 <b>Удаление участника</b>\n\n"
        "Нажмите на имя, чтобы убрать его из очереди:",
        kb,
    )
    await call.answer()


@dp.callback_query(F.data.startswith("rm:"))
async def cb_remove_one(call: CallbackQuery):
    idx = int(call.data.split(":", 1)[1])
    async with _lock:
        data = load_data()
        if idx >= len(data["queue"]):
            await call.answer("Уже удалено", show_alert=True)
            return
        name = data["queue"].pop(idx)
        save_data(data)

    await call.answer(f"«{name}» удалён(а)")
    data = load_data()
    await safe_edit(
        call,
        f"✅ <b>{name}</b> убран(а) из очереди.\n\n" + render_queue(data),
        back_kb(),
    )


# ---------- команды-фолбэки ----------
@dp.message(Command("duty"))
async def cmd_duty(message: Message):
    data = load_data()
    text, kb = render_duty(data)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(Command("queue"))
async def cmd_queue(message: Message):
    data = load_data()
    await message.answer(render_queue(data), reply_markup=back_kb(), parse_mode="HTML")


@dp.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может добавлять 👮")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await state.set_state(AddPerson.name)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
            ]
        )
        await message.answer("✏️ Отправьте имя нового человека:", reply_markup=kb)
        return
    name = parts[1].strip()
    async with _lock:
        data = load_data()
        if name in data["queue"]:
            await message.answer(f"⚠️ <b>{name}</b> уже в очереди", parse_mode="HTML")
            return
        data["queue"].append(name)
        save_data(data)
    await message.answer(
        f"✅ <b>{name}</b> добавлен(а)",
        reply_markup=main_menu_kb(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(Command("remove"))
async def cmd_remove(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может убирать 👮")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /remove Имя")
        return
    name = parts[1].strip()
    async with _lock:
        data = load_data()
        if name not in data["queue"]:
            await message.answer(f"⚠️ <b>{name}</b> нет в очереди", parse_mode="HTML")
            return
        data["queue"].remove(name)
        save_data(data)
    await message.answer(
        f"✅ <b>{name}</b> убран(а)",
        reply_markup=main_menu_kb(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(
        f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
        "Скопируйте его и добавьте в <code>ADMINS</code> в файле <code>.env</code>.",
        parse_mode="HTML",
    )


# ---------- запуск ----------
async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN не задан. Проверьте .env")
    print(f"🚀 Бот запущен. Админы: {ADMINS or 'все (не задано)'}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
