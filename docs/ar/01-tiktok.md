# 01 — تطبيق تيكتوك (Login Kit + Content Posting API)

النتيجة: مفتاحان في `deploy/.env` (أو أسرار GitHub):
`REPEATER_TIKTOK_CLIENT_KEY` و`REPEATER_TIKTOK_CLIENT_SECRET`.

## 1. إنشاء التطبيق

1. سجّل الدخول في https://developers.tiktok.com بحساب Magic Lap ← **Manage apps** ← **Connect an app**.
2. بيانات التطبيق:
   - **App name:** `Magic Lap Rotator` (اسم خاص بالتطبيق؛ ممنوع أن يحتوي "TikTok").
   - **App icon:** شعار واضح يطابق الاسم.
   - **Description:** أداة ينشر بها صانع المحتوى فيديوهاته الأصلية من مكتبته على حساباته (لا تصفها كناقل من منصة لأخرى).
   - **Terms of Service URL:** `https://rotator.magic-lap.com/terms`
   - **Privacy Policy URL:** `https://rotator.magic-lap.com/privacy`
   - **Platforms:** Web، و**Website URL:** `https://magic-lap.com` (موقع رسمي كامل؛ صفحة دخول اللوحة لا تُقبل في المراجعة).
3. أضف المنتجات (**Add products**):
   - **Login Kit** ← في إعداداته **Redirect URI** (Web):
     `https://rotator.magic-lap.com/oauth/tiktok/callback`
     (لازم https، مطلق، بدون `?` أو `#`، ويطابق حرفيًا).
   - **Content Posting API** ← فعّل **Direct Post**.
   - **Display API** (لصلاحية `video.list` التي تقرأ فيديوهات الحساب العامة).
4. **Scopes** التي يطلبها Repeater (تأكد أنها كلها مضافة): `user.info.basic`، `video.list`، `video.publish`، `video.upload`.
5. من صفحة التطبيق انسخ **Client key** و**Client secret** إلى الإعدادات، ثم انشر (`deploy.sh` أو Actions).
6. اختبر: اللوحة ← الحسابات ← ربط تيكتوك.

## 2. حدود التطبيق قبل التدقيق (Audit) — مهم للمرحلة 0

من إرشادات تيكتوك الرسمية لمشاركة المحتوى:

- كل ما ينشره تطبيق غير مدقق يكون **خاصًا فقط** (`SELF_ONLY`، يراه صاحب الحساب وحده).
- **حتى 5 مستخدمين فقط** يمكنهم النشر عبر التطبيق خلال 24 ساعة.
- الحساب الذي ينشر **يجب أن يكون خاصًا (Private) وقت النشر**؛ وإلا يرفض تيكتوك بخطأ `unaudited_client_can_only_post_to_private_accounts`.
- جعل منشور ظاهرًا لاحقًا: يحوّل صاحب الحساب حسابه إلى عام ثم يغيّر كل منشور إلى "Everyone" من التطبيق.

النتيجة العملية: حتى يُقبل التدقيق، اختبر النشر على حساب تيكتوك تجريبي خاص، لا على حساب Magic Lap العام.

## 3. قائمة التدقيق (قبل الطلب)

- [ ] موقع رسمي كامل على `https://magic-lap.com`، وفي تذييله رابطا الخصوصية والشروط ظاهران بدون فتح قائمة (يمكن أن يشيرا إلى `https://rotator.magic-lap.com/privacy` و`/terms`).
- [ ] شاشة النشر تطابق الإرشادات: اسم المستخدم (nickname) من `creator_info` ظاهر؛ قائمة الخصوصية **بدون قيمة افتراضية**؛ خيارات التعليق/Duet/Stitch غير مُعلَّمة وتُعطَّل ما يعطّله الحساب؛ مفتاح الإفصاح التجاري (Your brand / Branded content) مغلق افتراضيًا؛ نص الموافقة قبل زر النشر: "By posting, you agree to TikTok's Music Usage Confirmation"؛ معاينة للفيديو؛ لا علامات مائية دعائية.
- [ ] فيديو عرض (حتى 5 فيديوهات، كل منها حتى 50 ميجا) يُظهر الرحلة كاملة من الدخول حتى ظهور المنشور، والنطاق في الفيديو يطابق Website URL، وكل منتج وصلاحية مطلوبة ظاهرة فيه. التطبيقات غير المعتمدة سابقًا تصوّر العرض في **Sandbox** من بوابة المطورين.
- [ ] وصف يوضح أن المحتوى أصلي من مكتبة المستخدم نفسه (تيكتوك يرفض صراحة "تطبيقًا ينسخ محتوى عشوائيًا من منصات أخرى").
- [ ] تنبيه: إرشادات المراجعة ترفض التطبيقات "للاستخدام الخاص أو الشخصي" والتطبيقات قيد التطوير؛ لذلك يُقدَّم الطلب عند فتح Repeater للعملاء (المرحلة 3)، لا لنسخة Magic Lap الداخلية فقط.

## 4. ملاحظات تشغيل

- توكن الوصول يعيش 24 ساعة وتوكن التحديث 365 يومًا؛ Repeater يجددهما تلقائيًا.
- Repeater يرفع الملف نفسه لتيكتوك (`FILE_UPLOAD`)، فلا حاجة لتوثيق نطاق `PULL_FROM_URL`.
- "فصل الحساب" في اللوحة يلغي التوكن عند تيكتوك.

المراجع: https://developers.tiktok.com/doc/content-posting-api-get-started ، https://developers.tiktok.com/doc/content-sharing-guidelines ، https://developers.tiktok.com/doc/login-kit-web ، https://developers.tiktok.com/doc/app-review-guidelines
