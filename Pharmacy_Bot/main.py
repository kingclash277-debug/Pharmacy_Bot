import asyncio
import aiosqlite
import datetime
import io
import json
import logging
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

openai_import_error = None
try:
    import openai
except Exception as exc:
    openai = None
    openai_import_error = exc
    logger.warning('Failed to import openai: %s', exc)

import database_v2 as db
ADMIN_ID = 6342296339
cooldowns: Dict[str, float] = {}

IDLE = 'IDLE'
AWAITING_MED_NAME = 'AWAITING_MED_NAME'
AWAITING_MED_DOSAGE = 'AWAITING_MED_DOSAGE'
AWAITING_MED_STOCK = 'AWAITING_MED_STOCK'
AWAITING_REMINDER_TIME = 'AWAITING_REMINDER_TIME'
AWAITING_AI_MED_INFO = 'AWAITING_AI_MED_INFO'
PH_WAITING_FOR_ID = 'PH_WAITING_FOR_ID'
PH_WAIT_REFILL = 'PH_WAIT_REFILL'
PH_WAIT_BROADCAST = 'PH_WAIT_BROADCAST'
PH_ADD_MED_NAME = 'PH_ADD_MED_NAME'
PH_ADD_MED_DOSAGE = 'PH_ADD_MED_DOSAGE'
PH_ADD_MED_STOCK = 'PH_ADD_MED_STOCK'
PH_ADD_MED_REMINDER_TIME = 'PH_ADD_MED_REMINDER_TIME'
PH_EDIT_REMINDER_TIME = 'PH_EDIT_REMINDER_TIME'
# Patient edit states
PATIENT_EDIT_DOSAGE = 'PATIENT_EDIT_DOSAGE'
PATIENT_EDIT_STOCK = 'PATIENT_EDIT_STOCK'
PATIENT_EDIT_TIME = 'PATIENT_EDIT_TIME'
# Health tracking states
AWAITING_HEALTH_READING_TYPE = 'AWAITING_HEALTH_READING_TYPE'
AWAITING_HEALTH_READING_VALUE = 'AWAITING_HEALTH_READING_VALUE'
AWAITING_APPOINTMENT_DATE = 'AWAITING_APPOINTMENT_DATE'
AWAITING_APPOINTMENT_DOCTOR = 'AWAITING_APPOINTMENT_DOCTOR'
AWAITING_APPOINTMENT_REASON = 'AWAITING_APPOINTMENT_REASON'

MENU_BUTTONS = {
    'إضافة دواء 💊',
    'أدويتي 📋',
    'الالتزام بالدواء 📊',
    'قراءاتي الصحية 📈',
    'المواعيد الطبية 🗓️',
    'المساعد الطبي AI 🤖',
    'لوحة الصيدلي 👨‍⚕️',
    'إلغاء',
    'cancel',
}
MENU_BUTTONS_REGEX = r"^(إضافة دواء 💊|أدويتي 📋|الالتزام بالدواء 📊|قراءاتي الصحية 📈|المواعيد الطبية 🗓️|المساعد الطبي AI 🤖|لوحة الصيدلي 👨‍⚕️|إلغاء|cancel)$"

DOTENV_PATH = Path(__file__).resolve().parent / '.env'
if DOTENV_PATH.exists():
    load_dotenv(DOTENV_PATH)
else:
    logger.warning('.env file not found at %s', DOTENV_PATH)


def _load_env_value(key: str) -> Optional[str]:
    value = os.getenv(key, '')
    if value:
        return value.strip().strip('"').strip("'")

    if not DOTENV_PATH.exists():
        return None

    try:
        raw = DOTENV_PATH.read_text(encoding='utf-8-sig')
        for line in raw.splitlines():
            if not line or line.strip().startswith('#'):
                continue
            if line.split('=', 1)[0].strip() != key:
                continue
            value = line.split('=', 1)[1].strip()
            return value.strip().strip('"').strip("'")
    except Exception:
        logger.exception('Failed to manually parse %s from .env', key)
    return None


TELEGRAM_TOKEN = _load_env_value('TELEGRAM_TOKEN')
AI_API_KEY = _load_env_value('AI_API_KEY')
GROQ_API_KEY = _load_env_value('GROQ_API_KEY')

client = None
api_key = AI_API_KEY or GROQ_API_KEY
if openai is not None and api_key:
    try:
        client = openai.OpenAI(
            base_url='https://api.groq.com/openai/v1',
            api_key=api_key,
        )
    except Exception as exc:
        logger.warning('Failed to initialize GROQ OpenAI client: %s', exc)
        client = None


def build_main_menu_markup(user_id: Optional[str] = None) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton('إضافة دواء 💊'), KeyboardButton('أدويتي 📋')],
        [KeyboardButton('الالتزام بالدواء 📊'), KeyboardButton('قراءاتي الصحية 📈')],
        [KeyboardButton('المواعيد الطبية 🗓️'), KeyboardButton('المساعد الطبي AI 🤖')],
    ]
    if user_id and str(user_id) == str(ADMIN_ID):
        buttons.append([KeyboardButton('لوحة الصيدلي 👨‍⚕️'), KeyboardButton('إلغاء')])
    else:
        buttons.append([KeyboardButton('إلغاء')])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)


def build_cancel_markup() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton('إلغاء')]], resize_keyboard=True, one_time_keyboard=True)


def _normalize_text(text: Optional[str]) -> str:
    return text.strip() if text else ''


async def safe_reply(update: Update, text: str, reply_markup: Optional[Any] = None) -> None:
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)


async def enforce_rate_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    now = time.monotonic()
    user_id = str(update.effective_user.id) if update.effective_user else None
    if not user_id:
        return False

    last = cooldowns.get(user_id, 0.0)
    if now - last < 0.5:
        if update.message:
            await update.message.reply_text('يرجى الانتظار قليلاً قبل إرسال رسالة أخرى.')
        return True

    cooldowns[user_id] = now
    return False


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id) if update.effective_user else None
    await safe_reply(
        update,
        'مرحبًا! اختر خيارًا من القائمة الرئيسية:',
        reply_markup=build_main_menu_markup(user_id),
    )


def parse_time_flexible(time_text: str) -> Optional[datetime.time]:
    normalized = time_text.strip().lower()
    normalized = normalized.replace('ص', 'am').replace('م', 'pm')
    normalized = normalized.replace('مساء', 'pm').replace('صباحاً', 'am').replace('صباح', 'am')
    normalized = normalized.replace('مساءً', 'pm').replace('am.', 'am').replace('pm.', 'pm')
    normalized = normalized.replace(' ', '')

    formats = [
        '%I:%M%p',
        '%I%p',
        '%H:%M',
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(normalized, fmt).time()
        except ValueError:
            continue

    if re.match(r'^\d{1,2}:\d{2}$', normalized):
        try:
            return datetime.datetime.strptime(normalized, '%H:%M').time()
        except ValueError:
            return None

    return None


APP_TIMEZONE = ZoneInfo('Asia/Baghdad')

def parse_time_list(time_text: str) -> List[datetime.time]:
    """Parse one or more reminder times separated by commas or newlines."""
    times: List[datetime.time] = []
    for part in re.split(r'[\n,;،]+', time_text):
        parsed = parse_time_flexible(part)
        if parsed:
            times.append(parsed)
    return times


async def analyze_and_add_med_by_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id) if update.effective_user else None
    if not user_id or not update.message or not update.message.text:
        return

    await update.message.reply_text('🤖 جاري تحليل النص وجدولة الدواء تلقائياً... ⏳')

    if not AI_API_KEY:
        await safe_reply(
            update,
            'مفتاح AI_API_KEY غير مكوّن. يرجى التحقق من ملف البيئة.',
            reply_markup=build_main_menu_markup(user_id),
        )
        await reset_user_flow(user_id)
        return

    system_prompt = '''
أنت مساعد صيدلاني ذكي. مهمتك هي قراءة النص الذي يكتبه المريض واستخراج معلومات الدواء منه.
يجب أن تعيد النتيجة بصيغة JSON فقط، بدون أي مقدمات أو مؤخرات أو علامات اقتباس خارجية.

شكل الـ JSON المطلوب:
{
    "med_name": "اسم الدواء بالإنجليزية أو العربية",
    "dosage": "الجرعة مثلاً: حبة واحدة، ملعقتين، إلخ",
    "stock": 30,
    "time": "HH:MM"
}
'''

    try:
        url = 'https://api.groq.com/openai/v1/chat/completions'
        headers = {
            'Authorization': f'Bearer {AI_API_KEY}',
            'Content-Type': 'application/json',
        }
        payload = {
            'model': 'llama-3.3-70b-versatile',
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': update.message.text},
            ],
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()

        raw_content = ''
        if result.get('choices'):
            message = result['choices'][0].get('message', {})
            raw_content = message.get('content', '') if isinstance(message, dict) else ''

        raw_content = raw_content.strip()
        raw_content = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_content, flags=re.IGNORECASE)
        ai_data = json.loads(raw_content)

        med_name = ai_data.get('med_name', '').strip() or 'دواء غير مسمى'
        dosage = ai_data.get('dosage', '').strip() or 'جرعة اعتيادية'
        stock = ai_data.get('stock', 30)
        try:
            stock = int(stock)
        except (TypeError, ValueError):
            stock = 30

        time_str = ai_data.get('time', '').strip()
        reminder_time = parse_time_flexible(time_str) if time_str else None
        if not reminder_time:
            reminder_time = datetime.time(9, 0)
            time_str = '09:00'

        if not med_name or not dosage:
            raise ValueError('Incomplete medication information')

        medicine_id = await db.add_medicine(user_id, med_name, dosage, stock, refill_threshold=2)
        reminder_id = await db.add_reminder(medicine_id, time_str)
        await schedule_reminder_job(context.application, user_id, medicine_id, reminder_id, reminder_time)
        await reset_user_flow(user_id)

        report_msg = (
            f'✅ تم فهم الجرعة وإضافتها بنجاح عبر الذكاء الاصطناعي!\n\n'
            f'💊 الدواء: {med_name}\n'
            f'📐 الجرعة: {dosage}\n'
            f'📦 المخزون: {stock} حبة\n'
            f'⏰ وقت التذكير اليومي: {time_str}'
        )
        await update.message.reply_text(report_msg, reply_markup=build_main_menu_markup(user_id))
        
        # تحقق من التداخلات الدوائية
        await show_drug_interactions_warning(user_id, med_name, update, context)

    except json.JSONDecodeError:
        logger.error('AI Auto-Add JSON parse failed for user %s', user_id, exc_info=True)
        await safe_reply(
            update,
            '❌ لم يتمكن الذكاء الاصطناعي من استخراج بيانات صالحة من النص. حاول إعادة الصياغة أو استخدم الطريقة اليدوية.',
            reply_markup=build_main_menu_markup(user_id),
        )
        await reset_user_flow(user_id)
    except Exception as e:
        logger.error('AI Auto-Add Error: %s', e, exc_info=True)
        await safe_reply(
            update,
            '❌ لم أتمكن من استخراج بيانات الدواء بشكل دقيق، يرجى المحاولة كتابةً بصيغة أوضح أو استخدام الأزرار اليدوية.',
            reply_markup=build_main_menu_markup(user_id),
        )
        await reset_user_flow(user_id)


async def parse_schedule_via_ai(schedule_text: str) -> List[str]:
    if not AI_API_KEY:
        return ['09:00']

    url = 'https://api.groq.com/openai/v1/chat/completions'
    headers = {
        'Authorization': f'Bearer {AI_API_KEY}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': 'llama-3.3-70b-versatile',
        'messages': [
            {
                'role': 'system',
                'content': (
                    'أنت مبرمج نظام طبي ومهمتك تحويل الخيار النصي لجرعة الدواء إلى مواعيد ساعات محددة بصيغة 24 ساعة (HH:MM). '
                    'يجب أن تكون إجابتك عبارة عن مواقيت مفصولة بفاصلة فقط دون أي كلام إضافي تماماً. '
                    'أمثلة للترجمة:\n'
                    '- مرة واحدة - قبل الأكل -> 08:00\n'
                    '- مرة واحدة - بعد الأكل -> 09:30\n'
                    '- مرتين (صباحاً ومساءً) -> 08:00,20:00\n'
                    '- 3 مرات يومياً -> 08:00,14:00,22:00\n'
                    '- عند اللزوم -> 12:00'
                ),
            },
            {'role': 'user', 'content': f'حول هذا الخيار النصي إلى صيغة ساعات فقط: {schedule_text}'},
        ],
        'temperature': 0.1,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, json=payload, headers=headers)
            res.raise_for_status()
            raw_text = res.json()['choices'][0]['message']['content'].strip()
            return [t.strip() for t in raw_text.split(',') if t.strip()]
    except Exception as e:
        logger.error(f'AI parsing error: {e}')
        return ['09:00']


async def ai_chat_assistant(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    try:
        if question == 'المساعد الطبي AI 🤖':
            await update.message.reply_text('أنا صيدلانيك الذكي والمساعد الطبي الخاص بك 🤖. اكتب لي أي سؤال عن أعراضك أو أدويتك وسأجيبك فوراً!')
            return

        if not AI_API_KEY:
            await update.message.reply_text('🤖 ميزة الـ AI تحت الصيانة حالياً.')
            return

        await update.message.reply_chat_action('typing')
        url = 'https://api.groq.com/openai/v1/chat/completions'
        headers = {
            'Authorization': f'Bearer {AI_API_KEY}',
            'Content-Type': 'application/json',
        }
        payload = {
            'model': 'llama-3.3-70b-versatile',
            'messages': [
                {'role': 'system', 'content': 'أنت صيدلاني ذكي محترف. أجب باختصار واختم بعبارة: يرجى مراجعة الصيدلاني أو الطبيب المختص لسلامتك.'},
                {'role': 'user', 'content': question},
            ],
            'temperature': 0.4,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(url, json=payload, headers=headers)
            res.raise_for_status()
            result = res.json()
            ai_response = result['choices'][0]['message']['content']
            await update.message.reply_text(ai_response)
    except Exception as e:
        logger.error(f'CRITICAL ERROR in ai_chat_assistant: {e}', exc_info=True)
        await update.message.reply_text('❌ عذراً، واجهت مشكلة في الاتصال بالسيرفر الطبي.')


async def get_registered_user_ids() -> List[str]:
    try:
        async with aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute('SELECT DISTINCT user_id FROM users') as cursor:
                rows = await cursor.fetchall()
                return [row['user_id'] for row in rows if row['user_id']]
    except Exception:
        logger.exception('Failed to fetch registered user ids')
        return []


async def increment_medicine_stock(med_id: int, amount: int = 30) -> Optional[int]:
    try:
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute('PRAGMA foreign_keys = ON;')
            await conn.execute(
                'UPDATE medicines SET stock_quantity = stock_quantity + ? WHERE id = ?',
                (amount, med_id),
            )
            await conn.commit()
            async with conn.execute('SELECT stock_quantity FROM medicines WHERE id = ?', (med_id,)) as cursor:
                row = await cursor.fetchone()
                return row['stock_quantity'] if row else None
    except Exception:
        logger.exception('Failed to increment stock for medicine %s', med_id)
        return None


async def reset_user_flow(user_id: str) -> None:
    await db.update_user_state(user_id, IDLE)
    await db.clear_user_pending_data(user_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دالة البداية - التسجيل التلقائي للمستخدمين."""
    await db.init_db()
    user = update.effective_user
    if user:
        # قراءة البيانات مباشرة من التليجرام وحفظها تلقائياً
        first_name = user.first_name or ''
        last_name = user.last_name or ''
        full_name = f"{first_name} {last_name}".strip() or "مستخدم جديد"
        username = user.username or f"user_{user.id}"
        
        # حفظ تلقائي في قاعدة البيانات كـ patient أساساً
        role = 'admin' if str(user.id) == str(ADMIN_ID) else 'patient'
        await db.add_user(
            user_id=str(user.id),
            username=username,
            full_name=full_name,
            role=role,
        )
        await db.update_user_state(str(user.id), IDLE)
        await db.clear_user_pending_data(str(user.id))
    await send_main_menu(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id) if update.effective_user else None
    if user_id:
        await reset_user_flow(user_id)
    await send_main_menu(update, context)


async def show_compliance_report(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str) -> None:
    """عرض تقرير الالتزام الأسبوعي والشهري للمريض."""
    try:
        week_report = await db.get_compliance_rate(user_id, days=7)
        month_report = await db.get_compliance_rate(user_id, days=30)
        
        if not week_report.get('total', 0):
            await safe_reply(update, '📋 لم تسجل أي جرعات حتى الآن. ابدأ بإضافة أدويتك!')
            return
        
        week_rate = week_report.get('compliance_rate', 0)
        month_rate = month_report.get('compliance_rate', 0)
        
        msg = '📊 **تقرير الالتزام بالدواء**\n\n'
        msg += f'**الأسبوع الحالي:**\n'
        msg += f'  📈 نسبة الالتزام: {week_rate:.1f}%\n'
        msg += f'  ✅ تم تناول: {week_report.get("taken", 0)}/{week_report.get("total", 0)} جرعة\n'
        msg += f'  ⏭️ تم تجاوز: {week_report.get("skipped", 0)} جرعة\n'
        msg += f'  ❌ تم فقدان: {week_report.get("missed", 0)} جرعة\n\n'
        
        msg += f'**الشهر الحالي:**\n'
        msg += f'  📈 نسبة الالتزام: {month_rate:.1f}%\n'
        msg += f'  ✅ تم تناول: {month_report.get("taken", 0)}/{month_report.get("total", 0)} جرعة\n'
        msg += f'  ⏭️ تم تجاوز: {month_report.get("skipped", 0)} جرعة\n'
        msg += f'  ❌ تم فقدان: {month_report.get("missed", 0)} جرعة\n\n'
        
        if week_rate == 100:
            msg += '🎉 **تهانينا! أنت ملتزم تماماً هذا الأسبوع!**\n'
            msg += 'استمر في الالتزام بجدول الأدوية الخاص بك، صحتك أهمية عندنا! 💚'
        elif week_rate >= 80:
            msg += '👏 **ممتاز! التزامك جيد جداً**\n'
            msg += 'حاول تحسينه أكثر للوصول إلى 100% 💪'
        elif week_rate >= 60:
            msg += '⚠️ **يجب تحسين الالتزام**\n'
            msg += 'حاول عدم نسيان جرعاتك، استعن بالتذكيرات! 🔔'
        else:
            msg += '🆘 **التزامك منخفض جداً**\n'
            msg += 'تأكد من تناول أدويتك بانتظام. صحتك مهمة! ❤️'
        
        await safe_reply(update, msg, reply_markup=build_main_menu_markup(user_id))
    except Exception as e:
        logger.exception('Error in show_compliance_report')
        await safe_reply(update, '❌ حدث خطأ في تحميل التقرير', reply_markup=build_main_menu_markup(user_id))


async def show_health_readings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str) -> None:
    """عرض قائمة القراءات الصحية."""
    try:
        buttons = [
            [KeyboardButton('💓 ضغط الدم'), KeyboardButton('🩸 السكر')],
            [KeyboardButton('⚖️ الوزن'), KeyboardButton('💨 النبض')],
            [KeyboardButton('إلغاء')],
        ]
        markup = ReplyKeyboardMarkup(buttons, resize_keyboard=True)
        await safe_reply(
            update,
            '📈 **اختر نوع القراءة الصحية:**',
            reply_markup=markup
        )
        await db.update_user_state(user_id, AWAITING_HEALTH_READING_TYPE)
    except Exception as e:
        logger.exception('Error in show_health_readings_menu')
        await safe_reply(update, '❌ حدث خطأ', reply_markup=build_main_menu_markup(user_id))


async def show_appointments_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str) -> None:
    """عرض المواعيد الطبية المقبلة."""
    try:
        appointments = await db.get_upcoming_appointments(user_id)
        
        if not appointments:
            await safe_reply(
                update,
                '📅 لا توجد مواعيد طبية قادمة.\n\nاضغط على الزر أدناه لإضافة موعد جديد:',
                reply_markup=ReplyKeyboardMarkup([
                    [KeyboardButton('➕ إضافة موعد جديد')],
                    [KeyboardButton('إلغاء')],
                ], resize_keyboard=True)
            )
            return
        
        msg = '📅 **المواعيد الطبية القادمة:**\n\n'
        for apt in appointments:
            msg += f'🏥 {apt["doctor_name"]}\n'
            msg += f'📍 السبب: {apt["reason"]}\n'
            msg += f'🕐 التاريخ: {apt["appointment_date"]}\n'
            if apt.get('notes'):
                msg += f'📝 ملاحظات: {apt["notes"]}\n'
            msg += '─' * 30 + '\n'
        
        await safe_reply(
            update,
            msg,
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton('➕ إضافة موعد جديد')],
                [KeyboardButton('إلغاء')],
            ], resize_keyboard=True)
        )
    except Exception as e:
        logger.exception('Error in show_appointments_menu')
        await safe_reply(update, '❌ حدث خطأ', reply_markup=build_main_menu_markup(user_id))


async def show_drug_interactions_warning(
    user_id: str,
    medicine_name: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """تحقق من التداخلات الدوائية وأظهر التحذيرات للمريض."""
    try:
        interactions = await db.check_drug_interactions(user_id)
        if not interactions:
            return
        
        # فلترة التداخلات التي تتعلق بالدواء الجديد
        relevant = [
            i for i in interactions
            if medicine_name.lower() in i.get('drug1', '').lower() or medicine_name.lower() in i.get('drug2', '').lower()
        ]
        
        if not relevant:
            return
        
        msg = '⚠️ **تحذير: تداخلات دوائية مكتشفة!**\n\n'
        for inter in relevant:
            severity_icon = '🔴' if inter['severity'] == 'high' else '🟠' if inter['severity'] == 'medium' else '🟡'
            msg += f'{severity_icon} **{inter["drug1"]} + {inter["drug2"]}**\n'
            msg += f'   الخطورة: {inter["severity"]}\n'
            msg += f'   التفاصيل: {inter["description"]}\n\n'
        
        msg += '⚠️ **يرجى استشارة الصيدلي أو الطبيب قبل تناول هذا الدواء**'
        await safe_reply(update, msg, reply_markup=build_main_menu_markup(user_id))
    except Exception as e:
        logger.exception('Error checking drug interactions')


async def setup_startup_jobs(app) -> None:
    """Setup jobs at startup including seeding drug interactions."""
    try:
        # Seed drug interactions once
        await db.seed_drug_interactions()
        logger.info('Drug interactions seeded at startup')
    except Exception:
        logger.exception('Failed to seed drug interactions')


async def route_main_menu_button(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reset_user_flow(user_id)

    if text == 'إضافة دواء 💊':
        await db.update_user_state(user_id, AWAITING_MED_NAME)
        await safe_reply(update, 'أرسل اسم الدواء:', reply_markup=build_cancel_markup())
        return

    if text == 'أدويتي 📋':
        await display_patient_medicines(update, context, user_id, admin_view=False)
        return

    if text == 'الالتزام بالدواء 📊':
        await show_compliance_report(update, context, user_id)
        return

    if text == 'قراءاتي الصحية 📈':
        await show_health_readings_menu(update, context, user_id)
        return

    if text == 'المواعيد الطبية 📅':
        await show_appointments_menu(update, context, user_id)
        return

    if text == 'المساعد الطبي AI 🤖':
        await safe_reply(
            update,
            'أنا مساعدك الطبي الذكي 🤖. اسألني عن الأعراض أو الأدوية أو التفاعلات، وسأجيبك مباشرةً.',
            reply_markup=build_cancel_markup(),
        )
        return

    if text == 'تغيير اللغة 🌐':
        await safe_reply(update, 'يمكنك تغيير اللغة لاحقًا. هذه الميزة ستدعمها قريبًا.', reply_markup=build_main_menu_markup(user_id))
        return

    if text == 'لوحة الصيدلي 👨‍⚕️':
        if str(user_id) != str(ADMIN_ID):
            await safe_reply(update, 'عذراً، هذه الخاصية محجوزة للصيدلي فقط.', reply_markup=build_main_menu_markup(user_id))
            return
        await show_pharmacist_dashboard(update, context)
        return

    if text in {'إلغاء', 'cancel'}:
        await send_main_menu(update, context)
        return


async def route_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if await enforce_rate_limit(update, context):
            return

        if not update.message or not update.message.text:
            return

        user = update.effective_user
        if not user:
            return

        user_id = str(user.id)
        # detailed entry log for debugging
        try:
            preview_text = update.message.text[:200] if update.message and update.message.text else ''
        except Exception:
            preview_text = ''
        logger.info('route_user_message enter: user=%s text=%s', user_id, preview_text)
        full_name = ' '.join(filter(None, [user.first_name, user.last_name]))
        await db.add_user(user_id=user_id, username=user.username or '', full_name=full_name)

        text = _normalize_text(update.message.text)
        if text in MENU_BUTTONS and re.fullmatch(MENU_BUTTONS_REGEX, text):
            await route_main_menu_button(user_id, text, update, context)
            return

        current_state = await db.get_user_state(user_id)

        if current_state == AWAITING_MED_NAME:
            await handle_med_name_state(user_id, text, update, context)
            return

        if current_state == AWAITING_MED_DOSAGE:
            await handle_med_dosage_state(user_id, text, update, context)
            return

        if current_state == AWAITING_MED_STOCK:
            await handle_med_stock_state(user_id, text, update, context)
            return

        if current_state == AWAITING_REMINDER_TIME:
            await handle_reminder_time_state(user_id, text, update, context)
            return

        if current_state == AWAITING_AI_MED_INFO:
            await ai_chat_assistant(update, context, text)
            return

        if current_state == PH_WAITING_FOR_ID:
            await handle_pharmacist_patient_id_state(user_id, text, update, context)
            return

        if current_state == PH_WAIT_REFILL:
            await handle_pharmacist_refill_state(user_id, text, update, context)
            return

        if current_state == PH_WAIT_BROADCAST:
            await handle_pharmacist_broadcast_state(user_id, text, update, context)
            return

        if current_state == PH_ADD_MED_NAME:
            await handle_pharmacist_add_med_name_state(user_id, text, update, context)
            return

        if current_state == PH_ADD_MED_DOSAGE:
            await handle_pharmacist_add_med_dosage_state(user_id, text, update, context)
            return

        if current_state == PH_ADD_MED_STOCK:
            await handle_pharmacist_add_med_stock_state(user_id, text, update, context)
            return

        if current_state == PH_ADD_MED_REMINDER_TIME:
            await handle_pharmacist_add_med_reminder_time_state(user_id, text, update, context)
            return

        if current_state == PH_EDIT_REMINDER_TIME:
            await handle_pharmacist_edit_reminder_time_state(user_id, text, update, context)
            return

        # Patient-side edit handlers
        if current_state == PATIENT_EDIT_DOSAGE:
            med_id = context.user_data.get('pending_edit_med_id')
            if not med_id:
                await safe_reply(update, 'لم يتم تحديد الدواء.')
                await db.update_user_state(user_id, IDLE)
                return
            new_dosage = text.strip()
            ok = await db.update_medicine_dosage(int(med_id), new_dosage)
            await db.update_user_state(user_id, IDLE)
            context.user_data.pop('pending_edit_med_id', None)
            if ok:
                await safe_reply(update, '✅ تم تحديث الجرعة بنجاح.', reply_markup=build_main_menu_markup(user_id))
            else:
                await safe_reply(update, '❌ فشل تحديث الجرعة. حاول مرة أخرى.', reply_markup=build_main_menu_markup(user_id))
            await display_patient_medicines(update, context, user_id, admin_view=False)
            return

        if current_state == PATIENT_EDIT_STOCK:
            med_id = context.user_data.get('pending_edit_med_id')
            if not med_id:
                await safe_reply(update, 'لم يتم تحديد الدواء.')
                await db.update_user_state(user_id, IDLE)
                return
            try:
                new_stock = int(text.strip())
            except ValueError:
                await safe_reply(update, 'الرجاء إرسال رقم صالح للكمية.')
                return
            ok = await db.update_medicine_stock(int(med_id), new_stock)
            await db.update_user_state(user_id, IDLE)
            context.user_data.pop('pending_edit_med_id', None)
            if ok:
                await safe_reply(update, '✅ تم تحديث الكمية بنجاح.', reply_markup=build_main_menu_markup(user_id))
            else:
                await safe_reply(update, '❌ فشل تحديث الكمية. حاول مرة أخرى.', reply_markup=build_main_menu_markup(user_id))
            await display_patient_medicines(update, context, user_id, admin_view=False)
            return

        if current_state == PATIENT_EDIT_TIME:
            med_id = context.user_data.get('pending_edit_med_id')
            if not med_id:
                await safe_reply(update, 'لم يتم تحديد الدواء.')
                await db.update_user_state(user_id, IDLE)
                return
            times = parse_time_list(text)
            if not times:
                await safe_reply(update, 'لم يتم التعرف على أي وقت. الرجاء إرسال أوقات بصيغة HH:MM مفصولة بفواصل.')
                return
            # replace reminders in DB and scheduler
            await db.delete_reminders_for_medicine(int(med_id))
            remove_reminder_jobs_for_medicine(context.application, int(med_id))
            scheduled = []
            for t in times:
                rid = await db.add_reminder(int(med_id), t.strftime('%H:%M'))
                schedule_reminder_job(context.application, user_id, int(med_id), rid, t)
                scheduled.append(t.strftime('%H:%M'))
            await db.update_user_state(user_id, IDLE)
            context.user_data.pop('pending_edit_med_id', None)
            await safe_reply(update, f'✅ تم تحديث مواعيد التذكير: {", ".join(scheduled)}', reply_markup=build_main_menu_markup(user_id))
            await display_patient_medicines(update, context, user_id, admin_view=False)
            return

        if current_state == AWAITING_HEALTH_READING_TYPE:
            reading_type = text.strip()
            if reading_type not in ['💓 ضغط الدم', '🩸 السكر', '⚖️ الوزن', '💨 النبض']:
                if reading_type == 'إلغاء':
                    await send_main_menu(update, context)
                    return
                await safe_reply(update, 'اختر نوع القراءة من الخيارات المتاحة.')
                return
            
            context.user_data['reading_type'] = reading_type
            prompt = ''
            if reading_type == '💓 ضغط الدم':
                prompt = 'أدخل قراءة ضغط الدم (مثال: 120/80):'
            elif reading_type == '🩸 السكر':
                prompt = 'أدخل قراءة السكر (مثال: 120 mg/dL):'
            elif reading_type == '⚖️ الوزن':
                prompt = 'أدخل الوزن (مثال: 70 kg):'
            else:  # النبض
                prompt = 'أدخل النبض (مثال: 75 bpm):'
            
            await db.update_user_state(user_id, AWAITING_HEALTH_READING_VALUE)
            await safe_reply(update, prompt, reply_markup=build_cancel_markup())
            return

        if current_state == AWAITING_HEALTH_READING_VALUE:
            reading_type = context.user_data.get('reading_type', '')
            reading_value = text.strip()
            
            # محاولة استخراج القيمة الرقمية
            numbers = re.findall(r'[\d.]+', reading_value)
            if not numbers:
                await safe_reply(update, 'الرجاء إدخال قيمة رقمية صحيحة.', reply_markup=build_cancel_markup())
                return
            
            value = float(numbers[0])
            unit = 'mmHg' if 'ضغط' in reading_type else 'mg/dL' if 'السكر' in reading_type else 'kg' if 'الوزن' in reading_type else 'bpm'
            
            await db.add_health_reading(user_id, reading_type, value, unit, '')
            await db.update_user_state(user_id, IDLE)
            context.user_data.pop('reading_type', None)
            
            await safe_reply(update, f'✅ تم تسجيل القراءة: {reading_type} = {value} {unit}', reply_markup=build_main_menu_markup(user_id))
            return

        await ai_chat_assistant(update, context, text)
    except Exception as e:
        logger.error(f"CRITICAL ERROR in route_user_message: {e}", exc_info=True)
        # try notify admin (controlled by env toggle)
        try:
            notify = str(os.getenv('NOTIFY_ADMIN_ON_EXCEPTIONS', '1')).lower() in ('1', 'true', 'yes')
            if notify and context and context.bot:
                user_info = f'user_id={getattr(update.effective_user, "id", None)} username={getattr(update.effective_user, "username", None)}'
                text_preview = (update.message.text[:500] if update.message and update.message.text else '<no-text>')
                await context.bot.send_message(
                    chat_id=int(ADMIN_ID),
                    text=(
                        f"[Bot Error] Exception processing message\n{user_info}\nmessage_preview: {text_preview}\nerror: {e}"
                    ),
                )
        except Exception:
            logger.exception('Failed to send admin notification about exception')

        await safe_reply(update, 'حدث خطأ غير متوقع أثناء معالجة الرسالة. الرجاء المحاولة مرة أخرى.')


async def handle_med_name_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not text:
        await safe_reply(update, 'الرجاء إرسال اسم الدواء الصحيح.', reply_markup=build_cancel_markup())
        return

    await db.set_user_pending_data(user_id, pending_med_name=text)
    await db.update_user_state(user_id, AWAITING_MED_DOSAGE)
    await safe_reply(update, 'أرسل الجرعة المطلوبة:', reply_markup=build_cancel_markup())


async def handle_med_dosage_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not text:
        await safe_reply(update, 'الرجاء إرسال الجرعة المطلوبة.', reply_markup=build_cancel_markup())
        return

    await db.set_user_pending_data(user_id, pending_med_dosage=text)
    await db.update_user_state(user_id, AWAITING_MED_STOCK)
    await safe_reply(update, 'أرسل كمية المخزون الحالية (رقم):', reply_markup=build_cancel_markup())


async def handle_med_stock_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not text.isdigit():
        await safe_reply(update, 'الرجاء إرسال عدد صالح للمخزون. أرسل رقمًا.', reply_markup=build_cancel_markup())
        return

    await db.set_user_pending_data(user_id, pending_med_stock=int(text))
    await db.update_user_state(user_id, AWAITING_REMINDER_TIME)
    await safe_reply(update, 'أرسل وقت التذكير بصيغة 12 ساعة (مثلاً 08:30 ص أو 10:15 م):', reply_markup=build_cancel_markup())


async def handle_reminder_time_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    reminder_times = parse_time_list(text)
    if not reminder_times:
        await safe_reply(update, 'الوقت غير صالح. استخدم الصيغة 08:30 ص أو 10:15 م أو أوقات متعددة بفواصل.', reply_markup=build_cancel_markup())
        return

    pending = await db.get_user_pending_data(user_id)
    name = pending.get('pending_med_name', '')
    dosage = pending.get('pending_med_dosage', '')
    stock_quantity = int(pending.get('pending_med_stock', 0) or 0)

    if not name or not dosage:
        await reset_user_flow(user_id)
        await safe_reply(
            update,
            'فشل حفظ الدواء. أعد المحاولة من القائمة الرئيسية.',
            reply_markup=build_main_menu_markup(user_id),
        )
        return

    medicine_id = await db.add_medicine(user_id, name, dosage, stock_quantity, refill_threshold=2)
    scheduled_times = []
    for reminder_time in reminder_times:
        reminder_id = await db.add_reminder(medicine_id, reminder_time.strftime('%H:%M'))
        await schedule_reminder_job(context.application, user_id, medicine_id, reminder_id, reminder_time)
        scheduled_times.append(reminder_time.strftime('%H:%M'))

    await reset_user_flow(user_id)

    await safe_reply(
        update,
        f'تمت إضافة الدواء {name} بنجاح مع تذكير عند: {", ".join(scheduled_times)}.',
        reply_markup=build_main_menu_markup(user_id),
    )
    
    # تحقق من التداخلات الدوائية
    await show_drug_interactions_warning(user_id, name, update, context)


async def handle_pharmacist_patient_id_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not text.isdigit():
        await safe_reply(update, 'الرجاء إرسال رقم User ID صالح.', reply_markup=build_cancel_markup())
        return

    patient = await db.get_user(text)
    if not patient:
        await db.update_user_state(user_id, IDLE)
        await safe_reply(update, 'لم يتم العثور على المريض. تأكد من رقم المستخدم وحاول مجدداً.', reply_markup=build_main_menu_markup(user_id))
        return

    await db.update_user_state(user_id, IDLE)
    await safe_reply(update, f'النتائج للمريض: {patient.get("full_name", "-")}', reply_markup=build_main_menu_markup(user_id))
    await display_patient_medicines(update, context, text, admin_view=True, show_refill_buttons=True)


async def handle_pharmacist_refill_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not text.isdigit():
        await safe_reply(update, 'الرجاء إرسال رقم الدواء (ID) صالح لإضافة 30 حبة.', reply_markup=build_cancel_markup())
        return

    med_id = int(text)
    medicine = await db.get_medicine_by_id(med_id)
    if not medicine:
        await safe_reply(update, 'لم يتم العثور على الدواء. تأكد من رقم الدواء وحاول مجدداً.', reply_markup=build_cancel_markup())
        return

    updated_stock = await increment_medicine_stock(med_id, 30)
    await db.update_user_state(user_id, IDLE)
    await safe_reply(
        update,
        f'تمت إضافة 30 حبة إلى {medicine["name"]}. المخزون الجديد: {updated_stock or "غير معروف"} حبة.',
        reply_markup=build_main_menu_markup(user_id),
    )


async def handle_pharmacist_broadcast_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not text:
        await safe_reply(update, 'الرجاء إرسال رسالة للإذاعة.', reply_markup=build_cancel_markup())
        return

    recipients = await get_registered_user_ids()
    sent = 0
    failed = 0
    for recipient_id in recipients:
        if str(recipient_id) == str(ADMIN_ID):
            continue
        try:
            await context.bot.send_message(chat_id=int(recipient_id), text=text)
            sent += 1
        except Exception as exc:
            logger.warning('Broadcast failed for %s: %s', recipient_id, exc)
            failed += 1
            continue

    await db.update_user_state(user_id, IDLE)
    await safe_reply(
        update,
        f'تم إرسال الرسالة إلى {sent} مستخدمين. فشل الإرسال إلى {failed} مستخدمين.',
        reply_markup=build_main_menu_markup(user_id),
    )


async def handle_pharmacist_add_med_name_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not text:
        await safe_reply(update, 'الرجاء إرسال اسم الدواء الصحيح.', reply_markup=build_cancel_markup())
        return

    context.user_data['pending_med_name'] = text
    await db.update_user_state(user_id, PH_ADD_MED_DOSAGE)
    await safe_reply(update, 'أرسل الجرعة المطلوبة:', reply_markup=build_cancel_markup())


async def handle_pharmacist_add_med_dosage_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not text:
        await safe_reply(update, 'الرجاء إرسال الجرعة المطلوبة.', reply_markup=build_cancel_markup())
        return

    context.user_data['pending_med_dosage'] = text
    await db.update_user_state(user_id, PH_ADD_MED_STOCK)
    await safe_reply(update, 'أرسل كمية المخزون الحالية (رقم):', reply_markup=build_cancel_markup())


async def handle_pharmacist_add_med_stock_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not text.isdigit():
        await safe_reply(update, 'الرجاء إرسال عدد صالح للمخزون. أرسل رقمًا.', reply_markup=build_cancel_markup())
        return

    context.user_data['pending_med_stock'] = int(text)
    await db.update_user_state(user_id, PH_ADD_MED_REMINDER_TIME)
    await safe_reply(
        update,
        'أرسل وقت التذكير بصيغة 12 ساعة أو 24 ساعة. يمكنك إضافة أكثر من توقيت بفواصل، مثلاً: 08:00 ص, 08:00 م.',
        reply_markup=build_cancel_markup(),
    )


async def handle_pharmacist_add_med_reminder_time_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    reminder_times = parse_time_list(text)
    if not reminder_times:
        await safe_reply(update, 'الوقت غير صالح. استخدم صيغة أمثلة مثل 08:30 ص أو 20:00 أو عدة أوقات بفواصل.', reply_markup=build_cancel_markup())
        return

    patient_id = context.user_data.get('admin_target_patient_id')
    name = context.user_data.get('pending_med_name', '')
    dosage = context.user_data.get('pending_med_dosage', '')
    stock_quantity = int(context.user_data.get('pending_med_stock', 0) or 0)

    if not patient_id or not name or not dosage:
        await reset_user_flow(user_id)
        clear_admin_pending_context(context)
        await safe_reply(
            update,
            'فشل حفظ الدواء. أعد المحاولة من القائمة الرئيسية.',
            reply_markup=build_main_menu_markup(user_id),
        )
        return

    medicine_id = await db.add_medicine(patient_id, name, dosage, stock_quantity, refill_threshold=2)
    scheduled_times = []
    for reminder_time in reminder_times:
        reminder_id = await db.add_reminder(medicine_id, reminder_time.strftime('%H:%M'))
        await schedule_reminder_job(context.application, patient_id, medicine_id, reminder_id, reminder_time)
        scheduled_times.append(reminder_time.strftime('%H:%M'))

    await db.update_user_state(user_id, IDLE)
    clear_admin_pending_context(context)
    await safe_reply(
        update,
        f'✅ تمت إضافة الدواء للمريض بنجاح: {name}.\n⏰ مواعيد التذكير: {", ".join(scheduled_times)}.',
        reply_markup=build_main_menu_markup(user_id),
    )
    
    # تحقق من التداخلات الدوائية للمريض
    await show_drug_interactions_warning(patient_id, name, update, context)


async def handle_pharmacist_edit_reminder_time_state(
    user_id: str,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    reminder_times = parse_time_list(text)
    if not reminder_times:
        await safe_reply(update, 'الوقت غير صالح. استخدم صيغة أمثلة مثل 08:30 ص أو 20:00 أو عدة أوقات بفواصل.', reply_markup=build_cancel_markup())
        return

    med_id = context.user_data.get('pending_edit_med_id')
    if not med_id:
        await reset_user_flow(user_id)
        clear_admin_pending_context(context)
        await safe_reply(update, 'فشل تعديل التذكير. أعد المحاولة من القائمة الرئيسية.', reply_markup=build_main_menu_markup(user_id))
        return

    try:
        medicine = await db.get_medicine_by_id(int(med_id))
        if not medicine:
            await db.update_user_state(user_id, IDLE)
            clear_admin_pending_context(context)
            await safe_reply(update, 'لم يتم العثور على الدواء.', reply_markup=build_main_menu_markup(user_id))
            return

        await db.delete_reminders_for_medicine(int(med_id))
        remove_reminder_jobs_for_medicine(context.application, int(med_id))

        scheduled_times = []
        for reminder_time in reminder_times:
            reminder_id = await db.add_reminder(int(med_id), reminder_time.strftime('%H:%M'))
            await schedule_reminder_job(
                context.application,
                str(medicine['patient_id']),
                int(med_id),
                reminder_id,
                reminder_time,
            )
            scheduled_times.append(reminder_time.strftime('%H:%M'))

        await db.update_user_state(user_id, IDLE)
        clear_admin_pending_context(context)
        await safe_reply(
            update,
            f'✅ تم تحديث أوقات التذكير إلى: {", ".join(scheduled_times)}.',
            reply_markup=build_main_menu_markup(user_id),
        )
    except Exception as exc:
        logger.exception('Failed to update patient reminder times for med_id=%s', med_id)
        await db.update_user_state(user_id, IDLE)
        clear_admin_pending_context(context)
        await safe_reply(update, '❌ حدث خطأ أثناء تحديث مواعيد التذكير. حاول مرة أخرى لاحقاً.', reply_markup=build_main_menu_markup(user_id))
        if context and context.bot:
            try:
                await context.bot.send_message(
                    chat_id=int(ADMIN_ID),
                    text=(
                        f"[Reminder Edit Error] med_id={med_id} user_id={user_id} text={text[:200]}\nerror={exc}"
                    ),
                )
            except Exception:
                logger.exception('Failed to notify admin about reminder edit failure')


async def ai_medical_assistant(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    try:
        if not question:
            await safe_reply(update, 'أرسل سؤالك الطبي وسأساعدك بما أستطيع.', reply_markup=build_cancel_markup())
            return

        if not client:
            message = 'مفتاح AI_API_KEY أو GROQ_API_KEY غير مكوّن أو مكتبة openai غير متاحة. يرجى التحقق من الإعدادات.'
            if openai is None and openai_import_error:
                message = 'مكتبة OpenAI غير متاحة حالياً. يرجى التحقق من تثبيتها وعدم حظرها بواسطة سياسة الأمان.'
                logger.error('OpenAI import failure: %s', openai_import_error)
            await safe_reply(update, message)
            return

        response = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                messages=[
                    {'role': 'system', 'content': 'أنت مساعد طبي ودود وموثوق.'},
                    {'role': 'user', 'content': question},
                ],
                max_tokens=500,
            )
        )
        answer = response.choices[0].message.content.strip()
        await safe_reply(update, answer)
    except Exception as e:
        logger.error(f"CRITICAL ERROR in ai_medical_assistant: {e}", exc_info=True)
        await safe_reply(update, 'حدث خطأ أثناء استدعاء OpenAI. حاول مرة أخرى لاحقًا.')


async def schedule_reminder_job(
    application: Application,
    patient_id: str,
    medicine_id: int,
    reminder_id: int,
    reminder_time: datetime.time,
) -> None:
    try:
        job_name = f"reminder_{reminder_id}"
        next_job_name = f"{job_name}_next"

        for existing in application.job_queue.get_jobs_by_name(job_name):
            existing.schedule_removal()
        for existing in application.job_queue.get_jobs_by_name(next_job_name):
            existing.schedule_removal()

        # Schedule the recurring daily job (will run at the given time each day)
        application.job_queue.run_daily(
            send_reminder,
            time=reminder_time,
            days=tuple(range(7)),
            chat_id=int(patient_id),
            name=job_name,
            data={'reminder_id': reminder_id, 'medicine_id': medicine_id},
        )

        # Also schedule the immediate-next occurrence precisely (timezone-aware)
        now = datetime.datetime.now(APP_TIMEZONE)
        next_run = datetime.datetime.combine(now.date(), reminder_time)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=APP_TIMEZONE)
        if next_run <= now:
            next_run += timedelta(days=1)

        application.job_queue.run_once(
            send_reminder,
            when=next_run,
            chat_id=int(patient_id),
            name=next_job_name,
            data={'reminder_id': reminder_id, 'medicine_id': medicine_id},
        )
    except Exception:
        logger.exception('Failed to schedule reminder job for patient %s', patient_id)


def remove_reminder_jobs_for_medicine(application: Application, medicine_id: int) -> None:
    """Remove any queued reminder jobs for a given medicine."""
    for job in application.job_queue.jobs():
        if job.data and job.data.get('medicine_id') == medicine_id:
            job.schedule_removal()


def clear_admin_pending_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ['admin_target_patient_id', 'pending_edit_med_id', 'pending_med_name', 'pending_med_dosage', 'pending_med_stock']:
        if key in context.user_data:
            del context.user_data[key]


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data or {}
    reminder_id = job_data.get('reminder_id')
    medicine_id = job_data.get('medicine_id')
    patient_id = context.job.chat_id

    try:
        logger.info('Attempting to send reminder %s for medicine %s to chat %s', reminder_id, medicine_id, patient_id)
        reminders = await db.get_all_active_reminders()
        reminder = next((item for item in reminders if item['reminder_id'] == reminder_id), None)
        if not reminder:
            logger.warning('Reminder %s not found in DB; skipping send', reminder_id)
            return

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton('تم التناول ✅', callback_data=f'take_{medicine_id}'),
                    InlineKeyboardButton('تخطي ❌', callback_data=f'skip_{medicine_id}'),
                ]
            ]
        )
        send_text = (
            f"تذكير دواء: {reminder['medicine_name']}\n"
            f"الجرعة: {reminder['dosage']}\n"
            f"المخزون الحالي: {reminder['stock_quantity']}\n"
            'اضغط على الزر المناسب لتأكيد الحالة.'
        )
        await context.bot.send_message(chat_id=patient_id, text=send_text, reply_markup=keyboard)
        logger.info('Successfully sent reminder %s to chat %s', reminder_id, patient_id)
    except Exception:
        logger.exception('Failed to send reminder %s to chat %s', reminder_id, patient_id)


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command for admins to list active jobs in the job queue for debugging."""
    try:
        app = context.application
        jobs = app.job_queue.jobs()
        if not jobs:
            await safe_reply(update, 'لا توجد مهام مجدولة حالياً.')
            return

        lines: List[str] = []
        for job in jobs:
            name = getattr(job, 'name', 'unnamed')
            next_run = getattr(job, 'next_run_time', None)
            data = job.data or {}
            when = next_run.astimezone(APP_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S') if next_run else 'N/A'
            lines.append(f"{name} — next: {when} — data: {data}")

        await safe_reply(update, '\n'.join(lines))
    except Exception:
        logger.exception('Failed to list reminders')
        await safe_reply(update, 'حدث خطأ أثناء جلب قائمة المهام.')


async def test_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Schedule a one-off test reminder to run in ~60 seconds for the invoking user."""
    try:
        user = update.effective_user
        if not user:
            return
        chat_id = int(user.id)
        app = context.application
        job_name = f"test_reminder_{chat_id}_{int(time.time())}"
        app.job_queue.run_once(
            send_test_message,
            when=timedelta(seconds=60),
            chat_id=chat_id,
            name=job_name,
            data={'note': 'test'},
        )
        await safe_reply(update, 'تم جدولة تذكير تجريبي بعد ~60 ثانية. تحقق من وصول الإشعار.')
    except Exception:
        logger.exception('Failed to schedule test reminder')
        await safe_reply(update, 'فشل جدولة التذكير التجريبي.')


async def send_test_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        chat_id = context.job.chat_id
        note = (context.job.data or {}).get('note')
        logger.info('Sending test reminder to chat %s (note=%s)', chat_id, note)
        await context.bot.send_message(chat_id=chat_id, text='⚠️ هذا تذكير تجريبي — تأكد أن الإشعارات تعمل الآن.')
        logger.info('Test reminder sent to chat %s', chat_id)
    except Exception:
        logger.exception('Failed to send test reminder message to chat %s', getattr(context.job, 'chat_id', 'unknown'))


async def display_patient_medicines(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    patient_id: str,
    admin_view: bool = False,
    show_refill_buttons: bool = False,
) -> None:
    """عرض أدوية المريض مع سجل آخر الجرعات - بدون الحاجة لـ ID يدوي."""
    try:
        medicines = await db.get_patient_medicines(patient_id)
        if not medicines:
            await safe_reply(update, 'لا توجد أدوية مسجلة في هذا الحساب.', reply_markup=build_main_menu_markup(patient_id))
            return

        # For patients, send each medicine as its own message with isolated action buttons
        if not admin_view:
            for med in medicines:
                text_lines = [f"💊 {med['name']}", f"الجرعة: {med['dosage']}", f"الكمية المتبقية: {med['stock_quantity']}"]
                reminder_rows = await db.get_reminders_for_medicine(med['id'])
                reminder_times = [r['reminder_time'] for r in reminder_rows]
                if reminder_times:
                    text_lines.append("⏰ أوقات التذكير: " + ", ".join(reminder_times))

                text = "\n".join(text_lines)
                kb = [
                    [
                        InlineKeyboardButton('✏️ تعديل الجرعة', callback_data=f'editmed_dosage_{med["id"]}'),
                        InlineKeyboardButton('📦 تعديل الكمية', callback_data=f'editmed_stock_{med["id"]}'),
                    ],
                    [
                        InlineKeyboardButton('⏰ تعديل التوقيت', callback_data=f'editmed_time_{med["id"]}'),
                        InlineKeyboardButton('🗑️ حذف الدواء', callback_data=f'delete_{med["id"]}'),
                    ],
                ]
                if update.callback_query:
                    await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
                else:
                    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
            return

        # Admin aggregated view
        lines: List[str] = []
        keyboard_rows: List[List[InlineKeyboardButton]] = []
        for med in medicines:
            lines.append(f'\n💊 {med["name"]} | {med["dosage"]} | 📦 المخزون: {med["stock_quantity"]}')
            intake_logs = await db.get_recent_intake_logs_for_medicine(med['id'], limit=3)
            if intake_logs:
                lines.append('  📋 آخر الجرعات:')
                for log in intake_logs:
                    status = log.get('status', 'unknown')
                    timestamp = log.get('timestamp', 'غير معروف')
                    status_ar = {
                        'taken': '✅ تم التناول',
                        'skipped': '❌ تم التخطي',
                        'missed': '⚠️ تم التفويت'
                    }.get(status, f'📌 {status}')
                    lines.append(f'    {status_ar} • {timestamp}')
            else:
                lines.append('    📋 لا توجد سجلات جرعات بعد')

            row: List[InlineKeyboardButton] = []
            row.append(InlineKeyboardButton('➕ إضافة 30 حبة', callback_data=f'refill_{med["id"]}'))
            row.append(InlineKeyboardButton('🗑️ حذف الدواء', callback_data=f'delete_{med["id"]}'))
            row.append(InlineKeyboardButton('⏰ تغيير التذكير', callback_data=f'editreminder_{med["id"]}'))
            keyboard_rows.append(row)

        keyboard_rows.append([
            InlineKeyboardButton('➕ إضافة دواء جديد', callback_data=f'admin_add_med_{patient_id}'),
            InlineKeyboardButton('🔙 العودة', callback_data='back_to_main'),
        ])

        prefix = '📋 أدويتي:' if not admin_view else '📋 أدوية المريض:'
        await safe_reply(
            update,
            f'{prefix}' + ''.join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None,
        )
    except Exception as e:
        logger.error(f"CRITICAL ERROR in display_patient_medicines: {e}", exc_info=True)
        await safe_reply(update, 'حدث خطأ أثناء عرض الأدوية. حاول مرة أخرى لاحقًا.')


async def show_pharmacist_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """لوحة الصيدلي - عرض قائمة المرضى بدون الحاجة لإدخال ID يدوي."""
    try:
        # جلب جميع المرضى الذين لديهم أدوية مسجلة - ديناميكياً وبدون ID يدوي
        patients = await db.get_all_patients_with_medicines()
        
        if not patients:
            await safe_reply(
                update,
                '📭 لا يوجد مرضى مسجلين حالياً.',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('📢 إذاعة رسالة', callback_data='pharmacist_broadcast')],
                    [InlineKeyboardButton('🔙 العودة', callback_data='back_to_main')]
                ])
            )
            return
        
        # بناء قائمة الأزرار بأسماء المرضى فقط - callback_data يحتوي على patient_id
        keyboard_rows: List[List[InlineKeyboardButton]] = []
        for patient in patients:
            patient_id = patient['user_id']
            patient_name = patient.get('full_name', 'مريض')
            username_display = f" (@{patient['username']})" if patient.get('username') else ""
            
            keyboard_rows.append([
                InlineKeyboardButton(f'👤 {patient_name}{username_display}', callback_data=f'patient_view_{patient_id}')
            ])
        
        # إضافة أزرار الإجراءات الإدارية في الأسفل
        keyboard_rows.append([
            InlineKeyboardButton('📦 إضافة مخزون سريع', callback_data='pharmacist_refill')
        ])
        keyboard_rows.append([
            InlineKeyboardButton('➕ إضافة دواء لمريض', callback_data='pharmacist_add_med_start')
        ])
        keyboard_rows.append([
            InlineKeyboardButton('📢 إذاعة رسالة جماعية', callback_data='pharmacist_broadcast')
        ])
        
        await safe_reply(
            update,
            f'👨‍⚕️ لوحة الصيدلي\n\n📊 عدد المرضى: {len(patients)}\n\nاختر مريضاً لعرض أدويته وإدارة جرعاته أو استخدم زر إضافة دواء سريع:',
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )
    except Exception as e:
        logger.error(f"CRITICAL ERROR in show_pharmacist_dashboard: {e}", exc_info=True)
        await safe_reply(update, 'حدث خطأ في لوحة الصيدلي. حاول مرة أخرى لاحقًا.')


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    try:
        user_id = str(query.from_user.id) if query.from_user else None
        is_admin = str(user_id) == str(ADMIN_ID)

        # ===== معالج جديد: عرض أدوية المريض من قائمة الصيدلي (بدون ID يدوي) =====
        if query.data.startswith('patient_view_'):
            if not is_admin:
                await query.edit_message_text('عذراً، هذه الخاصية محجوزة للصيدلي فقط.')
                return
            
            patient_id = query.data.split('_', 2)[2]
            patient = await db.get_user(patient_id)
            if not patient:
                await query.edit_message_text('لم يتم العثور على المريض.')
                return
            
            try:
                await query.delete_message()
            except:
                pass
            
            await display_patient_medicines(update, context, patient_id, admin_view=True, show_refill_buttons=True)
            return

        if query.data.startswith('admin_add_med_'):
            if not is_admin:
                await query.edit_message_text('عذراً، هذه الخاصية محجوزة للصيدلي فقط.')
                return

            patient_id = query.data[len('admin_add_med_'):]
            if user_id:
                await db.update_user_state(user_id, PH_ADD_MED_NAME)
            context.user_data['admin_target_patient_id'] = patient_id
            try:
                await query.delete_message()
            except:
                pass
            await query.message.reply_text(
                f'أرسل اسم الدواء الذي تريد إضافته للمريض ({patient_id}):',
                reply_markup=build_cancel_markup(),
            )
            return

        if query.data == 'back_to_main':
            await query.delete_message()
            await send_main_menu(update, context)
            return

        if query.data == 'pharmacist_lookup':
            if not is_admin:
                await query.edit_message_text('عذراً، هذه الخاصية محجوزة للصيدلي فقط.')
                return
            if user_id:
                await db.update_user_state(user_id, PH_WAITING_FOR_ID)
            await query.message.reply_text('يرجى إرسال الـ User ID الخاص بالمريض:')
            return

        if query.data == 'pharmacist_add_med_start':
            if not is_admin:
                await query.edit_message_text('عذراً، هذه الخاصية محجوزة للصيدلي فقط.')
                return
            try:
                await query.delete_message()
            except:
                pass
            await show_pharmacist_dashboard(update, context)
            return

        if query.data == 'pharmacist_refill':
            if not is_admin:
                await query.edit_message_text('عذراً، هذه الخاصية محجوزة للصيدلي فقط.')
                return
            if user_id:
                await db.update_user_state(user_id, PH_WAIT_REFILL)
            await query.message.reply_text('أرسل رقم الدواء (ID) لإضافة 30 حبة:')
            return

        if query.data == 'pharmacist_broadcast':
            if not is_admin:
                await query.edit_message_text('عذراً، هذه الخاصية محجوزة للصيدلي فقط.')
                return
            if user_id:
                await db.update_user_state(user_id, PH_WAIT_BROADCAST)
            await query.message.reply_text('أرسل نص الإذاعة الذي ترغب بإرساله لجميع المرضى:')
            return

        if query.data.startswith('refill_'):
            if not is_admin:
                await query.edit_message_text('عذراً، هذه الخاصية محجوزة للصيدلي فقط.')
                return
            med_id = int(query.data.split('_', 1)[1])
            medicine = await db.get_medicine_by_id(med_id)
            if not medicine:
                await query.edit_message_text('لم يتم العثور على الدواء.')
                return
            updated_stock = await increment_medicine_stock(med_id, 30)
            await query.edit_message_text(
                f'✅ تمت إضافة 30 حبة إلى {medicine["name"]}.\n📦 المخزون الجديد: {updated_stock or "غير معروف"} حبة.'
            )
            return

        if query.data.startswith('editreminder_'):
            if not is_admin:
                await query.edit_message_text('عذراً، هذه الخاصية محجوزة للصيدلي فقط.')
                return
            med_id = int(query.data.split('_', 1)[1])
            if user_id:
                await db.update_user_state(user_id, PH_EDIT_REMINDER_TIME)
            context.user_data['pending_edit_med_id'] = med_id
            await query.message.reply_text(
                'أرسل الوقت الجديد للتذكير لهذا الدواء. يمكنك إرسال أكثر من توقيت بفواصل، مثل: 08:00 ص, 20:00',
                reply_markup=build_cancel_markup(),
            )
            return

        # Patient-edit handlers: edit dosage, stock, times
        if query.data.startswith('editmed_dosage_'):
            med_id = int(query.data.split('_')[-1])
            context.user_data['pending_edit_med_id'] = med_id
            if user_id:
                await db.update_user_state(user_id, PATIENT_EDIT_DOSAGE)
            await query.message.reply_text('أرسل الجرعة الجديدة للدواء (مثال: 1 حبة صباحاً ومساءً)')
            return

        if query.data.startswith('editmed_stock_'):
            med_id = int(query.data.split('_')[-1])
            context.user_data['pending_edit_med_id'] = med_id
            if user_id:
                await db.update_user_state(user_id, PATIENT_EDIT_STOCK)
            await query.message.reply_text('أرسل الكمية الجديدة كرقم (مثال: 30)')
            return

        if query.data.startswith('editmed_time_'):
            med_id = int(query.data.split('_')[-1])
            context.user_data['pending_edit_med_id'] = med_id
            if user_id:
                await db.update_user_state(user_id, PATIENT_EDIT_TIME)
            await query.message.reply_text('أرسل الأوقات الجديدة مفصولة بفواصل (مثال: 09:00, 13:00, 20:30). استخدم ، أو ,')
            return

        if query.data.startswith('delete_'):
            med_id = int(query.data.split('_', 1)[1])
            # allow admin or owner to delete
            med = await db.get_medicine_by_id(med_id)
            if not med:
                await query.edit_message_text('❌ لم يتم العثور على الدواء.')
                return
            if is_admin or (query.from_user and med.get('patient_id') == str(query.from_user.id)):
                success = await db.delete_medicine(med_id)
                await query.edit_message_text('✅ تم حذف الدواء بنجاح.' if success else '❌ فشل حذف الدواء.')
            else:
                await query.edit_message_text('لا تملك صلاحية حذف هذا الدواء.')
            return

        if query.data.startswith('take_'):
            medicine_id = int(query.data.split('_', 1)[1])
            await db.log_intake(medicine_id, 'taken')
            await db.decrement_stock(medicine_id)
            medicine = await db.get_medicine_by_id(medicine_id)
            if medicine:
                if medicine['stock_quantity'] <= 5:
                    await db.add_refill_request(medicine_id, str(query.from_user.id), 'Low stock after intake')
                    patient = await db.get_user(medicine['patient_id'])
                    patient_name = (
                        patient.get('full_name')
                        if patient and patient.get('full_name')
                        else (f'@{patient.get("username")}' if patient and patient.get('username') else medicine['patient_id'])
                    )
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f'⚠️ تنبيه صيدلاني: الدواء ({medicine["name"]}) الخاص بالمريض ({patient_name}) '
                            f'شارف على النفاد! المخزون الحالي: {medicine["stock_quantity"]} حبات فقط.'
                        ),
                    )
                    await query.edit_message_text('✅ تم تسجيل التناول.\n⚠️ الدواء يحتاج لإعادة تعبئة قريبًا.')
                else:
                    await query.edit_message_text('✅ تم تسجيل التناول بنجاح. شكراً لك.')
            else:
                await query.edit_message_text('❌ حدث خطأ أثناء تحديث المخزون.')
            return

        if query.data.startswith('skip_'):
            medicine_id = int(query.data.split('_', 1)[1])
            await db.log_intake(medicine_id, 'skipped')
            await query.edit_message_text('✅ تم تسجيل تخطي الجرعة. راجع الطبيب عند الحاجة.')
            return

        await query.edit_message_text('الإجراء غير معروف أو غير مدعوم.')
    except Exception as e:
        logger.error(f"CRITICAL ERROR in handle_callback_query: {e}", exc_info=True)
        try:
            await query.edit_message_text('❌ حدث خطأ أثناء معالجة الإجراء.')
        except Exception:
            pass


async def send_compliance_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        logs = await db.get_all_active_reminders()
        from fpdf import FPDF

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font('Arial', 'B', 16)
        pdf.cell(0, 10, 'تقرير الالتزام الطبي', ln=True)
        pdf.set_font('Arial', '', 12)

        if logs:
            for log in logs[:50]:
                pdf.cell(
                    0,
                    8,
                    f"{log['patient_id']} | {log['medicine_name']} | {log['reminder_time']} | مخزون: {log['stock_quantity']}",
                    ln=True,
                )
        else:
            pdf.cell(0, 8, 'لا توجد سجلات تذكير حالياً.', ln=True)

        buffer = io.BytesIO(pdf.output(dest='S').encode('latin-1'))
        buffer.name = 'compliance_report.pdf'
        if update.message:
            await update.message.reply_document(buffer)
        elif update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_document(buffer)
    except Exception:
        logger.exception('Failed to generate compliance PDF')
        await safe_reply(update, 'حدث خطأ أثناء إنشاء تقرير الامتثال. يرجى المحاولة مجدداً لاحقاً.')


async def startup_reload_reminders(application: Application) -> None:
    await db.init_db()
    
    # Setup startup jobs including seeding drug interactions
    await setup_startup_jobs(application)
    
    reminders = await db.get_all_active_reminders()
    for reminder in reminders:
        try:
            send_time = datetime.datetime.strptime(reminder['reminder_time'], '%H:%M').time()
            # Use centralized scheduler which also queues the immediate next occurrence
            await schedule_reminder_job(
                application,
                reminder['patient_id'],
                reminder['medicine_id'],
                reminder['reminder_id'],
                send_time,
            )
        except Exception:
            logger.exception('Failed to schedule reminder %s', reminder['reminder_id'])

    # Optionally schedule a startup test reminder to ADMIN to verify delivery
    try:
        enable_test = str(os.getenv('ENABLE_STARTUP_TEST', '0')).lower() in ('1', 'true', 'yes')
        if enable_test:
            test_job_name = 'startup_test'
            for existing in application.job_queue.get_jobs_by_name(test_job_name):
                existing.schedule_removal()
            application.job_queue.run_once(
                send_test_message,
                when=0,
                chat_id=int(ADMIN_ID),
                name=test_job_name,
                data={'note': 'startup_test'},
            )
            logger.info('Startup test reminder scheduled to admin (ENABLE_STARTUP_TEST=1)')
        else:
            logger.info('Startup test reminder disabled (ENABLE_STARTUP_TEST not set)')
    except Exception:
        logger.exception('Failed to schedule startup test reminder')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception('Update caused error: %s', context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text('حدث خطأ غير متوقع. حاول مرة أخرى لاحقًا.')


def main() -> None:
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
    )

    if not TELEGRAM_TOKEN:
        logger.error('TELEGRAM_TOKEN is required and not set in .env')
        raise RuntimeError('TELEGRAM_TOKEN is required')

    asyncio.run(db.init_db())
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('list_reminders', list_reminders))
    application.add_handler(CommandHandler('test_reminder', test_reminder))
    application.add_handler(CommandHandler('cancel', cancel))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_user_message))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_error_handler(error_handler)

    application.job_queue.run_once(lambda ctx: asyncio.create_task(startup_reload_reminders(application)), when=0)
    application.run_polling()


if __name__ == '__main__':
    main()
