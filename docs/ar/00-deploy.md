# 00 — نشر Repeater على سيرفر Hostinger (rotator.magic-lap.com)

## الصورة باختصار

- الموقع الرئيسي `magic-lap.com` على استضافة Hostinger العادية (hPanel) خلف Cloudflare، ولا نلمسه.
- Repeater يعمل على الـ VPS (KVM 4) بـ Docker Compose داخل `/srv/repeater`، على الساب دومين **`rotator.magic-lap.com`**.
- الخدمات: `db` (PostgreSQL 16) و`migrate` (ترحيلات القاعدة) و`web` (اللوحة) و`worker` (الاستيراد والترميز والنشر) و`caddy` (HTTPS تلقائي، يعمل فقط لو المنفذان 80/443 فاضيان).
- الحصة: نحو 2 vCPU و6 جيجا رام كحد أقصى، حتى لا يزاحم مشاريع Magic Lap الأخرى على نفس السيرفر.
- الملفات على السيرفر:

| المسار | المحتوى |
|---|---|
| `/srv/repeater/app` | الكود (يُحدَّث مع كل نشر) |
| `/srv/repeater/app/deploy/.env` | كل الأسرار والإعدادات (صلاحيات 600، لا يدخل Git أبدًا) |
| `/srv/repeater/media` | مكتبة الفيديوهات (مملوكة للمستخدم 10001) |
| `/srv/repeater/backups` | نسخ القاعدة اليومية |

## 1. سجل DNS في Cloudflare

1. ادخل https://dash.cloudflare.com ← اختر `magic-lap.com` ← **DNS** ← **Records** ← **Add record**.
2. القيم:
   - **Type:** `A`
   - **Name:** `rotator`
   - **IPv4 address:** عنوان الـ VPS (من hPanel ← VPS ← صفحة السيرفر).
   - **Proxy status:** **DNS only** (السحابة رمادية) ← مهم.
   - **TTL:** Auto
3. احفظ، وتأكد من جهازك: `dig +short rotator.magic-lap.com` يجب أن يطبع عنوان الـ VPS نفسه (لا عناوين Cloudflare).

**لماذا رمادية (بدون بروكسي Cloudflare)؟**

- Cloudflare في الخطتين المجانية وPro يرفض أي طلب جسمه أكبر من **100 ميجا** (خطأ 413)، ورفع الفيديو من اللوحة يصل إلى 500 ميجا.
- انستجرام ينشر الريل بأن تسحب خوادم ميتا ملف الفيديو من رابطنا الموقّع (`/m/...`). حماية البوتات والتحديات والكاش في Cloudflare قد تحجب هذا السحب أو تؤخره فيفشل النشر. وكذلك أي منصة نجعلها لاحقًا تسحب من رابط (مثل `PULL_FROM_URL` في تيكتوك).
- Caddy يستخرج شهادة Let's Encrypt بنفسه ويحتاج أن تصله الطلبات مباشرة.

لا تغيّر أي سجل آخر للدومين الرئيسي.

## 2. مفتاح SSH للنشر الآلي

على جهازك (لا على السيرفر):

```bash
ssh-keygen -t ed25519 -C "github-deploy-repeater" -f repeater_deploy -N ""
```

- ضع محتوى `repeater_deploy.pub` في السيرفر: من hPanel ← VPS ← **SSH keys** ← Add SSH key، أو بإضافته إلى `/root/.ssh/authorized_keys`.
- المستخدم: `root` (افتراضي Hostinger)، أو مستخدم مخصص له `sudo` بدون كلمة مرور.
- بصمة السيرفر (تمنع انتحال السيرفر): من جهازك `ssh-keyscan -p 22 <IP>` وانسخ الناتج كاملًا إلى السر `VPS_KNOWN_HOSTS`. للتأكد قارن البصمة بناتج `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` من الطرفية في hPanel.

## 3. أسرار GitHub

في المستودع: **Settings ← Secrets and variables ← Actions ← New repository secret**:

| السر | القيمة | إلزامي |
|---|---|---|
| `VPS_HOST` | عنوان IP للـ VPS | نعم |
| `VPS_USER` | `root` (أو المستخدم المخصص) | نعم |
| `VPS_SSH_KEY` | محتوى الملف الخاص `repeater_deploy` كاملًا (من `-----BEGIN` إلى `-----END`) | نعم |
| `REPEATER_DOMAIN` | `rotator.magic-lap.com` | نعم |
| `VPS_PORT` | منفذ SSH إن لم يكن 22 | لا |
| `VPS_KNOWN_HOSTS` | ناتج `ssh-keyscan` (بدونه يثق النشر بأول مفتاح يراه ويطبع تحذيرًا) | موصى به |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | حساب مدير اللوحة (كلمة المرور 10 أحرف على الأقل) | أول نشر |
| `REPEATER_TIKTOK_CLIENT_KEY` / `REPEATER_TIKTOK_CLIENT_SECRET` | من [01-tiktok.md](01-tiktok.md) | لا |
| `REPEATER_GOOGLE_CLIENT_ID` / `REPEATER_GOOGLE_CLIENT_SECRET` | من [02-google.md](02-google.md) | لا |
| `REPEATER_META_APP_ID` / `REPEATER_META_APP_SECRET` | من [03-meta.md](03-meta.md) | لا |
| `REPEATER_TELEGRAM_BOT_TOKEN` / `REPEATER_TELEGRAM_CHAT_ID` | من [04-telegram.md](04-telegram.md) | لا |
| `REPEATER_YTDLP_PROXY` | بروكسي التنزيل غير الرسمي إن وُجد | لا |

مفاتيح المنصات وتيليجرام: كل نشر ينسخ الموجود منها إلى `deploy/.env` على السيرفر (عبر stdin في SSH، لا تظهر في السجل). ما لا تضعه هنا يبقى كما هو في `.env`.

## 4. افحص السيرفر أولًا (موصى به)

**Actions ← Server inspect ← Run workflow**. يقرأ فقط ولا يغيّر شيئًا، ويطبع: النظام والموارد، Docker، المنافذ المفتوحة، **من يخدم 80/443** (nginx/Traefik/Coolify...)، وحالة `/srv/repeater`.

## 5. أول نشر

### الطريقة أ: GitHub Actions (لا تحتاج دخول السيرفر)

**Actions ← Deploy ← Run workflow** (ref = `main`)، أو ادفع إلى `main`. يشغّل CI أولًا، ثم ينسخ الكود إلى `/srv/repeater/app`، ولأن التثبيت لم يكتمل بعد (لا يوجد `deploy/.env` أو لم تُثبَّت مهمة النسخ الاحتياطي) يشغّل `bootstrap.sh` الذي:

1. يتحقق من النظام (Ubuntu 22.04+ أو Debian 12+) ويثبّت Docker (السكربت الرسمي get.docker.com) إن لم يكن مثبتًا.
2. ينشئ `/srv/repeater/{app,media,backups}`.
3. ينشئ `deploy/.env` من `.env.example` ويولّد بـ `openssl` كلمة سر القاعدة و`REPEATER_SECRET_KEY` ومفتاح تشفير التوكنات، ويضبط `REPEATER_BASE_URL=https://rotator.magic-lap.com`.
4. يفحص المنفذين 80/443:
   - **فاضيان:** يشغّل Caddy الخاص بـ Repeater ويستخرج الشهادة تلقائيًا.
   - **مشغولان** (nginx أو Traefik أو Coolify أو غيره): **لا يلمسه ولا يوقفه**، ويطبع في سجل الـ Action إعدادًا جاهزًا (بعنوان `Ports 80/443 are served by`) يضيفه من يدير ذلك البروكسي، يوجّه `rotator.magic-lap.com` إلى `127.0.0.1:8090` (أو عنوان docker0 لو البروكسي نفسه حاوية).
5. يبني الصورة (أول مرة 5–10 دقائق) ويشغّل الخدمات وينتظر سلامتها.
6. ينشئ حساب المدير من `ADMIN_EMAIL`/`ADMIN_PASSWORD`.
7. يثبّت نسخًا احتياطيًا يوميًا (`/etc/cron.d/repeater-backup`).
8. يطبع قائمة الخطوات الباقية.

في النهاية يفحص النشر `https://rotator.magic-lap.com/healthz`؛ فشله وقتها تحذير فقط (غالبًا DNS لم ينتشر بعد).

النشرات التالية: كل دفع إلى `main` = CI ثم `deploy.sh` (بناء، ترحيلات، تشغيل، انتظار السلامة). لا يغيّر `deploy/.env` إلا بمفاتيح المنصات الموجودة في الأسرار.

### الطريقة ب: يدويًا على السيرفر

```bash
# على السيرفر كـ root
apt-get update && apt-get install -y git
git clone <رابط المستودع> /srv/repeater/app      # أو ارفع الكود بـ scp/rsync
REPEATER_DOMAIN=rotator.magic-lap.com ADMIN_EMAIL=you@magic-lap.com \
  bash /srv/repeater/app/deploy/bootstrap.sh
```

يسألك عن كلمة مرور المدير في الطرفية. إعادة تشغيل السكربت آمنة: لا يعيد توليد الأسرار ولا يغيّر ما ضبطته.

## 6. حساب المدير

- إنشاء أو إعادة ضبط: **Actions ← Deploy ← Run workflow** مع تفعيل `reset_admin` (يستخدم السرّين `ADMIN_EMAIL`/`ADMIN_PASSWORD`).
- أو على السيرفر: `sudo ADMIN_EMAIL=you@magic-lap.com bash /srv/repeater/app/deploy/bootstrap.sh --create-admin`
- الدخول: `https://rotator.magic-lap.com/login`

## 7. مفاتيح المنصات

بعد إنشاء التطبيقات ([01](01-tiktok.md)، [02](02-google.md)، [03](03-meta.md)، [04](04-telegram.md)) إما:

- تضعها في أسرار GitHub ثم تشغّل Deploy، أو
- تعدّل `/srv/repeater/app/deploy/.env` على السيرفر ثم `sudo bash /srv/repeater/app/deploy/deploy.sh`.

للتحقق: `cd /srv/repeater/app/deploy && docker compose --env-file .env exec web python -m repeater.cli check` (يطبع أي منصة مضبوطة وروابط الـ redirect المتوقعة).

## 8. النسخ الاحتياطي والاسترجاع

- تلقائيًا كل يوم 03:17 (توقيت السيرفر): `/srv/repeater/backups/repeater-db-<التاريخ>.sql.gz`، ويُحذف ما عمره أكثر من 14 يومًا (`BACKUP_RETENTION_DAYS`). السجل: `journalctl -t repeater-backup`.
- نسخة يدوية الآن: `sudo bash /srv/repeater/app/deploy/backup.sh`
- **خارج السيرفر (موصى به):** `apt-get install -y rclone` ثم `rclone config` (مثلًا Cloudflare R2 باسم `r2`)، ثم في `deploy/.env`: `RCLONE_REMOTE=r2:magiclab-repeater`. القاعدة تُنسخ يوميًا والوسائط أسبوعيًا يوم الأحد (`RCLONE_MEDIA_SYNC=weekly|daily|off`).
- **احفظ نسخة من `deploy/.env` في مدير كلمات مرور** (Bitwarden أو 1Password). بدون `REPEATER_TOKEN_ENCRYPTION_KEYS` تُسترجع القاعدة لكن كل الحسابات المربوطة تحتاج إعادة ربط.
- الاسترجاع (يوقف web وworker، يحمّل النسخة، يطبّق الترحيلات، ثم يشغّلهما):

```bash
sudo bash /srv/repeater/app/deploy/backup.sh restore /srv/repeater/backups/repeater-db-20261014T031700Z.sql.gz --yes
```

- استرجاع الوسائط: `rclone copy r2:magiclab-repeater/media /srv/repeater/media && chown -R 10001:10001 /srv/repeater/media`
- نقل لسيرفر جديد: انسخ `deploy/.env` + آخر نسخة + الوسائط، شغّل `bootstrap.sh` على الجديد، ثم `backup.sh restore`.
- جرّب الاسترجاع مرة شهريًا على نسخة اختبار.

## 9. ملاحظات أمان

- `deploy/.env` صلاحياته 600 ولا يُطبع في أي سجل؛ `server-inspect` لا يقرأ منه إلا قيمًا غير سرية.
- منفذ اللوحة (8090) مربوط على `127.0.0.1` فقط. لا تنشر أي منفذ على `0.0.0.0`: Docker يتجاوز ufw.
- لو فعّلت جدار حماية Hostinger للـ VPS من hPanel: اسمح بـ TCP 22 و80 و443 (وUDP 443 لـ HTTP/3).
- الروابط المطلوبة في لوحات المنصات: الخصوصية `https://rotator.magic-lap.com/privacy` والشروط `https://rotator.magic-lap.com/terms`.
