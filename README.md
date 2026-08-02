# 🤖 المخلافي — بوت AniList الذكي لـ Telegram (v3.0)

بوت ذكي يفهم اللغة الطبيعية بالعربية العامية لتحديث قائمة **AniList** تلقائياً، مبني على **Vercel Serverless** و **Google Gemini**. كل المنطق في ملف واحد `api/webhook.py` (Python stdlib فقط — بدون أي pip packages).

---

## ⚡ المميزات (v3.0)

### 🧠 فهم ذكي للمحادثة

- *"بدأت أشوف لورد الغوامض، شفت حلقتين"* ➔ الحلقة **2** (بداية جديدة = absolute، وليس جمع على التقدم السابق!)
- *"شفت 3 حلقات من ون بيس"* ➔ يضيف 3 للتقدم الحالي (`progress_delta`).
- *"خلصت Death Note وقيمته 9.5"* ➔ `COMPLETED` + تقييم 9.5.
- *"شفت الجزء الجديد من ري زيرو"* ➔ يبحث في AniList ويجيب **أحدث جزء** تلقائياً (لا يعتمد على معرفة الـ AI).

### ♻️ التصحيح والتراجع (جديد)

- *"صحّح التقدم إلى حلقة 2"* / *"كنت اقصد الجزء الرابع"* ➔ يصحّح آخر إجراء.
- *"تراجع"* أو `/undo` ➔ يرجّع آخر تعديل أو حذف.

### 💾 ذاكرة دائمة عبر الطلبات (جديد)

- يحفظ سياق المحادثة، التأكيدات المعلّقة ("نعم/لا")، وآخر إجراء — في **Upstash Redis** (اختياري) مع fallback للذاكرة.
- يحل مشاكل: عدم فهم "نعم"، الإشارات (ه/ها/هذا)، والتصحيحات.

### 📋 30+ إجراء

تتبع، حذف، إحصائيات، نشاطات، قوائم، أصدقاء، مقارنة، توصيات، ترند، مواسم، شخصيات، استديوهات، علاقات، مواعيد الحلقات، مفضلات، دفعات، فاجئني، أخبار، ومحادثة عامة.

---

## 🗝️ الحصول على الـ Tokens

### 1. AniList Access Token

👉 `https://anilist.co/api/v2/oauth/authorize?client_id=2699&response_type=token`
انسخ الـ `access_token` من رابط الصفحة بعد الموافقة.

### 2. Telegram Bot Token

1. ابحث عن `@BotFather` في تليجرام.
2. أرسل `/newbot` واتبع التعليمات.
3. انسخ الـ `HTTP API Token`.

### 3. Gemini API Key

من [Google AI Studio](https://aistudio.google.com/apikey).

### 4. (اختياري موصى به) Upstash Redis — للذاكرة الدائمة

Vercel Serverless لا يحتفظ بالذاكرة بين الطلبات. لإتاحة التصحيحات والتأكيدات متعددة الأدوار:

1. أنشئ قاعدة مجانية على [Upstash Console](https://console.upstash.com/redis).
2. انسخ `UPSTASH_REDIS_REST_URL` و `UPSTASH_REDIS_REST_TOKEN` (تبويب REST).
3. أضفهما في Environment Variables على Vercel.

- بدونها يعمل البوت بذاكرة مؤقتة (in-memory) وقد لا يتذكر السياق بين الطلبات.

> بديل: **Vercel KV** (مبني على Upstash) — يضبط `KV_REST_API_URL` و `KV_REST_API_TOKEN` تلقائياً.

---

## 🚀 النشر على Vercel

1. ارفع المشروع إلى GitHub.
2. في [Vercel](https://vercel.com) اضغط **New Project** واختر المستودع، ثم عيّن **Root Directory** = `anilist-bot`.
3. أضف Environment Variables: `TELEGRAM_BOT_TOKEN`, `ANILIST_ACCESS_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL`، و(اختيارياً) متغيرات Upstash.
4. اضغط **Deploy**.
5. فعّل الـ Webhook:
   `https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<APP>.vercel.app/api/webhook`

---

## 🧪 التشغيل المحلي / التشخيص

- افحص صحة البوت: `GET /api/webhook` ➔ حالة الـ tokens والنموذج ونوع التخزين.
- افحص Gemini: `GET /api/webhook?test=1`.

---

## 🛠️ البنية (ملف واحد: `api/webhook.py`)

- `[0-1]` تحميل .env + الإعدادات.
- `[2-3]` مساعدات null-safe + طبقة تخزين (Redis/in-memory).
- `[4]` عميل AniList GraphQL (+ حلّ الأجزاء `resolve_franchise_parts`).
- `[5]` عميل Telegram.
- `[6]` مساعدات العرض.
- `[7]` محلّل Arabic regex (fallback).
- `[8]` محلّل Gemini AI.
- `[9]` صنف المعالج (التوجيه، 30+ معالج، التصحيح، التراجع، الكول‌باك).
