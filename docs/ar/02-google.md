# 02 — مشروع Google Cloud (YouTube + Google Drive)

النتيجة: `REPEATER_GOOGLE_CLIENT_ID` و`REPEATER_GOOGLE_CLIENT_SECRET`.
تطبيق OAuth واحد يخدم نشر يوتيوب شورتس واستيراد الفيديوهات من Google Drive.

## 1. المشروع والـ APIs

1. https://console.cloud.google.com ← قائمة المشاريع ← **New project** ← الاسم `Magic Lap Rotator`.
2. **APIs & Services ← Library** وفعّل:
   - **YouTube Data API v3**
   - **Google Drive API**

## 2. شاشة الموافقة (Google Auth Platform)

افتح https://console.cloud.google.com/auth/overview (تأكد أن المشروع الصحيح مختار) ثم **Get started**:

1. **Branding:**
   - App name: `Magic Lap Rotator` (بدون "YouTube" أو "YT" في الاسم)، وبريد الدعم.
   - Application home page: **صفحة عامة على النطاق الموثّق** تصف التطبيق وتربط بـ `/privacy` و`/terms`، مثل `https://magic-lap.com/rotator` (أنشئها قبل التقديم). **لا تضع `https://rotator.magic-lap.com`**: للزائر بلا حساب يحوّله التطبيق (303) إلى صفحة الدخول، وتحقق العلامة التجارية في Google يشترط صفحة رئيسية "ظاهرة بلا تسجيل دخول، ثابتة، ولا تحوّل إلى عنوان آخر، وتعرض معلومات عن التطبيق" ويرفض الصفحة الواقعة خلف تسجيل الدخول (https://support.google.com/cloud/answer/13807376). نفس القاعدة طبقها تيكتوك ([01-tiktok.md](01-tiktok.md)). في وضع Testing/Internal لا يُطلب هذا؛ يلزم عند الانتقال إلى Production (القسم 4).
   - Privacy policy: `https://rotator.magic-lap.com/privacy`
   - Terms of service: `https://rotator.magic-lap.com/terms`
   - Authorized domains: `magic-lap.com`
2. **Audience:**
   - User type: **External**.
   - Publishing status: **Testing**.
   - **Test users ← Add users:** حسابات Google التي تملك قنوات يوتيوب Magic Lap ومجلدات Drive (حتى 100).
3. **Data Access ← Add or remove scopes:**
   - `https://www.googleapis.com/auth/youtube.upload`
   - `https://www.googleapis.com/auth/youtube.readonly`
   - `https://www.googleapis.com/auth/drive.readonly`
   - `openid` و`.../auth/userinfo.email`
4. **Clients ← Create client:**
   - Application type: **Web application**، الاسم `Repeater web`.
   - **Authorized redirect URIs:** `https://rotator.magic-lap.com/oauth/google/callback`
   - **Create** ← انسخ Client ID وClient secret إلى الإعدادات وانشر.

اختبر: اللوحة ← الحسابات ← ربط يوتيوب، ثم ربط Google Drive. عند الموافقة اترك كل المربعات مُعلَّمة؛ لو أُلغي مربع يطلب منك Repeater الربط مجددًا.

## 3. وضع Testing: انتهاء بعد 7 أيام

- في Testing مع نوع External، توكن التحديث **ينتهي بعد 7 أيام** من الموافقة. بعدها يظهر الحساب في اللوحة بحاجة لإعادة ربط (وتنبيه تيليجرام)، فيُعاد الربط من صفحة الحسابات.
- **بديل بدون انتهاء أسبوعي:** لو بريد Magic Lap على Google Workspace وقنوات يوتيوب وDrive مملوكة لحسابات من نفس المؤسسة، اختر في Audience نوع **Internal**: لا قائمة مختبرين ولا انتهاء 7 أيام ولا تحقق من Google.

## 4. الانتقال إلى Production (عند فتح المنتج للعملاء)

1. **Audience ← Publish app**.
2. صلاحيات يوتيوب (`youtube.upload`، `youtube.readonly`) **حساسة (Sensitive)**: تحتاج تحقق Google: سياسة خصوصية على نفس النطاق، إثبات ملكية `magic-lap.com` في Google Search Console، فيديو عرض، وتبرير لكل صلاحية (عادة أيام عمل).
3. `drive.readonly` صلاحية **مقيدة (Restricted)**: تحتاج تحققًا مقيدًا و**تقييمًا أمنيًا** يُجدَّد كل 12 شهرًا. البديل الأرخص لاحقًا: صلاحية `drive.file` غير الحساسة مع Google Picker (يتطلب تعديل كود).
4. حتى يكتمل التحقق: يرى المستخدمون شاشة "تطبيق غير موثّق"، والحد 100 مستخدم جديد طوال عمر المشروع.

## 5. الحصة (Quota)

- `videos.insert`: **100 رفع في اليوم** للمشروع كله (حزمة Video Uploads منفصلة)، تتجدد منتصف الليل بتوقيت المحيط الهادئ. بقية الطلبات من 10,000 وحدة يوميًا.
- لزيادتها: نموذج تدقيق الامتثال وتمديد الحصة لـ YouTube API (Audit and Quota Extension Form).
- الفيديوهات المرفوعة من مشروع غير موثّق **لا تُحصر** في وضع خاص (حسب صفحة `videos.insert` الحالية).

## 6. ملاحظات

- سياسة الخصوصية يجب أن تذكر استخدام YouTube API Services ورابط سياسة Google وطريقة الإلغاء عبر https://myaccount.google.com/permissions (صفحة `/privacy` في Repeater تغطي ذلك).
- "فصل الحساب" في اللوحة يلغي التوكن عند Google.
- سياسة مطوري YouTube API تشترط أن تُحدَّث البيانات المخزّنة من YouTube (عنوان القناة، الصورة، معرفات الفيديوهات والروابط) أو تُحذف خلال 30 يومًا. Repeater يجدد بيانات قنوات يوتيوب المفعّلة كل 25 يومًا على الأكثر؛ النص الملزم لما يُحفظ ومدة حفظه هو صفحة `/privacy` في اللوحة.

المراجع: https://developers.google.com/youtube/v3/docs/videos/insert ، https://developers.google.com/identity/protocols/oauth2 ، https://support.google.com/cloud/answer/15549945 ، https://developers.google.com/workspace/drive/api/guides/api-specific-auth ، https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
