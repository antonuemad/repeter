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
- بصمة السيرفر **إلزامية**: من جهازك `ssh-keyscan -p 22 <IP>` وانسخ الناتج كاملًا إلى السر `VPS_KNOWN_HOSTS`. قبل الحفظ قارن البصمة (`ssh-keygen -lf` على ملف الناتج) بناتج `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` من الطرفية في hPanel. بدونها يرفض النشر أن يعمل: لو وثق بأي مفتاح يقدمه السيرفر لحظة النشر لأرسل كلمة مرور المدير ومفاتيح المنصات لمن ينتحل السيرفر (DNS مخترق أو وسيط في الشبكة). استخدم عنوان IP لا اسم نطاق في `VPS_HOST`.

## 3. أسرار GitHub

في المستودع: **Settings ← Secrets and variables ← Actions ← New repository secret**:

| السر | القيمة | إلزامي |
|---|---|---|
| `VPS_HOST` | عنوان IP للـ VPS | نعم |
| `VPS_USER` | `root` (أو المستخدم المخصص) | نعم |
| `VPS_SSH_KEY` | محتوى الملف الخاص `repeater_deploy` كاملًا (من `-----BEGIN` إلى `-----END`) | نعم |
| `REPEATER_DOMAIN` | `rotator.magic-lap.com` | نعم |
| `VPS_PORT` | منفذ SSH إن لم يكن 22 | لا |
| `REPEATER_PROXY` | أول نشر فقط: `auto` (الافتراضي) أو `caddy` أو `external` (القسم 5) | لا |
| `VPS_KNOWN_HOSTS` | ناتج `ssh-keyscan` بعد مطابقة بصمته مع بصمة السيرفر (القسم 2) | **نعم** |
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
4. يفحص المنفذين 80/443 (يحسب كل ما يحجزهما، حتى لو كان متوقفًا لحظة الفحص: حاوية متوقفة أو في إعادة تشغيل متكررة لها ربط على 80/443، وخدمة nginx أو apache أو caddy أو traefik مفعّلة في systemd لكنها لا تستمع الآن، وتثبيت Coolify):
   - **فاضيان والسيرفر لا يشغّل شيئًا آخر:** يشغّل Caddy الخاص بـ Repeater ويستخرج الشهادة تلقائيًا.
   - **فاضيان لكن السيرفر مشترك** (فيه حاويات مشاريع أخرى): **لا يشغّل Caddy من تلقاء نفسه**؛ ما يبدو فاضيًا الآن قد يكون بروكسي متوقفًا أو سيُشغَّل لاحقًا، وCaddy بـ `restart: unless-stopped` سيأخذ المنفذين ويُسقط كل المواقع الأخرى عند أول إعادة تشغيل. يطبع الإعداد الجاهز للبروكسي. لو أردت Caddy فعلًا اضبط `REPEATER_PROXY=caddy` (متغير بيئة، أو سر GitHub بنفس الاسم في أول نشر، أو `--stdin-env`).
   - **مشغولان** (nginx أو Traefik أو Coolify أو غيره، شغّال أو متوقف): **لا يلمسه ولا يوقفه**، ويطبع في سجل الـ Action إعدادًا جاهزًا (بعنوان `Ports 80/443 are served by`) يضيفه من يدير ذلك البروكسي، يوجّه `rotator.magic-lap.com` إلى `127.0.0.1:8090` (أو عنوان docker0 لو البروكسي نفسه حاوية). في هذا الإعداد `X-Forwarded-For` **يُضبط** على عنوان العميل (`$remote_addr` في nginx) ولا يُلحَق بما أرسله العميل، لأن التطبيق يعتمد عليه في تحديد معدل محاولات الدخول وفي سجل الوصول للوسائط.
   - `REPEATER_PROXY=caddy` مع منفذين مشغولين يتوقف السكربت برسالة صريحة (لا يشغّل Caddy ليفشل أو ليسرق المنفذين لاحقًا).
   - القرار يُحفظ في `deploy/.env` (`COMPOSE_PROFILES`, `REPEATER_WEB_BIND`, `REPEATER_WEB_PORT`) ويُفحص **في كل تشغيل لاحق** لـ `bootstrap.sh`: لو لم يعد يناسب السيرفر (مثلًا نُقل `.env` من سيرفر آخر) يتوقف قبل أي بناء أو تشغيل برسالة تذكر المشكلة، ويمكن إعادة القرار بـ `sudo bash bootstrap.sh --redetect-proxy`.
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

### النسخ

- تلقائيًا كل يوم 03:17 (توقيت السيرفر): `/srv/repeater/backups/repeater-db-<التاريخ>.sql.gz`، ويُحذف ما عمره أكثر من 14 يومًا (`BACKUP_RETENTION_DAYS`). السجل: `journalctl -t repeater-backup`.
- **قبل كل نشر** يأخذ `deploy.sh` نسخة تحمل اسم `repeater-predeploy-<التاريخ>.sql.gz` (يُحتفظ بها 30 يومًا؛ إعادة المحاولة لا تحذف نسخة ما قبل الترحيل)، وهي نقطة الرجوع لو غيّر الإصدار الجديد شكل القاعدة. فشل هذه النسخة يوقف النشر قبل الترحيلات. (`SKIP_PREDEPLOY_BACKUP=1` تتجاوزها، لا يُنصح.)
- نسخة يدوية الآن: `sudo bash /srv/repeater/app/deploy/backup.sh`. آخر نجاح: `sudo bash /srv/repeater/app/deploy/backup.sh status`.
- **التنبيهات:** فشل النسخة (القاعدة متوقفة، القرص ممتلئ، رفع rclone فشل...) يرسل رسالة تيليجرام إلى نفس بوت [04-telegram.md](04-telegram.md). ومهمة ثانية الساعة 09:07 تنبّه لو مرّت 36 ساعة بلا نسخة ناجحة (مثلًا الـ cron نفسه متوقف). علامة آخر نجاح في `/srv/repeater/backups/status/last_success`، ويقرأها `repeater.cli check` داخل الحاوية. بدون إعداد تيليجرام تبقى الرسائل في `journalctl` فقط، فاضبطه. (السيرفرات المثبّتة قبل هذا التحديث: أعد تشغيل `bootstrap.sh` مرة لتثبيت مهمة 09:07.)
- **خارج السيرفر (موصى به):** `apt-get install -y rclone` ثم `rclone config` (مثلًا Cloudflare R2 باسم `r2`)، ثم في `deploy/.env`: `RCLONE_REMOTE=r2:magiclab-repeater`. القاعدة تُنسخ يوميًا والوسائط أسبوعيًا يوم الأحد (`RCLONE_MEDIA_SYNC=weekly|daily|off`).
  - **النسخ لا تُعكس:** ملفات النسخ تُرفع بـ `rclone copy` فلا يحذف السيرفر ما ليس عنده؛ والقديم يُحذف بالعمر فقط (`BACKUP_REMOTE_RETENTION_DAYS`، افتراضيًا 30 يومًا، ولا يحذف شيئًا ما لم توجد نسخة أحدث من الحد على الجهة البعيدة).
  - **مكتبة الوسائط مرآة بحماية:** ما يُحذف أو يُستبدل محليًا ينتقل إلى `media-trash/<التاريخ>` على الجهة البعيدة ويبقى `RCLONE_TRASH_DAYS` (7 أيام) ثم يُمحى؛ وبحد أقصى `RCLONE_MAX_DELETE` ملفًا (500) في المرة، فالسيرفر الذي استُرجع بعضه فقط لا يمسح المكتبة البعيدة؛ ومجلد وسائط محلي **فارغ** لا يُزامَن فوق نسخة بعيدة غير فارغة (النسخ يفشل ويصلك تنبيه). بعد حذف مقصود لعدد كبير من الملفات ارفع `RCLONE_MAX_DELETE` مرة واحدة.
  - **خصوصية:** النسخ (14 يومًا محليًا و30 يومًا بعيدًا، وسلة الوسائط 7 أيام) تحتفظ بالبيانات المحذوفة حتى تنتهي مدتها؛ وصف سياسة الخصوصية للنسخ الاحتياطية يجب أن يطابق هذه الأرقام.
- **احفظ نسخة من `deploy/.env` في مدير كلمات مرور** (Bitwarden أو 1Password). بدون `REPEATER_TOKEN_ENCRYPTION_KEYS` تُسترجع القاعدة لكن كل الحسابات المربوطة تحتاج إعادة ربط. (تدوير المفتاح: [05-operations.md](05-operations.md).)

### الاسترجاع

```bash
sudo bash /srv/repeater/app/deploy/backup.sh restore /srv/repeater/backups/repeater-db-20261014T031700Z.sql.gz --yes
```

ما يفعله، بالترتيب:

1. يتأكد أن الملف نسخة `pg_dump` سليمة، ويأخذ **نسخة أمان** من القاعدة الحالية (`repeater-prerestore-*`، يُحتفظ بآخر 3).
2. يوقف web وworker، ثم يحمّل النسخة **في معاملة واحدة تبدأ بمسح مخطط القاعدة** (`DROP SCHEMA public CASCADE`). لو فشل أي جزء تتراجع المعاملة كلها وتبقى القاعدة الحالية كما كانت، ويمكن إعادة المحاولة. (قبل ذلك كان التحميل فوق القاعدة الحية يتعثر في منتصفه حين يكون مخططها أحدث من النسخة، ويترك قاعدة تالفة لا تقبل إعادة المحاولة.)
3. يطبّق ترحيلات الإصدار المحدد بـ `REPEATER_IMAGE_TAG` (الافتراضي `latest`).
4. **يوقف النشر في كل مساحات العمل** (`paused`). النسخة تحتوي طابور المهام كما كان وقت أخذها: منشورات خرجت بعدها تعود "مجدولة"، فلو عمل العامل لنشرها مرة ثانية.
5. يشغّل **web فقط**. العامل لا يعمل إلا مع `--resume`، وحتى حينها لا يُنشر شيء قبل أن تستأنف مساحات العمل من اللوحة.

بعد الاسترجاع: راجع كل حساب مربوط بحثًا عن منشورات خرجت بعد وقت النسخة (يطبعه السكربت)، احذف المكررات أو ألغِ المجدول منها، ثم شغّل العامل بالأمر الذي يطبعه سكربت الاسترجاع، مع نفس `REPEATER_IMAGE_TAG` الذي استرجعت به (مثل `REPEATER_IMAGE_TAG=previous docker compose up -d worker` للرجوع للإصدار السابق)، أو أعد أمر الاسترجاع بنفس الوسم ومع `--resume` واستأنف النشر لكل مساحة عمل من **الإعدادات**.

- **الرجوع لإصدار سابق غيّر القاعدة:** `REPEATER_IMAGE_TAG=previous bash backup.sh restore /srv/repeater/backups/repeater-predeploy-<الوقت>.sql.gz --yes` (تفاصيل في [05-operations.md](05-operations.md#الرجوع-للإصدار-السابق)).
- **لا تستخدم `.env` الإنتاج لاختبار الاسترجاع على جهاز آخر وسيرفر الإنتاج شغّال:** نفس مفاتيح التشفير تفك التوكنات الحقيقية. للاختبار الشهري أنشئ `.env` جديدًا (يولّده `bootstrap.sh`، بمفتاح تشفير مختلف فلن تُفكّ التوكنات)، ولا تضف `--resume`؛ الاسترجاع بدونه لا يشغّل العامل أصلًا.
- استرجاع الوسائط: `rclone copy r2:magiclab-repeater/media /srv/repeater/media && chown -R 10001:10001 /srv/repeater/media`
- **النقل لسيرفر جديد، بالترتيب:**
  1. على السيرفر القديم أوقف كل شيء: `docker compose --env-file .env stop` (لا يعمل سيرفران بنفس التوكنات).
  2. على الجديد: انسخ `deploy/.env` وآخر نسخة، واجعل `RCLONE_REMOTE=` **فارغًا** في `.env` مؤقتًا حتى ينتهي نسخ الوسائط (وإلا قد تزامن مهمة 03:17 مجلدًا نصف ممتلئ؛ والمزامنة الآن محمية بحدود وسلة محذوفات لكن لا داعي للمخاطرة).
  3. `sudo bash bootstrap.sh --redetect-proxy` (يعيد اختيار وضع البروكسي وعنوان ربط اللوحة لهذا السيرفر؛ بدون الخيار يتوقف لو لم يناسبه `.env` المنقول).
  4. `backup.sh restore ... --yes`، ثم استرجاع الوسائط، ثم المراجعة والاستئناف كما فوق، ثم أعد `RCLONE_REMOTE`.
  5. حدّث سجل DNS ليشير للسيرفر الجديد.
- جرّب الاسترجاع مرة شهريًا على نسخة اختبار (بـ `.env` جديد كما فوق).

## 9. ملاحظات أمان

- `deploy/.env` صلاحياته 600 ولا يُطبع في أي سجل؛ `server-inspect` لا يقرأ منه إلا قيمًا غير سرية.
- منفذ اللوحة (8090) مربوط على `127.0.0.1` فقط. لا تنشر أي منفذ على `0.0.0.0`: Docker يتجاوز ufw.
- **عنوان العميل خلف البروكسي:** التطبيق يعتمد على `X-Forwarded-For` و`X-Forwarded-Proto` فقط من البروكسي نفسه: الـ loopback ونطاقات Docker الخاصة (`172.16.0.0/12` و`192.168.0.0/16`)، وهو ما يغطي Caddy الخاص بـ Repeater وnginx أو Nginx Proxy Manager أو Traefik/Coolify على نفس السيرفر. القيمة في `REPEATER_FORWARDED_ALLOW_IPS` (غيّرها لو يصل بروكسيك من عنوان آخر). لا تضع `*` أبدًا: عندها يأخذ التطبيق أول عنوان في الترويسة، وهو ما يكتبه العميل نفسه لو كان البروكسي يُلحِق بدل أن يستبدل (`$proxy_add_x_forwarded_for` في nginx)، فيتجاوز المهاجم تحديد معدل الدخول لكل IP ويزوّر عناوين سجل الوصول للوسائط. في nginx/NPM استخدم `proxy_set_header X-Forwarded-For $remote_addr;` (الإعداد الذي يطبعه `bootstrap.sh`)، وفي Apache `ProxyAddHeaders Off` مع `RequestHeader set X-Forwarded-For "%{REMOTE_ADDR}s"`. Caddy وTraefik يتجاهلان افتراضيًا ما يرسله العميل من هذه الترويسات. (حتى لو ألحق البروكسي، القائمة المحددة تجعل التطبيق يأخذ **آخر** عنوان غير موثوق، وهو ما أضافه البروكسي، لا أول عنوان؛ الضبط بـ `$remote_addr` طبقة حماية إضافية.)
- لو فعّلت جدار حماية Hostinger للـ VPS من hPanel: اسمح بـ TCP 22 و80 و443 (وUDP 443 لـ HTTP/3).
- الروابط المطلوبة في لوحات المنصات: الخصوصية `https://rotator.magic-lap.com/privacy` والشروط `https://rotator.magic-lap.com/terms`.


يفحص `deploy.sh` بعد تشغيل اللوحة عناوين بوابة Docker وCaddy الفعلية (IPv4 وIPv6) وعنوان الربط
مقابل `FORWARDED_ALLOW_IPS` داخل الحاوية. إذا ظهر `trusted proxy check failed`، أضف عنوان
البروكسي الظاهر في الرسالة أو نطاقه المقصود إلى `REPEATER_FORWARDED_ALLOW_IPS` ثم أعد النشر.
السكربت يتوقف عند عدم التطابق ولا يوسّع الثقة تلقائيًا. خيار `--no-build` يشترط وجود وسم الصورة
المطلوب محليًا؛ ورسالة الرجوع لا تظهر إلا إذا كانت صورة `previous` موجودة.
