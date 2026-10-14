# 05 — التشغيل اليومي واستكشاف الأعطال

كل الأوامر على السيرفر كـ root من مجلد النشر:

```bash
cd /srv/repeater/app/deploy
alias dc='docker compose --env-file .env'      # اختصار للجلسة الحالية
```

## الحالة والسجلات

| المطلوب | الأمر |
|---|---|
| حالة الخدمات وسلامتها | `dc ps` |
| سجلات حية | `dc logs -f --tail 200 web worker` |
| أخطاء العامل فقط (سطور JSON) | `dc logs --no-color worker \| grep '"level": "error"'` |
| سجل Caddy (الشهادات) | `dc logs --tail 100 caddy` |
| فحص السلامة | `curl -s http://127.0.0.1:8090/healthz` (المنفذ من `REPEATER_WEB_PORT`) |
| فحص الإعدادات | `dc exec web python -m repeater.cli check` |
| الموارد | `docker stats --no-stream` |
| المساحة | `df -h /srv && du -sh /srv/repeater/media /srv/repeater/backups` |
| سجل النسخ الاحتياطي | `journalctl -t repeater-backup --since yesterday` |

السجلات تُدوَّر تلقائيًا (10 ميجا × 5 ملفات لكل خدمة). بدون SSH: **Actions ← Server inspect** يطبع آخر 100 سطر من web وworker.

## إعادة التشغيل والتحديث

- إعادة تشغيل اللوحة والعامل: `dc restart web worker`
- نشر الكود الحالي من جديد: `bash deploy.sh`
- تحديث عادي: ادفع إلى `main` (أو **Actions ← Deploy ← Run workflow**).
- بعد تعديل `deploy/.env` يدويًا: `bash deploy.sh` (يعيد إنشاء الحاويات التي تغيرت إعداداتها).
- العامل يُكمل المهمة الجارية (ترميز أو رفع) قبل أن يتوقف، حتى 3 دقائق.

## تحديث yt-dlp وصور الأساس

- yt-dlp يتحدث تلقائيًا مع **أول نشر في كل يوم** (طبقة مستقلة في الصورة تُعاد بناؤها فقط).
- تحديثه الآن (مثلًا بعد عطل في تيكتوك/يوتيوب): `bash deploy.sh --refresh-ytdlp`
- تحديث صور الأساس (Python، Postgres، Caddy) لإصلاحات أمنية، مرة شهريًا: `bash deploy.sh --pull`
- الصورة تتضمن Deno لأن yt-dlp يحتاج محرك JavaScript لتنزيلات يوتيوب.

## الرجوع للإصدار السابق

كل بناء يحفظ الصورة السابقة باسم `repeater-app:previous`:

```bash
REPEATER_IMAGE_TAG=previous bash deploy.sh --no-build
```

تنبيه: لو أضاف الإصدار الجديد ترحيلًا للقاعدة، قد لا يعمل الإصدار القديم عليها؛ الحل حينها نسخة احتياطية سابقة للنشر (`backup.sh restore`).

## النسخ الاحتياطي

التفاصيل في [00-deploy.md](00-deploy.md#8-النسخ-الاحتياطي-والاسترجاع). باختصار:

```bash
bash backup.sh                                              # نسخة الآن
ls -lt /srv/repeater/backups | head                              # آخر النسخ
bash backup.sh restore /srv/repeater/backups/<file>.sql.gz --yes
```

## الأسرار والمفاتيح

- تغيير مفاتيح منصة: عدّل `deploy/.env` أو سر GitHub ثم انشر.
- تدوير مفتاح تشفير التوكنات: أضف مفتاحًا جديدًا **في أول** `REPEATER_TOKEN_ENCRYPTION_KEYS` مع إبقاء القديم بعده (`k2:جديد,k1:قديم`)، ثم انشر. لا تحذف المفتاح القديم ما دامت توكنات مشفرة به. لتوليد مفتاح: `dc exec web python -m repeater.cli gen-secrets`.
- لا تغيّر `POSTGRES_PASSWORD` في `.env` وحده: القاعدة الموجودة ستبقى على القديمة.

## استكشاف الأعطال

| العَرَض | السبب المرجّح | الحل |
|---|---|---|
| المتصفح: لا يفتح `rotator.magic-lap.com` | سجل DNS غير موجود أو برتقالي، أو جدار Hostinger يغلق 80/443 | راجع سجل A (DNS only)، ثم `dc logs caddy` |
| خطأ شهادة / Caddy لا يستخرج شهادة | الـ DNS لا يشير للسيرفر بعد، أو المنفذ 80 مغلق | `dig +short rotator.magic-lap.com`، وانتظر انتشار DNS؛ Caddy يعيد المحاولة وحده |
| 502 من Caddy أو البروكسي | حاوية web متوقفة أو غير سليمة | `dc ps` و`dc logs --tail 100 web` |
| 413 عند رفع فيديو | بروكسي Cloudflare مفعّل (حد 100 ميجا) أو حد البروكسي الخارجي | اجعل السجل DNS only؛ في nginx: `client_max_body_size 600m` |
| العامل unhealthy | ملف النبض قديم: العامل عالق أو القاعدة متوقفة | `dc logs --tail 200 worker` ثم `dc restart worker` |
| `Permission denied` على الوسائط | مجلد الوسائط ليس للمستخدم 10001 | `chown -R 10001:10001 /srv/repeater/media` |
| `migrate` يفشل عند النشر | خطأ ترحيل؛ الإصدار القديم يبقى شغالًا | اقرأ الخطأ في سجل النشر وأصلحه ثم انشر مجددًا |
| `password authentication failed` للقاعدة | `POSTGRES_PASSWORD` في `.env` لا يطابق القاعدة | أرجع `.env` من النسخة المحفوظة |
| انستجرام: فشل تنزيل الوسائط | خوادم ميتا لا تصل لرابط `/m/...` | السجل DNS only، و`curl -I` للرابط من خارج السيرفر |
| تيكتوك: `unaudited_client_can_only_post_to_private_accounts` | التطبيق غير مدقق والحساب عام | انشر على حساب خاص حتى التدقيق ([01-tiktok.md](01-tiktok.md)) |
| يوتيوب/Drive يطلب إعادة ربط كل أسبوع | تطبيق Google في وضع Testing (7 أيام) | أعد الربط، أو Internal/Production ([02-google.md](02-google.md)) |
| yt-dlp: `Sign in to confirm you're not a bot` | يوتيوب يحجب عناوين مراكز البيانات | ملف كوكيز للمصدر في `REPEATER_YTDLP_COOKIES_DIR` أو `REPEATER_YTDLP_PROXY`، أو الرفع اليدوي/Drive |
| المنفذ 8090 مستخدم | خدمة أخرى عليه | غيّر `REPEATER_WEB_PORT` في `.env` وفي إعداد البروكسي، ثم `deploy.sh` |
| لا تصل تنبيهات تيليجرام | توكن أو chat id خطأ | [04-telegram.md](04-telegram.md) |
