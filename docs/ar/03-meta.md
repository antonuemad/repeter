# 03 — تطبيق ميتا (فيسبوك ريلز + انستجرام ريلز)

النتيجة: `REPEATER_META_APP_ID` و`REPEATER_META_APP_SECRET` (و`REPEATER_META_GRAPH_VERSION=v26.0` افتراضيًا).
تطبيق واحد بتسجيل دخول فيسبوك يربط صفحات فيسبوك وحسابات انستجرام المهنية المرتبطة بها.

## 0. شروط الحسابات (قبل أي شيء)

- حساب انستجرام **مهني (Business أو Creator)**، و**مرتبط بصفحة فيسبوك** (من إعدادات الصفحة ← Linked accounts، أو من Accounts Center).
- الشخص الذي يربط في Repeater له على الصفحة صلاحية **إنشاء المحتوى** (Create content / تحكم كامل).
- يفضَّل أن يكون للتطبيق Business Portfolio باسم Magic Lap (business.facebook.com).

## 1. إنشاء التطبيق

1. https://developers.facebook.com/apps ← **Create app**.
2. اختر حالة الاستخدام **Other** ثم نوع التطبيق **Business** (إن ظهر الاختيار)، الاسم `Magic Lap Rotator`، بريد التواصل، واربطه بـ Business Portfolio الخاص بـ Magic Lap.
3. أضف منتج **Facebook Login for Business** (أو Facebook Login لو هو المتاح لنوع التطبيق) ← **Settings**:
   - Client OAuth login: On — Web OAuth login: On — Enforce HTTPS: On — Use Strict Mode for redirect URIs: On
   - **Valid OAuth Redirect URIs:** `https://rotator.magic-lap.com/oauth/meta/callback`
   - **Deauthorize callback URL:** `https://rotator.magic-lap.com/meta/deauthorize`
4. **App settings ← Basic:**
   - App domains: `rotator.magic-lap.com`
   - Privacy Policy URL: `https://rotator.magic-lap.com/privacy`
   - Terms of Service URL: `https://rotator.magic-lap.com/terms`
   - User data deletion ← **Data deletion callback URL:** `https://rotator.magic-lap.com/meta/data-deletion`
   - App icon (1024×1024) والفئة (Category).
   - انسخ **App ID** و**App secret** (زر Show) إلى الإعدادات وانشر.

## 2. الصلاحيات التي يطلبها Repeater

| الصلاحية | لماذا |
|---|---|
| `pages_show_list` | عرض الصفحات التي تديرها لتختار منها |
| `pages_read_engagement` | قراءة بيانات الصفحة وتوكنها (مطلوبة للريلز وانستجرام عبر فيسبوك) |
| `pages_manage_posts` | نشر الريلز على الصفحة |
| `instagram_basic` | قراءة حساب انستجرام المرتبط ووسائطه |
| `instagram_content_publish` | نشر الريلز على انستجرام |
| `business_management` | الوصول للصفحات والحسابات المملوكة عبر Business Portfolio |

ملاحظة: Facebook Login for Business يفضّل "إعدادات تسجيل الدخول" (`config_id`) بدل تمرير `scope`؛ Repeater حاليًا يمرّر `scope` (مدعوم لتوكنات المستخدم). لو رفض حوار الدخول الصلاحيات، أنشئ Configuration بنوع User access token بنفس الصلاحيات وأبلغ فريق التطوير.

## 3. وضع التطوير (Development) وأدوار التطبيق — يكفي للمرحلة 0

- التطبيق في **Development** يطلب الصلاحيات فقط من أصحاب الأدوار فيه، والصلاحيات تعمل لهم بدون App Review.
- **App roles ← Roles ← Add People:** أضف كل شخص من Magic Lap سيربط صفحة أو حساب انستجرام (Administrator أو Developer أو Tester)، ويقبل الدعوة من حسابه على developers.facebook.com.
- اختبر: اللوحة ← الحسابات ← ربط فيسبوك/انستجرام، واختر الصفحات.
- النشر من التطبيق في وضع التطوير يظهر عامًا على حسابات أصحاب الأدوار.

## 4. لاحقًا: فتح التطبيق للعملاء (المرحلة 3)

- **Advanced Access** لكل صلاحية أعلاه = **App Review** (فيديو شاشة لكل صلاحية: دخول، اختيار الصفحة، نشر، ظهور المنشور) + **Business Verification** لـ Magic Lap.
- ثم تحويل التطبيق إلى **Live** من مفتاح وضع التطبيق أعلى لوحة التطبيق.
- يجب أن تبقى روابط الخصوصية وحذف البيانات وإلغاء التفويض تعمل (ميتا تختبرها).

## 5. حدود يجب معرفتها

- فيسبوك: 30 ريل منشورة عبر الـ API لكل صفحة خلال 24 ساعة، والخصوصية عامة فقط.
- انستجرام: حد نشر يومي للحساب (يقرأه Repeater من `content_publishing_limit` وقت التشغيل).
- انستجرام يسحب الفيديو من رابط Repeater الموقّع، لذلك سجل DNS يجب أن يبقى **DNS only** (راجع [00-deploy.md](00-deploy.md)).
- توكن الصفحة المأخوذ من توكن مستخدم طويل لا ينتهي، لكنه يسقط عند تغيير كلمة المرور أو الدور، أو بعد 90 يومًا بدون نشاط؛ عندها يطلب Repeater إعادة الربط.

المراجع: https://developers.facebook.com/docs/development/build-and-test/app-modes ، https://developers.facebook.com/docs/development/create-an-app/app-dashboard/data-deletion-callback ، https://developers.facebook.com/docs/instagram-platform/overview ، https://developers.facebook.com/docs/facebook-login/facebook-login-for-business ، https://developers.facebook.com/docs/video-api/guides/reels-publishing
