import asyncio
import os
import random
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from pymongo import MongoClient

# --- 1. خادم Flask لإبقاء السيرفر صاحياً عبر Render ---
app_web = Flask('')

@app_web.route('/')
def home():
    return "Bot is running perfectly!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# --- 2. إعدادات البوت والوسائط وقاعدة البيانات ---
BOT_TOKEN = "8867227824:AAFTkVDZ6ziSQgXmsDOK4Nzw6a3gP0E3wQU"

# رابط قراءة هادئة لسورة الكهف (القارئ هزاع البلوشي)
SURAH_KAHF_AUDIO = "https://server14.mp3quran.net/hazza/018.mp3"

# أسماء ملفات الصور المحلية المرفوعة على GitHub
MORNING_IMAGE_PATH = "morning.jpg"
EVENING_IMAGE_PATH = "evening.jpg"

# مجموعة لحفظ مرجع المهام بالخلفية لمنع إيقافها تلقائياً
BACKGROUND_TASKS = set()

MONGO_URI = os.environ.get("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client['azkar_bot_db']
chats_col = db['chats']

# --- 3. وظائف قاعدة البيانات ---
def save_or_update_chat(chat_id, update_data=None):
    default_data = {
        "chat_id": chat_id,
        "interval_hours": 1,
        "start_hour": 5,
        "end_hour": 2,
        "last_sent_hour": None,
        "last_morning_sent": None,
        "last_evening_sent": None,
        "last_friday_sent": None
    }
    
    existing = chats_col.find_one({"chat_id": chat_id})
    if not existing:
        chats_col.insert_one(default_data)
    elif update_data:
        chats_col.update_one({"chat_id": chat_id}, {"$set": update_data})

def get_chat_settings(chat_id):
    chat = chats_col.find_one({"chat_id": chat_id})
    if not chat:
        save_or_update_chat(chat_id)
        return chats_col.find_one({"chat_id": chat_id})
    return chat

def is_within_working_hours(current_hour, start_hour=5, end_hour=2):
    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour
    else:
        return current_hour >= start_hour or current_hour < end_hour

# --- 4. قائمة الأذكار ---
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

# --- 5. أوامر البوت والتفاعل ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_or_update_chat(chat_id)
    
    await update.message.reply_text(
        "🌿 **تم تفعيل بوت طُمأنينة بنجاح!**\n\n"
        "سيقوم البوت بإرسال الأذكار دورياً، وأذكار الصباح والمساء، وسورة الكهف يوم الجمعة.\n"
        "⚙️ للتحكم بالإعدادات والتكرار، أرسل: /settings",
        parse_mode='Markdown'
    )

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = get_chat_settings(chat_id)
    
    keyboard = [
        [
            InlineKeyboardButton("كل ساعة", callback_data="set_int_1"),
            InlineKeyboardButton("كل ساعتين", callback_data="set_int_2")
        ],
        [
            InlineKeyboardButton("كل 3 ساعات", callback_data="set_int_3"),
            InlineKeyboardButton("كل 4 ساعات", callback_data="set_int_4")
        ],
        [
            InlineKeyboardButton("⏰ ساعات العمل (5 ص - 2 م)", callback_data="set_hours_normal"),
            InlineKeyboardButton("🌐 24 ساعة بدون إيقاف", callback_data="set_hours_24")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        "⚙️ **إعدادات بوت طُمأنينة:**\n\n"
        f"• **معدل تكرار الأذكار:** كل {config.get('interval_hours', 1)} ساعة\n"
        f"• **نطاق ساعات العمل:** من {config.get('start_hour', 5)}:00 صباحاً إلى {config.get('end_hour', 2)}:00 ليلاً\n\n"
        "إختر من الأزرار للتعديل:"
    )
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data.startswith("set_int_"):
        interval = int(data.split("_")[2])
        save_or_update_chat(chat_id, {"interval_hours": interval})
        await query.edit_message_text(f"✅ تم تغيير معدل الإرسال إلى: **كل {interval} ساعات**.", parse_mode='Markdown')
        
    elif data == "set_hours_normal":
        save_or_update_chat(chat_id, {"start_hour": 5, "end_hour": 2})
        await query.edit_message_text("✅ تم ضبط ساعات العمل: **من 5:00 صباحاً حتى 2:00 ليلاً**.", parse_mode='Markdown')
        
    elif data == "set_hours_24":
        save_or_update_chat(chat_id, {"start_hour": 0, "end_hour": 24})
        await query.edit_message_text("✅ تم ضبط البوت ليعمل **على مدار 24 ساعة بدون توقف**.", parse_mode='Markdown')

# --- 6. محرك الجدولة المتقدم (توقيت مكة المكرمة) ---
async def master_scheduler(app):
    tz = ZoneInfo("Asia/Riyadh")
    
    while True:
        try:
            now = datetime.now(tz)
            today_str = now.strftime("%Y-%m-%d")
            current_hour = now.hour
            current_minute = now.minute

            chats = list(chats_col.find())
            for chat in chats:
                chat_id = chat["chat_id"]
                interval = chat.get("interval_hours", 1)
                start_h = chat.get("start_hour", 5)
                end_h = chat.get("end_hour", 2)

                # أ) الأذكار الدورية
                if is_within_working_hours(current_hour, start_h, end_h):
                    if current_hour % interval == 0:
                        hour_key = f"{today_str}_{current_hour}"
                        if chat.get("last_sent_hour") != hour_key:
                            text = random.choice(ATHKAR_LIST)
                            try:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"✨ **طُمأنينة:**\n\n{text}",
                                    parse_mode='Markdown'
                                )
                                chats_col.update_one({"chat_id": chat_id}, {"$set": {"last_sent_hour": hour_key}})
                            except Exception as e:
                                print(f"خطأ إرسال الذكر {chat_id}: {e}")

                # ب) أذكار الصباح (6:00 صباحاً) - استخدام الصورة المرفوعة محلياً
                if current_hour == 6 and current_minute < 5:
                    if chat.get("last_morning_sent") != today_str:
                        try:
                            caption_text = "🌅 **أذكار الصباح**\n\nأصبحنا وأصبح الملك لله، والحمد لله ولا إله إلا الله."
                            if os.path.exists(MORNING_IMAGE_PATH):
                                with open(MORNING_IMAGE_PATH, 'rb') as photo:
                                    await app.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption_text, parse_mode='Markdown')
                            else:
                                await app.bot.send_message(chat_id=chat_id, text=caption_text, parse_mode='Markdown')
                            chats_col.update_one({"chat_id": chat_id}, {"$set": {"last_morning_sent": today_str}})
                        except Exception as e:
                            print(f"خطأ أذكار الصباح {chat_id}: {e}")

                # ج) أذكار المساء (5:00 مساءً) - استخدام الصورة المرفوعة محلياً
                if current_hour == 17 and current_minute < 5:
                    if chat.get("last_evening_sent") != today_str:
                        try:
                            caption_text = "إليك الكود المكتمل بجميع تفاصيله وخالٍ تماماً من أي أخطاء مصنعية أو نصوص منقطعة. 

انسخ الكود كاملاً من المربع أدناه واستبدل به محتوى ملف `main.py` في GitHub لتجاوز خطأ التنسيق فوراً:

```python
import asyncio
import os
import random
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from pymongo import MongoClient

# --- 1. خادم Flask لإبقاء السيرفر صاحياً عبر Render ---
app_web = Flask('')

@app_web.route('/')
def home():
    return "Bot is running perfectly!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# --- 2. إعدادات البوت والوسائط وقاعدة البيانات ---
BOT_TOKEN = "8867227824:AAFTkVDZ6ziSQgXmsDOK4Nzw6a3gP0E3wQU"

# رابط قراءة هادئة لسورة الكهف (القارئ هزاع البلوشي)
SURAH_KAHF_AUDIO = "[https://server14.mp3quran.net/hazza/018.mp3](https://server14.mp3quran.net/hazza/018.mp3)"

# أسماء ملفات الصور المحلية المرفوعة على GitHub
MORNING_IMAGE_PATH = "morning.jpg"
EVENING_IMAGE_PATH = "evening.jpg"

# مجموعة لحفظ مرجع المهام بالخلفية لمنع إيقافها تلقائياً
BACKGROUND_TASKS = set()

MONGO_URI = os.environ.get("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client['azkar_bot_db']
chats_col = db['chats']

# --- 3. وظائف قاعدة البيانات ---
def save_or_update_chat(chat_id, update_data=None):
    default_data = {
        "chat_id": chat_id,
        "interval_hours": 1,
        "start_hour": 5,
        "end_hour": 2,
        "last_sent_hour": None,
        "last_morning_sent": None,
        "last_evening_sent": None,
        "last_friday_sent": None
    }
    
    existing = chats_col.find_one({"chat_id": chat_id})
    if not existing:
        chats_col.insert_one(default_data)
    elif update_data:
        chats_col.update_one({"chat_id": chat_id}, {"$set": update_data})

def get_chat_settings(chat_id):
    chat = chats_col.find_one({"chat_id": chat_id})
    if not chat:
        save_or_update_chat(chat_id)
        return chats_col.find_one({"chat_id": chat_id})
    return chat

def is_within_working_hours(current_hour, start_hour=5, end_hour=2):
    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour
    else:
        return current_hour >= start_hour or current_hour < end_hour

# --- 4. قائمة الأذكار ---
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
    "اللَّهُمَّ اكْفِنِي بِحَلالِكَ عَنْ حَرَامِكَ، وَأَغْنِني بِفَضْلِكَ عَمَّنْ سِوَاكَ.",
    "اللَّهُمَّ إِنِّي أَسْأَلُكَ عِلْماً نَافِعاً، وَرِزْقاً طَيِّباً، وَعَمَلاً مُتَقَبَّلاً.",
    "اللَّهُمَّ إِنِّي أَعُوذُ بِكَ مِنْ زَوَالِ نِعْمَتِكَ، وَتَحَوُّلِ عَافِيَتِكَ، وَفُجَاءَةِ نِقْمَتِكَ، وَجَمِيعِ سَخَطِكَ.",
    "اللَّهُمَّ أحْسِنْ عَاقِبَتَنَا فِي الأُمُورِ كُلِّهَا، وَأَجِرْنَا مِنْ خِزْيِ الدُّنْيَا وَعَذَابِ الآخِرَةِ.",
    "اللَّهُمَّ آتِ نُفُوسَنَا تَقْوَاهَا، وَزَكِّهَا أَنْتَ خَيْرُ مَنْ زَكَّاهَا، أَنْتَ وَلِيُّهَا وَمَوْلاهَا."
]

# --- 5. أوامر البوت والتفاعل ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_or_update_chat(chat_id)
    
    await update.message.reply_text(
        "🌿 **تم تفعيل بوت طُمأنينة بنجاح!**\n\n"
        "سيقوم البوت بإرسال الأذكار دورياً، وأذكار الصباح والمساء، وسورة الكهف يوم الجمعة.\n"
        "⚙️ للتحكم بالإعدادات والتكرار، أرسل: /settings",
        parse_mode='Markdown'
    )

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = get_chat_settings(chat_id)
    
    keyboard = [
        [
            InlineKeyboardButton("كل ساعة", callback_data="set_int_1"),
            InlineKeyboardButton("كل ساعتين", callback_data="set_int_2")
        ],
        [
            InlineKeyboardButton("كل 3 ساعات", callback_data="set_int_3"),
            InlineKeyboardButton("كل 4 ساعات", callback_data="set_int_4")
        ],
        [
            InlineKeyboardButton("⏰ ساعات العمل (5 ص - 2 م)", callback_data="set_hours_normal"),
            InlineKeyboardButton("🌐 24 ساعة بدون إيقاف", callback_data="set_hours_24")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        "⚙️ **إعدادات بوت طُمأنينة:**\n\n"
        f"• **معدل تكرار الأذكار:** كل {config.get('interval_hours', 1)} ساعة\n"
        f"• **نطاق ساعات العمل:** من {config.get('start_hour', 5)}:00 صباحاً إلى {config.get('end_hour', 2)}:00 ليلاً\n\n"
        "إختر من الأزرار للتعديل:"
    )
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data.startswith("set_int_"):
        interval = int(data.split("_")[2])
        save_or_update_chat(chat_id, {"interval_hours": interval})
        await query.edit_message_text(f"✅ تم تغيير معدل الإرسال إلى: **كل {interval} ساعات**.", parse_mode='Markdown')
        
    elif data == "set_hours_normal":
        save_or_update_chat(chat_id, {"start_hour": 5, "end_hour": 2})
        await query.edit_message_text("✅ تم ضبط ساعات العمل: **من 5:00 صباحاً حتى 2:00 ليلاً**.", parse_mode='Markdown')
        
    elif data == "set_hours_24":
        save_or_update_chat(chat_id, {"start_hour": 0, "end_hour": 24})
        await query.edit_message_text("✅ تم ضبط البوت ليعمل **على مدار 24 ساعة بدون توقف**.", parse_mode='Markdown')

# --- 6. محرك الجدولة المتقدم (توقيت مكة المكرمة) ---
async def master_scheduler(app):
    tz = ZoneInfo("Asia/Riyadh")
    
    while True:
        try:
            now = datetime.now(tz)
            today_str = now.strftime("%Y-%m-%d")
            current_hour = now.hour
            current_minute = now.minute

            chats = list(chats_col.find())
            for chat in chats:
                chat_id = chat["chat_id"]
                interval = chat.get("interval_hours", 1)
                start_h = chat.get("start_hour", 5)
                end_h = chat.get("end_hour", 2)

                # أ) الأذكار الدورية
                if is_within_working_hours(current_hour, start_h, end_h):
                    if current_hour % interval == 0:
                        hour_key = f"{today_str}_{current_hour}"
                        if chat.get("last_sent_hour") != hour_key:
                            text = random.choice(ATHKAR_LIST)
                            try:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"✨ **طُمأنينة:**\n\n{text}",
                                    parse_mode='Markdown'
                                )
                                chats_col.update_one({"chat_id": chat_id}, {"$set": {"last_sent_hour": hour_key}})
                            except Exception as e:
                                print(f"خطأ إرسال الذكر {chat_id}: {e}")

                # ب) أذكار الصباح (6:00 صباحاً) - استخدام الصورة المرفوعة محلياً
                if current_hour == 6 and current_minute < 5:
                    if chat.get("last_morning_sent") != today_str:
                        try:
                            caption_text = "🌅 **أذكار الصباح**\n\nأصبحنا وأصبح الملك لله، والحمد لله ولا إله إلا الله."
                            if os.path.exists(MORNING_IMAGE_PATH):
                                with open(MORNING_IMAGE_PATH, 'rb') as photo:
                                    await app.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption_text, parse_mode='Markdown')
                            else:
                                await app.bot.send_message(chat_id=chat_id, text=caption_text, parse_mode='Markdown')
                            chats_col.update_one({"chat_id": chat_id}, {"$set": {"last_morning_sent": today_str}})
                        except Exception as e:
                            print(f"خطأ أذكار الصباح {chat_id}: {e}")

                # ج) أذكار المساء (5:00 مساءً) - استخدام الصورة المرفوعة محلياً
                if current_hour == 17 and current_minute < 5:
                    if chat.get("last_evening_sent") != today_str:
                        try:
                            caption_text = "🌆 **أذكار المساء**\n\nأمسينا وأمسى الملك لله، والحمد لله ولا إله إلا الله."
                            if os.path.exists(EVENING_IMAGE_PATH):
                                with open(EVENING_IMAGE_PATH, 'rb') as photo:
                                    await app.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption_text, parse_mode='Markdown')
                            else:
                                await app.bot.send_message(chat_id=chat_id, text=caption_text, parse_mode='Markdown')
                            chats_col.update_one({"chat_id": chat_id}, {"$set": {"last_evening_sent": today_str}})
                        except Exception as e:
                            print(f"خطأ أذكار المساء {chat_id}: {e}")

                # د) يوم الجمعة - سورة الكهف صوتية (9:00 صباحاً)
                if now.weekday() == 4 and current_hour == 9 and current_minute < 5:
                    if chat.get("last_friday_sent") != today_str:
                        try:
                            caption_text = (
                                "📖 **سورة الكهف | نورٌ ما بين الجمعتين**\n\n"
                                "✨ **صلوا على خير الأنام وآله ﷺ**"
                            )
                            await app.bot.send_audio(
                                chat_id=chat_id,
                                audio=SURAH_KAHF_AUDIO,
                                caption=caption_text,
                                parse_mode='Markdown'
                            )
                            chats_col.update_one({"chat_id": chat_id}, {"$set": {"last_friday_sent": today_str}})
                        except Exception as e:
                            print(f"خطأ الجمعة {chat_id}: {e}")

        except Exception as e:
            print(f"خطأ المجدول الرئيسي: {e}")

        await asyncio.sleep(60)

async def post_init(app):
    task = asyncio.create_task(master_scheduler(app))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)

# --- 7. التشغيل الرئيسي ---
def main():
    keep_alive()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("بوت طُمأنينة يعمل الآن بنجاح...")
    app.run_polling()

if __name__ == '__main__':
    main()
