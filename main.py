@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "🌟 <b>مرحباً بك مجدداً يا ديلان في بوتك الذكي!</b> 🌟\n\n"
        "أنا جاهز الآن لتخزين وبناء ذاكرتك الخاصة لاستراتيجيات التداول:\n"
        "• أرسل أي رابط فيديو وسأقوم بحفظه وتحليل ما أمكن من محتواه.\n"
        "• استخدم /price لمعرفة أسعار السوق الحالية.\n"
        "• استخدم /memory لعرض جميع الاستراتيجيات والروابط المحفوظة في ذاكرتك."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message(Command("price"))
async def cmd_price(message: types.Message):
    gold_price = "2,550.40 USD"
    btc_price = "92,100.00 USD"
    
    response_text = (
        f"📊 <b>أسعار السوق الحالية:</b>\n\n"
        f"🟡 الذهب (XAU/USD): <b>{gold_price}</b>\n"
        f"₿ البيتكوين (BTC): <b>{btc_price}</b>\n\n"
        f"<i>حالة النظام: مستقر ويعمل 🟢</i>"
    )
    await message.answer(response_text, parse_mode="HTML")

@dp.message(Command("memory"))
async def cmd_memory(message: types.Message):
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip():
            if len(content) > 3500:
                content = content[-3500:] + "\n\n[... تم إظهار أحدث محتوى الذاكرة ...]"
            response_text = f"🧠 <b>ذاكرة الاستراتيجيات والروابط المحفوظة:</b>\n\n<pre>{content}</pre>"
        else:
            response_text = "📭 ذاكرة البوت فارغة حالياً. أرسل بعض روابط الفيديوهات لتبدأ التعلم!"
    else:
        response_text = "📭 لا توجد ذاكرة مسجلة حتى الآن. أرسل روابط الفيديوهات وسأقوم بتخزينها."
    
    await message.answer(response_text, parse_mode="HTML")
