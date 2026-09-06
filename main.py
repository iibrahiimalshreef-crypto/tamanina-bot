import asyncio
import logging
import os
import random
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from threading import Thread

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError


# =========================================================
# 0. إعداد نظام تسجيل الأخطاء (Logging)
# =========================================================
# بدل print() نستخدم logging عشان نقدر نتابع الأخطاء
# بشكل منظم مع الوقت ومستوى الخطورة، وهذا مفيد جداً
# على منصات مثل Render لمراجعة الـ logs لاحقاً.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("tomaninah_bot")


# =========================================================
# 1. خادم Flask لإبقاء السيرفر صاحياً عبر Render
# =========================================================

app_web = Flask(__name__)


@app_web.route("/")
def home():
    return "Bot is running perfectly!"


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host="0.0.0.0", port=port)


def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()


# =========================================================
# 2. إعدادات البوت والوسائط وقاعدة البيانات
# =========================================================

BOT_TOKEN = "8867227824:AAFTkVDZ6ziSQgXmsDOK4Nzw6a3gP0E3wQU"

# ⚠️ ضع هنا ID حسابك في التلجرام لتشغيل أمر الإحصائيات
ADMIN_ID = 8955520748

# رابط قراءة سورة الكهف
SURAH_KAHF_AUDIO = "https://server14.mp3quran.net/hazza/018.mp3"

# أسماء ملفات الصور
MORNING_IMAGE_PATH = "morning.jpg"
EVENING_IMAGE_PATH = "evening.jpg"

BACKGROUND_TASKS = set()

# رابط MongoDB من Render
MONGO_URI = os.environ.get("MONGO_URI")

if not MONGO_URI:
    # نوقف التشغيل فوراً بدل ما نخلي pymongo يحاول
    # الاتصال بـ localhost بصمت ويفشل لاحقاً بدون سبب واضح
    raise RuntimeError(
        "متغير البيئة MONGO_URI غير معرف! "
        "تأكد من ضبطه في إعدادات Render قبل التشغيل."
    )

client = MongoClient(MONGO_URI)
db = client["azkar_bot_db"]
chats_col = db["chats"]

# فهرس فريد على chat_id يمنع إنشاء سجلين لنفس المحادثة
# في حال وصول طلبين متزامنين (race condition)
chats_col.create_index("chat_id", unique=True)


# =========================================================
# 3. وظائف قاعدة البيانات
# =========================================================

def save_or_update_chat(chat_id, chat_type="private", update_data=None):
    """
    إنشاء أو تحديث سجل المحادثة بعملية upsert واحدة ذرية،
    بدل find_one ثم insert_one (كانت تسمح بحالة نادرة
    لإنشاء سجلين لنفس chat_id عند التزامن).
    """

    default_data = {
        "chat_id": chat_id,
        "type": chat_type,
        "is_active": True,

        # عدد ساعات التكرار
        "interval_hours": 1,

        # ساعات العمل الافتراضية:
        # من 5 صباحًا إلى 2 صباحًا
        "start_hour": 5,
        "end_hour": 2,

        # وقت آخر ذكر دوري تم إرساله
        "last_athkar_sent": None,

        # أوقات الأذكار الخاصة
        "last_morning_sent": None,
        "last_evening_sent": None,
        "last_friday_sent": None
    }

    try:
        if update_data:
            # عند وجود تحديثات: نطبّقها دائماً،
            # ونستخدم القيم الافتراضية فقط عند الإنشاء لأول مرة
            chats_col.update_one(
                {"chat_id": chat_id},
                {
                    "$set": update_data,
                    "$setOnInsert": {
                        k: v for k, v in default_data.items()
                        if k not in update_data
                    }
                },
                upsert=True
            )
        else:
            # لا توجد تحديثات: ننشئ السجل فقط لو غير موجود
            chats_col.update_one(
                {"chat_id": chat_id},
                {"$setOnInsert": default_data},
                upsert=True
            )

    except DuplicateKeyError:
        # حالة نادرة جداً عند تزامن طلبين في نفس اللحظة،
        # لكن السجل يكون موجود فعلياً فلا داعي لأي إجراء
        logger.warning(
            f"DuplicateKeyError عند upsert للمحادثة {chat_id} "
            "(تم تجاهله لأن السجل موجود بالفعل)"
        )


def remove_chat(chat_id):
    """حذف المحادثة التي حظرت البوت أو تم طرده منها"""
    chats_col.delete_one({"chat_id": chat_id})


def get_chat_settings(chat_id, chat_type="private"):
    chat = chats_col.find_one({"chat_id": chat_id})

    if not chat:
        save_or_update_chat(chat_id, chat_type)
        return chats_col.find_one({"chat_id": chat_id})

    return chat


def is_within_working_hours(current_hour, start_hour=5, end_hour=2):
    """
    تحديد هل الوقت الحالي داخل ساعات العمل.

    مثال:
    5 ص -> 2 ص = فترة تمتد عبر منتصف الليل.
    """

    # 24 ساعة كاملة
    if start_hour == 0 and end_hour == 24:
        return True

    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour

    return current_hour >= start_hour or current_hour < end_hour


async def is_authorized_to_manage(chat, user_id, bot):
    """
    التحقق من صلاحية المستخدم للتحكم بالإعدادات:
    - في المحادثات الخاصة: مسموح دائماً (صاحب المحادثة نفسه).
    - في القروبات: مسموح فقط لو المستخدم أدمن أو مالك (creator).
    """

    if chat.type == "private":
        return True

    try:
        member = await bot.get_chat_member(chat.id, user_id)
        return member.status in ("administrator", "creator")

    except Exception as e:
        logger.error(
            f"تعذر التحقق من صلاحية المستخدم {user_id} "
            f"في {chat.id}: {e}"
        )
        # في حال تعذر التحقق (خطأ شبكة مثلاً)
        # الأفضل نرفض الوصول احتياطاً بدل السماح بالخطأ
        return False


def parse_saved_datetime(value, tz):
    """
    تحويل الوقت المخزن في MongoDB إلى datetime.
    في حال وجود قيمة قديمة أو غير صالحة يرجع None.
    """

    if not value:
        return None

    try:
        saved_time = datetime.fromisoformat(value)

        if saved_time.tzinfo is None:
            saved_time = saved_time.replace(tzinfo=tz)

        return saved_time

    except (ValueError, TypeError):
        return None


# =========================================================
# 4. قائمة الأذكار
# =========================================================

ATHKAR_LIST = [
    "سُبْحَانَ اللَّهِ وَبِحَمْدِهِ ، سُبْحَانَ اللَّهِ الْعَظِيمِ.",
    "لا إِلَهَ إِلا اللَّهُ وَحْدَهُ لا شَرِيكَ لَهُ، لَهُ الْمُلْكُ وَلَهُ الْحَمْدُ، وَهُوَ عَلَى كُلِّ شَيْءٍ قَدِيرٌ.",
    "سُبْحَانَ اللَّهِ، وَالْحَمْدُ لِلَّهِ، وَلا إِلَهَ إِلا اللَّهُ، وَاللَّهُ أَكْبَرُ.",
    "لا حَوْلَ وَلا قُوَّةَ إِلا بِاللَّهِ الْعَلِيِّ الْعَظِيمِ.. كَنْزٌ مِنْ كُنُوزِ الْجَنَّةِ.",
    "سُبْحَانَ اللَّهِ وَبِحَمْدِهِ ، عَدَدَ خَلْقِهِ ، وَرِضَا نَفْسِهِ ، وَزِنَةَ عَرْشِهِ ، وَمِدَادَ كَلِمَاتِهِ.",
    "الْحَمْدُ لِلَّهِ حَمْداً كَثِيراً طَيِّباً مُبَارَكاً فِيهِ.",
    "اللَّهُ أَكْبَرُ كَبِيراً، وَالْحَمْدُ لِلَّهِ كَثِيراً، وَسُبْحَانَ اللَّهِ بُكْرَةً وَأَصِيلاً.",
    "سُبْحَانَ المَلِكِ القُدُّوسِ.",
    "سُبُّوحٌ قُدُّوسٌ، رَبُّ المَلاَئِكَةِ وَالرُّوحِ.",
    "الْحَمْدُ لِلَّهِ الَّذِي بِنِعْمَتِهِ تَتِمُّ الصَّالِحَاتُ.",
    "اللَّهُمَّ صَلِّ وَسَلِّمْ وَبَارِكْ عَلَى نَبِيِّنَا مُحَمَّدٍ.",
    "اللَّهُمَّ صَلِّ عَلَى مُحَمَّدٍ وَعَلَى آلِ مُحَمَّدٍ كَمَا صَلَّيْتَ عَلَى إِبْرَاهِيمَ وَعَلَى آلِ إِبْرَاهِيمَ إِنَّكَ حَمِيدٌ مَجِيدٌ.",
    "صَلَّى اللَّهُ عَلَيْهِ وَسَلَّمَ.",
    "اللَّهُمَّ صَلِّ وَسَلِّمْ عَلَى نَبِيِّنَا مُحَمَّدٍ عَدَدَ مَا ذَكَرَهُ الذَّاكِرُونَ وَغَفَلَ عَنْ ذِكْرِهِ الْغَافِلُونَ.",
    "أَسْتَغْفِرُ اللَّهَ الْعَظِيمَ الَّذِي لا إِلَهَ إِلا هُوَ الْحَيَّ الْقَيُّومَ وَأَتُوبُ إِلَيْهِ.",
    "سَيِّدُ الاسْتِغْفَارِ: اللَّهُمَّ أَنْتَ رَبِّي لا إِلَهَ إِلا أَنْتَ، خَلَقْتَنِي وَأَنَا عَبْدُكَ، وَأَنَا عَلَى عَهْدِكَ وَوَعْدِكَ مَا اسْتَطَعْتُ، أَعُوذُ بِكَ مِنْ شَرِّ مَا صَنَعْتُ، أَبُوءُ لَكَ بِنِعْمَتِكَ عَلَيَّ، وَأَبُوءُ بِذَنْبِي فَاغْفِرْ لِي فَإِنَّهُ لا يَغْفِرُ الذُّنُوبَ إِلا أَنْتَ.",
    "رَبِّ اغْفِرْ لِي وَتُبْ عَلَيَّ إِنَّكَ أَنْتَ التَّوَّابُ الرَّحِيمُ.",
    "أَسْتَغْفِرُ اللَّهَ وَأَتُوبُ إِلَيْهِ.",
    "اللَّهُمَّ إِنِّي ظَلَمْتُ نَفْسِي ظُلْماً كَثِيراً، وَلا يَغْفِرُ الذُّنُوبَ إِلا أَنْتَ، فَاغْفِرْ لِي مَغْفِرَةً مِنْ عِنْدِكَ وَارْحَمْنِي إِنَّكَ أَنْتَ الْغَفُورُ الرَّحِيمُ.",
    "﴿رَبَّنَا آتِنَا فِي الدُّنْيَا حَسَنَةً وَفِي الآخِرَةِ حَسَنَةً وَقِنَا عَذَابَ النَّارِ﴾",
    "﴿رَبِّ اشْرَحْ لِي صَدْرِي * وَيَسِّرْ لِي أَمْرِي﴾",
    "﴿رَبَّنَا لا تُزِغْ قُلُوبَنَا بَعْدَ إِذْ هَدَيْتَنَا وَهَبْ لَنَا مِنْ لَدُنْكَ رَحْمَةً إِنَّكَ أَنْتَ الْوَهَّابُ﴾",
    "﴿رَبِّ زِدْنِي عِلْماً﴾",
    "﴿رَبِّ ابْنِ لِي عِنْدَكَ بَيْتاً فِي الْجَنَّةِ﴾",
    "﴿رَبَّنَا اغْفِرْ لِي وَلِوَالِدَيَّ وَلِلْمُؤْمِنِينَ يَوْمَ يَقُومُ الْحِسَابُ﴾",
    "﴿رَبِّ أَوْزِعْنِي أَنْ أَشْكُرَ نِعْمَتَكَ الَّتِي أَنْعَمْتَ عَلَيَّ وَعَلَى وَالِدَيَّ وَأَنْ أَعْمَلَ صَالِحاً تَرْضَاهُ﴾",
    "﴿رَبِّ هَبْ لِي مِنْ لَدُنْكَ ذُرِّيَّةً طَيِّبَةً إِنَّكَ سَمِيعُ الدُّعَاءِ﴾",
    "﴿رَبِّ اجْعَلْنِي مُقِيمَ الصَّلاةِ وَمِنْ ذُرِّيَّتِي رَبَّنَا وَتَقَبَّلْ دُعَاءِ﴾",
    "﴿حَسْبِيَ اللَّهُ لا إِلَهَ إِلا هُوَ عَلَيْهِ تَوَكَّلْتُ وَهُوَ رَبُّ الْعَرْشِ الْعَظِيمِ﴾",
    "رَضِيتُ بِاللَّهِ رَبّاً، وَبِالإِسْلامِ دِيناً، وَبِمُحَمَّدٍ صَلَّى اللَّهُ عَلَيْهِ وَسَلَّمَ نَبِيّاً وَرَسُولاً.",
    "يَا حَيُّ يَا قَيُّومُ بِرَحْمَتِكَ أَسْتَغِيثُ، أَصْلِحْ لِي شَأْنِي كُلَّهُ وَلا تَكِلْنِي إِلَى نَفْسِي طَرْفَةَ عَيْنٍ.",
    "لا إِلَهَ إِلا أَنْتَ سُبْحَانَكَ إِنِّي كُنْتُ مِنَ الظَّالِمِينَ.",
    "اللَّهُمَّ إِنِّي أَسْأَلُكَ الْعَفْوَ وَالْعَافِيَةَ فِي الدُّنْيَا وَالآخِرَةِ.",
    "اللَّهُمَّ إنِّي أَعُوذُ بِكَ مِنَ الهَمِّ وَالحَزَنِ، وَالعَجْزِ وَالكَسَلِ، وَالبُخْلِ وَالجُبْنِ، وَضَلَعِ الدَّيْنِ، وَغَلَبَةِ الرِّجَالِ.",
    "اللَّهُمَّ أَعِنِّي عَلَى ذِكْرِكَ وَشُكْرِكَ وَحُسْنِ عِبَادَتِكَ.",
    "اللَّهُمَّ يَا مُقَلِّبَ الْقُلُوبِ ثَبِّتْ قَلْبِي عَلَى دِينِكَ.",
    "اللَّهُمَّ إنِّي أَسْأَلُكَ الهُدَى وَالتُّقَى وَالعَفَافَ وَالغِنَى.",
    "اللَّهُمَّ اكْفِنِي بِحَلالِكَ عَنْ حَرَامِكَ، وَأَغْنِنِي بِفَضْلِكَ عَمَّنْ سِوَاكَ.",
    "اللَّهُمَّ إِنِّي أَسْأَلُكَ عِلْماً نَافِعاً، وَرِزْقاً طَيِّباً، وَعَمَلاً مُتَقَبَّلاً.",
    "اللَّهُمَّ إِنِّي أَعُوذُ بِكَ مِنْ زَوَالِ نِعْمَتِكَ، وَتَحَوُّلِ عَافِيَتِكَ، وَفُجَاءَةِ نِقْمَتِكَ، وَجَمِيعِ سَخَطِكَ.",
    "اللَّهُمَّ أحْسِنْ عَاقِبَتَنَا فِي الأُمُورِ كُلِّهَا، وَأَجِرْنَا مِنْ خِزْيِ الدُّنْيَا وَعَذَابِ الآخِرَةِ.",
    "اللَّهُمَّ آتِ نُفُوسَنَا تَقْوَاهَا، وَزَكِّهَا أَنْتَ خَيْرُ مَنْ زَكَّاهَا، أَنْتَ وَلِيُّهَا وَمَوْلاهَا."
]


# =========================================================
# 5. إرسال الذكر الدوري
# =========================================================

async def send_periodic_athkar(application, chat_id, tz):
    """
    إرسال ذكر دوري واحد للمحادثة.
    يرجع True إذا تم الإرسال بنجاح.
    """

    text_athkar = random.choice(ATHKAR_LIST)

    message_text = (
        f"✨ *طُمأنينة:*\n\n"
        f"{text_athkar}"
    )

    share_url = (
        "https://t.me/share/url?url="
        + urllib.parse.quote(message_text)
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "شارك الذكر 🔄",
                url=share_url
            )
        ]
    ])

    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=message_text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

        sent_time = datetime.now(tz)

        chats_col.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "last_athkar_sent": sent_time.isoformat()
                }
            }
        )

        return True

    except (Forbidden, BadRequest):
        remove_chat(chat_id)
        return False

    except Exception as e:
        logger.error(f"خطأ إرسال الذكر {chat_id}: {e}")
        return False


# =========================================================
# 6. الأوامر (/start, /stop, /settings, /stats)
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    tz = ZoneInfo("Asia/Riyadh")

    authorized = await is_authorized_to_manage(
        chat,
        user.id,
        context.bot
    )

    if not authorized:
        await update.message.reply_text(
            "🚫 هذا الأمر متاح فقط لمالك القروب أو المشرفين (الأدمن)."
        )
        return

    existing = chats_col.find_one({"chat_id": chat.id})

    if existing:
        chats_col.update_one(
            {"chat_id": chat.id},
            {
                "$set": {
                    "is_active": True,
                    # إعادة بدء عداد التكرار
                    "last_athkar_sent": None
                }
            }
        )
    else:
        save_or_update_chat(
            chat.id,
            chat.type,
            update_data={
                "is_active": True,
                "last_athkar_sent": None
            }
        )

    await update.message.reply_text(
        "🌿 *تم تفعيل بوت طُمأنينة بنجاح!*\n\n"
        "سيقوم البوت بإرسال الأذكار دورياً، "
        "وأذكار الصباح والمساء، وسورة الكهف يوم الجمعة.\n\n"
        "⚙️ للتحكم بالإعدادات: /settings\n"
        "🛑 لإيقاف الإشعار المؤقت: /stop",
        parse_mode="Markdown"
    )

    # إرسال ذكر مباشرة عند تشغيل البوت
    config = get_chat_settings(chat.id, chat.type)

    if is_within_working_hours(
        datetime.now(tz).hour,
        config.get("start_hour", 5),
        config.get("end_hour", 2)
    ):
        await send_periodic_athkar(
            context.application,
            chat.id,
            tz
        )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    authorized = await is_authorized_to_manage(
        chat,
        user.id,
        context.bot
    )

    if not authorized:
        await update.message.reply_text(
            "🚫 هذا الأمر متاح فقط لمالك القروب أو المشرفين (الأدمن)."
        )
        return

    save_or_update_chat(
        chat.id,
        chat.type,
        update_data={
            "is_active": False
        }
    )

    await update.message.reply_text(
        "🛑 *تم إيقاف إرسال الأذكار بنجاح.*\n\n"
        "يمكنك إعادة تشغيل البوت في أي وقت بإرسال: /start",
        parse_mode="Markdown"
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    authorized = await is_authorized_to_manage(
        chat,
        user.id,
        context.bot
    )

    if not authorized:
        await update.message.reply_text(
            "🚫 هذا الأمر متاح فقط لمالك القروب أو المشرفين (الأدمن)."
        )
        return

    config = get_chat_settings(chat.id, chat.type)

    keyboard = [
        [
            InlineKeyboardButton(
                "كل ساعة",
                callback_data="set_int_1"
            ),
            InlineKeyboardButton(
                "كل ساعتين",
                callback_data="set_int_2"
            )
        ],
        [
            InlineKeyboardButton(
                "كل 3 ساعات",
                callback_data="set_int_3"
            ),
            InlineKeyboardButton(
                "كل 4 ساعات",
                callback_data="set_int_4"
            )
        ],
        [
            InlineKeyboardButton(
                "⏰ ساعات العمل (5 ص - 2 ص)",
                callback_data="set_hours_normal"
            )
        ],
        [
            InlineKeyboardButton(
                "🌐 24 ساعة بدون إيقاف",
                callback_data="set_hours_24"
            )
        ],
        [
            InlineKeyboardButton(
                "🔴 إيقاف الإشعارات"
                if config.get("is_active", True)
                else "🟢 تشغيل الإشعارات",
                callback_data="toggle_active"
            )
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    status_str = (
        "🟢 شغال"
        if config.get("is_active", True)
        else "🔴 متوقف"
    )

    start_hour = config.get("start_hour", 5)
    end_hour = config.get("end_hour", 2)

    if start_hour == 0 and end_hour == 24:
        hours_text = "24 ساعة بدون توقف"
    else:
        hours_text = f"{start_hour}:00 - {end_hour}:00"

    text = (
        "⚙️ *إعدادات بوت طُمأنينة:*\n\n"
        f"• *حالة البوت:* {status_str}\n"
        f"• *معدل تكرار الأذكار:* كل "
        f"{config.get('interval_hours', 1)} ساعة\n"
        f"• *ساعات العمل:* {hours_text}\n\n"
        "اختر من الأزرار للتعديل:"
    )

    await update.message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر معرفة عدد المشتركين والجروبات - للأدمن فقط"""

    user_id = update.effective_user.id

    if user_id != ADMIN_ID:
        return

    total_chats = chats_col.count_documents({})

    active_chats = chats_col.count_documents({
        "is_active": True
    })

    private_chats = chats_col.count_documents({
        "type": "private"
    })

    group_chats = chats_col.count_documents({
        "type": {
            "$in": [
                "group",
                "supergroup"
            ]
        }
    })

    text = (
        "📊 *إحصائيات بوت طُمأنينة:*\n\n"
        f"• *إجمالي المسجلين:* {total_chats}\n"
        f"• *المشتركين النشطين:* {active_chats}\n"
        f"• *المحادثات الخاصة:* {private_chats}\n"
        f"• *المجموعات:* {group_chats}"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown"
    )


# =========================================================
# 7. التعامل مع الأزرار
# =========================================================

async def button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    chat = query.message.chat
    user = query.from_user

    authorized = await is_authorized_to_manage(
        chat,
        user.id,
        context.bot
    )

    if not authorized:
        await query.answer(
            "🚫 هذا الإجراء متاح فقط لمالك القروب أو المشرفين.",
            show_alert=True
        )
        return

    await query.answer()

    chat_id = query.message.chat_id
    data = query.data

    tz = ZoneInfo("Asia/Riyadh")

    # =====================================================
    # تغيير معدل التكرار
    # =====================================================

    if data.startswith("set_int_"):

        interval = int(data.split("_")[2])

        config = get_chat_settings(chat_id)

        # حفظ الإعداد الجديد
        # وإعادة ضبط المؤقت
        save_or_update_chat(
            chat_id,
            update_data={
                "interval_hours": interval,
                "last_athkar_sent": None,
                "is_active": True
            }
        )

        # إرسال ذكر مباشرة بعد اختيار التكرار
        current_hour = datetime.now(tz).hour

        if is_within_working_hours(
            current_hour,
            config.get("start_hour", 5),
            config.get("end_hour", 2)
        ):
            sent = await send_periodic_athkar(
                context.application,
                chat_id,
                tz
            )

            if sent:
                await query.edit_message_text(
                    "✅ *تم ضبط معدل الإرسال بنجاح!*\n\n"
                    f"📿 سيتم إرسال ذكر كل *{interval} ساعة*.\n\n"
                    "✨ تم إرسال أول ذكر الآن، "
                    "وسيبدأ العد من وقت إرساله.",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(
                    "⚠️ تعذر إرسال الذكر حاليًا.",
                    parse_mode="Markdown"
                )

        else:
            await query.edit_message_text(
                "✅ *تم تغيير معدل الإرسال بنجاح!*\n\n"
                f"📿 كل *{interval} ساعة*.\n\n"
                "⏰ أنت حاليًا خارج ساعات العمل، "
                "وسيبدأ الإرسال عند دخول ساعات العمل.",
                parse_mode="Markdown"
            )

    # =====================================================
    # ساعات العمل العادية
    # =====================================================

    elif data == "set_hours_normal":

        save_or_update_chat(
            chat_id,
            update_data={
                "start_hour": 5,
                "end_hour": 2
            }
        )

        await query.edit_message_text(
            "✅ تم ضبط ساعات العمل:\n\n"
            "*من 5:00 صباحًا حتى 2:00 صباحًا*",
            parse_mode="Markdown"
        )

    # =====================================================
    # 24 ساعة
    # =====================================================

    elif data == "set_hours_24":

        save_or_update_chat(
            chat_id,
            update_data={
                "start_hour": 0,
                "end_hour": 24
            }
        )

        await query.edit_message_text(
            "✅ تم ضبط البوت ليعمل:\n\n"
            "*على مدار 24 ساعة بدون توقف*",
            parse_mode="Markdown"
        )

    # =====================================================
    # تشغيل / إيقاف
    # =====================================================

    elif data == "toggle_active":

        config = chats_col.find_one({
            "chat_id": chat_id
        })

        if not config:
            save_or_update_chat(chat_id)
            config = chats_col.find_one({
                "chat_id": chat_id
            })

        current_state = config.get(
            "is_active",
            True
        )

        new_state = not current_state

        update_data = {
            "is_active": new_state
        }

        # إذا تم تشغيل الإشعارات من جديد
        # نعيد ضبط عداد الذكر الدوري
        if new_state:
            update_data["last_athkar_sent"] = None

        save_or_update_chat(
            chat_id,
            update_data=update_data
        )

        if new_state:

            # إرسال ذكر مباشرة عند التشغيل
            config = get_chat_settings(chat_id)

            current_hour = datetime.now(tz).hour

            if is_within_working_hours(
                current_hour,
                config.get("start_hour", 5),
                config.get("end_hour", 2)
            ):
                sent = await send_periodic_athkar(
                    context.application,
                    chat_id,
                    tz
                )

                if sent:
                    msg = (
                        "🟢 *تم تشغيل الإشعارات بنجاح!*\n\n"
                        "✨ تم إرسال ذكر الآن، "
                        "وسيبدأ العد من وقت الإرسال."
                    )
                else:
                    msg = (
                        "🟢 تم تشغيل الإشعارات، "
                        "لكن تعذر إرسال الذكر الآن."
                    )

            else:
                msg = (
                    "🟢 *تم تشغيل الإشعارات بنجاح!*\n\n"
                    "⏰ أنت خارج ساعات العمل حاليًا، "
                    "وسيبدأ الإرسال عند دخول ساعات العمل."
                )

        else:
            msg = (
                "🔴 *تم إيقاف الإشعارات مؤقتاً.*"
            )

        await query.edit_message_text(
            msg,
            parse_mode="Markdown"
        )


# =========================================================
# 8. المجدول الرئيسي
# =========================================================

async def master_scheduler(application):

    tz = ZoneInfo("Asia/Riyadh")

    while True:

        try:

            now = datetime.now(tz)

            current_hour = now.hour
            current_minute = now.minute

            # جلب المحادثات المفعلة فقط
            chats = list(
                chats_col.find({
                    "is_active": {
                        "$ne": False
                    }
                })
            )

            for chat in chats:

                chat_id = chat["chat_id"]

                interval = chat.get(
                    "interval_hours",
                    1
                )

                start_h = chat.get(
                    "start_hour",
                    5
                )

                end_h = chat.get(
                    "end_hour",
                    2
                )

                # =================================================
                # أ) الأذكار الدورية
                # =================================================

                if is_within_working_hours(
                    current_hour,
                    start_h,
                    end_h
                ):

                    last_sent = parse_saved_datetime(
                        chat.get("last_athkar_sent"),
                        tz
                    )

                    should_send = False

                    # إذا لم يوجد إرسال سابق
                    if last_sent is None:
                        should_send = True

                    else:

                        next_send_time = (
                            last_sent
                            + timedelta(hours=interval)
                        )

                        if now >= next_send_time:
                            should_send = True

                    if should_send:

                        await send_periodic_athkar(
                            application,
                            chat_id,
                            tz
                        )

                # =================================================
                # ب) أذكار الصباح - 6 صباحًا
                # =================================================

                if current_hour == 6 and current_minute < 5:

                    today_str = now.strftime(
                        "%Y-%m-%d"
                    )

                    if chat.get(
                        "last_morning_sent"
                    ) != today_str:

                        try:

                            caption_text = (
                                "🌅 *أذكار الصباح*\n\n"
                                "أصبحنا وأصبح الملك لله، "
                                "والحمد لله ولا إله إلا الله."
                            )

                            if os.path.exists(
                                MORNING_IMAGE_PATH
                            ):

                                with open(
                                    MORNING_IMAGE_PATH,
                                    "rb"
                                ) as photo:

                                    await application.bot.send_photo(
                                        chat_id=chat_id,
                                        photo=photo,
                                        caption=caption_text,
                                        parse_mode="Markdown"
                                    )

                            else:

                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=caption_text,
                                    parse_mode="Markdown"
                                )

                            chats_col.update_one(
                                {
                                    "chat_id": chat_id
                                },
                                {
                                    "$set": {
                                        "last_morning_sent": today_str
                                    }
                                }
                            )

                        except (
                            Forbidden,
                            BadRequest
                        ):

                            remove_chat(chat_id)

                        except Exception as e:

                            logger.error(
                                f"خطأ أذكار الصباح "
                                f"{chat_id}: {e}"
                            )

                # =================================================
                # ج) أذكار المساء - 5 مساءً
                # =================================================

                if current_hour == 17 and current_minute < 5:

                    today_str = now.strftime(
                        "%Y-%m-%d"
                    )

                    if chat.get(
                        "last_evening_sent"
                    ) != today_str:

                        try:

                            caption_text = (
                                "🌆 *أذكار المساء*\n\n"
                                "أمسينا وأمسى الملك لله، "
                                "والحمد لله ولا إله إلا الله."
                            )

                            if os.path.exists(
                                EVENING_IMAGE_PATH
                            ):

                                with open(
                                    EVENING_IMAGE_PATH,
                                    "rb"
                                ) as photo:

                                    await application.bot.send_photo(
                                        chat_id=chat_id,
                                        photo=photo,
                                        caption=caption_text,
                                        parse_mode="Markdown"
                                    )

                            else:

                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=caption_text,
                                    parse_mode="Markdown"
                                )

                            chats_col.update_one(
                                {
                                    "chat_id": chat_id
                                },
                                {
                                    "$set": {
                                        "last_evening_sent": today_str
                                    }
                                }
                            )

                        except (
                            Forbidden,
                            BadRequest
                        ):

                            remove_chat(chat_id)

                        except Exception as e:

                            logger.error(
                                f"خطأ أذكار المساء "
                                f"{chat_id}: {e}"
                            )

                # =================================================
                # د) يوم الجمعة - سورة الكهف 9 صباحًا (مقطع صوتي)
                # =================================================
                # ملاحظة: send_audio يجعل تلجرام يجلب الملف من
                # الرابط ويعرضه كمقطع صوتي قابل للتشغيل داخل
                # المحادثة، وليس كنص فيه رابط. هذا الجزء يعمل
                # كما هو مطلوب تماماً ولم يتم تغييره.

                if (
                    now.weekday() == 4
                    and current_hour == 9
                    and current_minute < 5
                ):

                    today_str = now.strftime(
                        "%Y-%m-%d"
                    )

                    if chat.get(
                        "last_friday_sent"
                    ) != today_str:

                        try:

                            caption_text = (
                                "📖 *سورة الكهف | نورٌ ما بين الجمعتين*\n\n"
                                "✨ *صلوا على خير الأنام وآله ﷺ*"
                            )

                            await application.bot.send_audio(
                                chat_id=chat_id,
                                audio=SURAH_KAHF_AUDIO,
                                caption=caption_text,
                                parse_mode="Markdown"
                            )

                            chats_col.update_one(
                                {
                                    "chat_id": chat_id
                                },
                                {
                                    "$set": {
                                        "last_friday_sent": today_str
                                    }
                                }
                            )

                        except (
                            Forbidden,
                            BadRequest
                        ):

                            remove_chat(chat_id)

                        except Exception as e:

                            logger.error(
                                f"خطأ الجمعة "
                                f"{chat_id}: {e}"
                            )

                # إعطاء فرصة للـ event loop
                await asyncio.sleep(0.05)

        except Exception as e:

            logger.error(
                f"خطأ المجدول الرئيسي: {e}"
            )

        # فحص كل دقيقة
        await asyncio.sleep(60)


# =========================================================
# 9. التشغيل الرئيسي
# =========================================================

async def post_init(application):

    task = asyncio.create_task(
        master_scheduler(application)
    )

    BACKGROUND_TASKS.add(task)

    task.add_done_callback(
        BACKGROUND_TASKS.discard
    )


def main():

    keep_alive()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("stop", stop)
    )

    application.add_handler(
        CommandHandler("settings", settings)
    )

    application.add_handler(
        CommandHandler("stats", stats)
    )

    application.add_handler(
        CallbackQueryHandler(button_callback)
    )

    logger.info(
        "✅ بوت طُمأنينة يعمل الآن بنجاح..."
    )

    application.run_polling()


if __name__ == "__main__":
    main()
