<div dir="rtl">

# gametrans — ترجمه‌ی زنده‌ی متن بازی به فارسی

متنی که روی صفحه‌ی بازی است (زیرنویس، دیالوگ، توضیح آیتم) را می‌خواند، با هوش مصنوعی به فارسی ترجمه می‌کند، و همان‌جا روی بازی به‌صورت **راست‌به‌چپ** نشان می‌دهد — با کمترین تاخیر ممکن.

بدون نصب مود، بدون دست‌زدن به فایل‌های بازی. هر بازی‌ای که متن را روی صفحه بکشد کار می‌کند.

</div>

```
┌────────────── بازی (Borderless Windowed) ──────────────┐
│                                                        │
│                                                        │
│      ┌──────────────────────────────────────┐          │
│      │  "You must reach the castle before    │  ← ناحیه‌ی کپچر
│      │   nightfall, traveller."              │          │
│      └──────────────────────────────────────┘          │
│      ┌──────────────────────────────────────┐          │
│      │   .مسافر، باید پیش از شب به قلعه برسی │  ← اورلی فارسی
│      └──────────────────────────────────────┘          │
└────────────────────────────────────────────────────────┘
```

<div dir="rtl">

## تاخیر: چه انتظاری داشته باشید

| مرحله | زمان |
|---|---|
| کپچر ناحیه | ۱ تا ۳ میلی‌ثانیه |
| تشخیص تغییر فریم | زیر ۱ میلی‌ثانیه |
| OCR (ویندوز) | ۱۰ تا ۲۰ میلی‌ثانیه |
| انتظار برای تثبیت متن | ۰ تا ۲۲۰ میلی‌ثانیه (قابل تنظیم) |
| ترجمه تا اولین کلمه | ۲۰۰ تا ۵۰۰ میلی‌ثانیه |
| **جمع — خط جدید** | **حدود ۰٫۳ تا ۰٫۷ ثانیه** |
| **جمع — خط تکراری (از کش)** | **زیر ۵ میلی‌ثانیه** |

سه ترفندی که تاخیر را پایین نگه می‌دارد:

۱. **فقط ناحیه‌ی زیرنویس کپچر می‌شود**، نه کل صفحه.
۲. **فریم‌های بدون تغییر اصلاً وارد خط لوله نمی‌شوند** — در یک بازی ۶۰ فریمی، بیش از ۹۵٪ فریم‌ها دور ریخته می‌شوند پیش از آنکه به OCR برسند.
۳. **کش ترجمه** — متن‌های تکراری بازی (منو، نام آیتم، دیالوگ‌های تکراری) اصلاً به سرور نمی‌روند. کش روی دیسک ذخیره می‌شود، پس جلسه‌ی دوم همان بازی از ابتدا گرم است.

## نصب

نیازمندی: **پایتون ۳٫۹ یا بالاتر** ([python.org](https://www.python.org/downloads/) — هنگام نصب تیک «Add Python to PATH» را بزنید).

</div>

```powershell
git clone https://github.com/qaradagh/translator
cd translator

python -m venv .venv
.venv\Scripts\activate

# ویندوز — سریع‌ترین حالت (OCR داخلی ویندوز، بدون دانلود مدل)
pip install -e ".[windows,dxcam,hotkeys]"
```

<div dir="rtl">

اگر OCR ویندوز روی فونت بازی خوب کار نکرد، موتور دقیق‌تر را نصب کنید (حدود ۱۵ مگابایت مدل دانلود می‌کند):

</div>

```powershell
pip install -e ".[rapidocr]"
```

<div dir="rtl">

### گرفتن کلید رایگان

یکی کافی است، ولی هر دو را بگذارید تا وقتی یکی به سقف خورد خودکار برود سراغ بعدی:

| سرویس | کیفیت فارسی | سقف رایگان | لینک |
|---|---|---|---|
| **Gemini** | عالی | ۱۵ درخواست/دقیقه، حدود ۱۰۰۰–۱۵۰۰/روز | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| **Groq** | خوب | ۳۰ درخواست/دقیقه، ۱۴٬۴۰۰/روز | [console.groq.com/keys](https://console.groq.com/keys) |
| **Ollama** (لوکال) | متوسط | نامحدود، آفلاین، بدون محدودیت منطقه‌ای | [ollama.com](https://ollama.com/download) |

</div>

<div dir="rtl">

بعد کلید را ثبت کنید. **ساده‌ترین راه: روی فایل `START-HERE.bat` دوبار کلیک کنید** و از منو گزینه‌ی ۱ را بزنید.

اگر ترجیح می‌دهید دستور بزنید (کلید نمایش داده نمی‌شود و در تاریخچه هم نمی‌ماند):

</div>

```powershell
gametrans setkey gemini
gametrans setkey groq
```

<div dir="rtl">

این کلید را در فایل `.env` کنار پروژه ذخیره می‌کند. اگر ترجیح می‌دهید دستی انجام دهید،
همان فایل `.env` را با Notepad باز کنید و کلید را جلوی نامش بگذارید:

</div>

```ini
GEMINI_API_KEY=AIza...
GROQ_API_KEY=gsk_...
```

<div dir="rtl">

فایل `.env` در `.gitignore` هست، پس کلید هیچ‌وقت روی گیت‌هاب نمی‌رود.
اگر متغیر محیطی سیستم (`setx`) هم تنظیم شده باشد، آن اولویت دارد؛ `gametrans check`
نشان می‌دهد هر کلید از کدام منبع خوانده شده.

</div>

<div dir="rtl">

## اجرا

روی **`START-HERE.bat`** دوبار کلیک کنید. یک منو باز می‌شود:

</div>

```
  1  -  Add or change an API key          ثبت کلید
  2  -  Check that everything is ready    بررسی آمادگی
  3  -  Choose the subtitle area          انتخاب ناحیه‌ی زیرنویس
  4  -  START TRANSLATING                 شروع ترجمه
  5  -  Test one translation              تست یک ترجمه
  7  -  Update to the newest version      به‌روزرسانی (با یا بدون git)
```

<div dir="rtl">

بار اول به ترتیب ۱ ← ۲ ← ۳ ← ۴ را بزنید.

اگر ترمینال را ترجیح می‌دهید، همان کارها با دستور:

</div>

```powershell
gametrans check          # بررسی اینکه همه‌چیز آماده است
gametrans pick-region    # با ماوس روی ناحیه‌ی زیرنویس بکشید
gametrans run            # شروع
```

<div dir="rtl">

بازی را در حالت **Borderless Windowed** اجرا کنید، نه Fullscreen Exclusive — در حالت انحصاری، ویندوز اجازه نمی‌دهد هیچ پنجره‌ای روی بازی کشیده شود.

### کلیدهای میان‌بر

| کلید | کار |
|---|---|
| `Ctrl+Alt+P` | توقف / ادامه |
| `Ctrl+Alt+H` | نمایش / پنهان کردن اورلی |
| `Ctrl+Alt+R` | انتخاب دوباره‌ی ناحیه |
| `Ctrl+Alt+Q` | خروج |

### دستورهای دیگر

</div>

```powershell
gametrans translate --preview "You must reach the castle"   # تست ترجمه با پنجره‌ی پیش‌نمایش
gametrans setkey gemini                           # ثبت کلید در فایل .env
gametrans models                                  # لیست زنده‌ی مدل‌های هر سرویس
gametrans bench                                   # اندازه‌گیری تاخیر هر مرحله
gametrans monitors                                # فهرست مانیتورها
gametrans run --stats                             # نمایش زنده‌ی تاخیر روی اورلی
gametrans run --show-source                       # نمایش متن اصلی زیر ترجمه
```

<div dir="rtl">

سرویس‌ها گاهی نام مدل‌ها را منسوخ می‌کنند. اگر خطای «model not found» گرفتید،
`gametrans models` را بزنید و نام درست را از لیست زنده بردارید.

### دسترسی منطقه‌ای

Gemini و Groq در برخی کشورها — از جمله ایران — در دسترس نیستند و درخواست را رد می‌کنند.
اگر `gametrans check` کلید را می‌بیند ولی `gametrans translate` خطای دسترسی می‌دهد، دلیلش همین است.
در این حالت **مدل لوکال** کاملاً کار می‌کند و هیچ محدودیت منطقه‌ای و سقفی ندارد:

</div>

```powershell
# https://ollama.com/download نصب کنید، سپس:
ollama pull qwen3:8b
```

<div dir="rtl">

و در `config.toml` سرویس `ollama-local` را `enabled = true` کنید.

</div>

<div dir="rtl">

## تنظیم برای بازی خودتان

همه‌چیز در `config.toml` است. سه تنظیمی که بیشترین اثر را دارند:

**۱. سریع‌تر ولی با ریسک جمله‌ی نیمه:**

</div>

```toml
[stability]
frames_required = 1     # به‌محض خواندن، ترجمه کن
max_wait_ms     = 120
```

<div dir="rtl">

**۲. کیفیت ترجمه‌ی بهتر** — به مدل بگویید در چه بازی‌ای هستید:

</div>

```toml
[translate]
context_hint = "Elden Ring — فانتزی تاریک، لحن کهن و رسمی"

[translate.glossary]
"Site of Grace" = "جایگاه فیض"
"Estus Flask"   = "فلاسک اِستوس"
```

<div dir="rtl">

**۳. زیرنویس‌ها دیر تشخیص داده می‌شوند؟** آستانه‌ی تغییر را کم کنید:

</div>

```toml
[capture]
hash_threshold  = 2
pixel_threshold = 1.5
target_fps      = 20
```

<div dir="rtl">

## چطور کار می‌کند

</div>

```
                  ┌──────────────┐
   هر ۱/۱۲ ثانیه  │   کپچر ناحیه  │  mss یا dxcam · ۱ تا ۳ms
                  └──────┬───────┘
                         ▼
                  ┌──────────────┐
                  │ دروازه‌ی تغییر │  dHash + اختلاف پیکسلی · زیر ۱ms
                  └──────┬───────┘  ← بیش از ۹۵٪ فریم‌ها اینجا دور ریخته می‌شوند
                         ▼
                  ┌──────────────┐
                  │     OCR       │  Windows.Media.Ocr یا RapidOCR
                  └──────┬───────┘
                         ▼
                  ┌──────────────┐
                  │ تثبیت متن     │  صبر تا تمام‌شدن خط، با سقف زمانی
                  └──────┬───────┘
                         ▼
                  ┌──────────────┐
                  │   کش ترجمه    │  حافظه + SQLite · اصابت = زیر ۰٫۱ms
                  └──────┬───────┘
                         ▼ (فقط در صورت نبود در کش)
                  ┌──────────────┐
                  │ زنجیره‌ی مدل‌ها │  Gemini → Groq → لوکال
                  └──────┬───────┘  ← جابه‌جایی خودکار هنگام رسیدن به سقف
                         ▼
                  ┌──────────────┐
                  │  اورلی فارسی  │  Qt · شکل‌دهی حروف + دوجهته
                  └──────────────┘
```

<div dir="rtl">

نکته‌ی مهم درباره‌ی رندر فارسی: اورلی متن را از QTextLayout عبور می‌دهد، نه رسم مستقیم رشته — این تنها راهی است که حروف فارسی به‌درستی به هم بچسبند و جملات مختلط (فارسی + نام انگلیسی + عدد) درست چیده شوند. فونت [Vazirmatn](https://github.com/rastikerdar/vazirmatn) همراه پروژه است، پس ظاهر روی هر سیستمی یکسان است.

روی ویندوز، اورلی از دید APIهای کپچر صفحه پنهان می‌شود (`WDA_EXCLUDEFROMCAPTURE`) تا برنامه متن فارسی خودش را دوباره نخواند و در حلقه نیفتد. (عارضه‌ی جانبی: OBS هم اورلی را نمی‌بیند.)

## عیب‌یابی

| مشکل | راه‌حل |
|---|---|
| اورلی روی بازی دیده نمی‌شود | بازی را روی **Borderless Windowed** بگذارید |
| هیچ متنی خوانده نمی‌شود | `gametrans bench` را اجرا کنید و ببینید OCR چه می‌خواند؛ `upscale` را ۳ کنید |
| ترجمه‌ها ناقص‌اند | `frames_required` را ۳ کنید |
| «rate limited» در لاگ | کلید Groq را هم اضافه کنید تا زنجیره جابه‌جا شود |
| «model not found» | `gametrans models` را بزنید و نام درست را در config بگذارید |
| خطای دسترسی با کلید درست | سرویس در کشور شما در دسترس نیست — سراغ Ollama لوکال بروید |
| `MISSING $GEMINI_API_KEY` | `gametrans setkey gemini` را بزنید |
| کلید را گذاشتم ولی دیده نمی‌شود | `gametrans check` منبع کلید را نشان می‌دهد؛ فایل باید `.env` باشد نه `.env.txt` |
| `'gametrans' is not recognized` | در پنجره‌ی معمولی cmd دستور کار نمی‌کند — از `START-HERE.bat` استفاده کنید |
| `git pull` خطا می‌دهد | فایل‌ها را ZIP دانلود کرده‌اید نه clone. از منو گزینه‌ی ۷ را بزنید |
| کلیدهای میان‌بر کار نمی‌کنند | اگر بازی با دسترسی مدیر اجرا می‌شود، برنامه را هم Run as administrator کنید |
| فارسی بریده‌بریده است | یعنی فونت لود نشده — پوشه‌ی `assets/fonts` باید کنار پروژه باشد |
| در پنجره‌ی مشکی فارسی برعکس است | طبیعی است؛ cmd متن راست‌به‌چپ را پشتیبانی نمی‌کند. اورلی درست است — با `--preview` ببینید |
| مصرف CPU زیاد | `target_fps` را ۸ کنید |

## توسعه

</div>

```bash
pip install -e ".[dev]"
pytest -q          # ۱۴۶ تست، بدون نیاز به شبکه یا صفحه‌نمایش
```

<div dir="rtl">

تست‌ها همه‌ی منطق خالص را پوشش می‌دهند: دروازه‌ی تغییر فریم، نرمال‌سازی متن، کش، تثبیت، جابه‌جایی بین مدل‌ها، و خط لوله‌ی کامل با کپچر و OCR شبیه‌سازی‌شده. تست‌های رندر فارسی به‌صورت headless اجرا می‌شوند و شکل‌دهی حروف و چینش دوجهته را واقعاً بررسی می‌کنند.

## پروانه

کد تحت MIT. فونت Vazirmatn تحت SIL Open Font License (نسخه در `assets/fonts/OFL.txt`).

</div>

---

## English

Real-time on-screen game text translator with a Persian (RTL) overlay. Captures only the subtitle region, skips unchanged frames before they reach OCR, caches translations to disk, and streams from a failover chain of free AI providers (Gemini → Groq → local model), so a new line lands in roughly 0.3–0.7 s and a repeated line in under 5 ms.

```powershell
pip install -e ".[windows,dxcam,hotkeys]"
gametrans setkey gemini            # free key: https://aistudio.google.com/apikey
gametrans check && gametrans pick-region && gametrans run
```

Run the game in **borderless windowed** mode. See `config.example.toml` for every tunable, and `gametrans --help` for all commands.
