"""Arabic domain messages. English diagnostics are persisted alongside each key."""

AR = {
    "error.generic": "تعذّر إكمال العملية. راجع إعدادات الحساب والملف قبل المحاولة مجددًا.",
    "platform.AuthError": "انتهت صلاحية الاتصال أو أذونات الحساب. أعد ربط الحساب ثم حاول مجددًا. الرمز: {code}",
    "platform.RateLimitedError": "وصلت المنصة إلى حد الطلبات أو الحصة اليومية. سيحاول النظام بعد انتهاء الانتظار. الرمز: {code}",
    "platform.RetryableError": "تعذّر الاتصال بالمنصة مؤقتًا. سيحاول النظام مجددًا. الرمز: {code}",
    "platform.RestartPublish": "أكدت المنصة انتهاء جلسة الرفع دون نشر. سيبدأ رفع جديد. الرمز: {code}",
    "platform.UnknownOutcomeError": "لم تؤكد المنصة نتيجة الرفع. قد يكون الفيديو منشورًا؛ افحص الحساب قبل إعادة النشر. الرمز: {code}",
    "platform.PermanentError": "رفضت المنصة الطلب. راجع إعدادات النشر ومواصفات الفيديو ثم حاول مجددًا. الرمز: {code}",
    "platform.NotSupportedError": "هذه العملية غير متاحة من خلال الواجهة الرسمية للمنصة. استخدم ملفًا محليًا أو Google Drive. الرمز: {code}",
    "platform.PlatformError": "تعذّرت العملية على المنصة. راجع إعدادات الحساب. الرمز: {code}",
    "import.uploaded": "تم رفع «{title}»؛ جارٍ تجهيزه للنشر.",
    "import.failed": "تعذّر استيراد الفيديو «{title}»: {detail}",
    "duration.seconds": "{n} ثانية",
    "duration.minutes": "{n} دقيقة",
    "duration.hours": "{n} ساعة",
    "publish.note": "راجع إشعار المنصة وحالة ظهور المنشور من حسابك.",
    "import.landscape": "تم وضع الفيديو الأفقي داخل إطار رأسي مع أشرطة سوداء.",
    "import.hdr": "تم تحويل ألوان HDR إلى الألوان القياسية.",
    "message.join": "{parts}",
}

AR.update(
    {
        "account.reconnect": "حساب {p0} «{p1}» يحتاج إلى إعادة الربط: {p2}",
        "account.connected": "تم ربط حساب {p1} «{p2}».",
        "account.revoke_failed": "تعذّر إلغاء التفويض لدى {p0}؛ تم حذف الرموز محليًا.",
        "account.disconnected": "تم فصل حساب {p0} «{p1}».",
        "account.post_interrupted": "المنشور #{p0}: {p1}",
        "account.disabled": "تم فصل الحساب.",
        "publish.skipped": "تم تخطي منشور «{p0}» على {p1}.",
        "publish.now": "جارٍ نشر «{p0}» على {p1} الآن.",
        "publish.video_now": "جارٍ نشر «{p0}» على {p1} ({p2}) الآن.",
        "publish.rules_changed": "تغيّرت قواعد النشر؛ ستُعاد جدولة {p0} من المنشورات المخططة.",
        "publish.duplicate": "الفيديو موجود بالفعل على {p0}؛ تم إلغاء المنشور.",
        "publish.duplicates_canceled": "الفيديو «{p0}» موجود بالفعل على {p1}؛ تم إلغاء {p2} من المنشورات المخططة.",
        "publish.archived": "تمت أرشفة «{p0}» وإلغاء {p1} من المنشورات المخططة.",
        "publish.restored": "عاد «{p0}» إلى دورة النشر.",
        "publish.restored_processing": "تمت استعادة «{p0}»؛ سينضم إلى دورة النشر بعد المعالجة.",
        "publish.paused": "النشر متوقف لهذه المساحة؛ ستُعاد الجدولة عند استئنافه.",
        "publish.testing_lock": "النشر ممنوع على هذا النظام خلال فترة الاختبار.",
        "publish.disconnected": "تم فصل الحساب.",
        "publish.reconnect_first": "أعد ربط الحساب؛ ستُعاد جدولة المنشور بعد ذلك.",
        "publish.rotation_off": "دورة النشر متوقفة لهذا الحساب.",
        "publish.video_gone": "الفيديو لم يعد موجودًا.",
        "publish.video_not_ready": "الفيديو خارج دورة النشر؛ انتظر اكتمال معالجته أو أعد استيراده.",
        "publish.missed_slot": "فات موعد المنشور؛ ستُعاد جدولته.",
        "publish.encoded_missing": "ملف الفيديو المعالج غير موجود على الخادم؛ أعد معالجة الفيديو.",
        "publish.unexpected_upload": "حدث خطأ أثناء الرفع إلى {p1}. قد يكون الفيديو منشورًا؛ افحص الحساب قبل إعادة النشر.",
        "publish.unexpected": "حدث خطأ غير متوقع. سيحاول النظام مجددًا. الرمز: {p0}",
        "publish.unconfirmed": "لم تؤكد {p0} الرفع: {p1}. قد يكون الفيديو منشورًا؛ افحص الحساب قبل إعادة النشر.",
        "publish.checking_lost_answer": "فُقد رد رفع «{p0}» إلى {p1}: {p2}. جارٍ التحقق من النتيجة لدى {p3}.",
        "publish.rate_giveup": "{p0} استمر تقييد الرفع على {p1} لمدة {p2} أيام.",
        "publish.rate_wait": "تم تقييد الطلبات: {p0} المحاولة التالية بعد {p1}.",
        "publish.rate_activity": "تم تقييد نشر «{p0}» على {p1}؛ المحاولة التالية بعد {p2} دون احتساب محاولة فاشلة: {p3}",
        "publish.giveup": "{p0} توقفت المحاولات بعد {p1} محاولات.",
        "publish.retry_error": "فشلت المحاولة {p0}: {p1} المحاولة التالية بعد {p2}.",
        "publish.retry_activity": "تعذّر نشر «{p0}» على {p1}؛ المحاولة {p2} من {p3} بعد {p4}: {p5}",
        "publish.target_gone": "الحساب أو الفيديو لم يعد موجودًا.",
        "publish.poll_giveup": "تعذّر التحقق من المنشور لدى {p0} لمدة {p1}: {p2}. قد يكون الفيديو منشورًا؛ افحص الحساب قبل إعادة النشر.",
        "publish.poll_wait": "تعذّر التحقق من الرفع: {p0} التحقق التالي بعد {p1}.",
        "publish.published": "تم نشر «{p0}» على {p1}. {p2}",
        "publish.poll_timeout": "لم تؤكد {p0} المنشور خلال ساعتين من الرفع. افحص الحساب قبل إعادة النشر.",
        "publish.bad_answer": "أعادت {p0} ردًا غير متوقع؛ افحص الحساب قبل إعادة النشر.",
        "publish.failed": "تعذّر نشر «{p0}» على {p1}: {p2}",
        "publish.canceled": "تم إلغاء المنشور #{p0}: {p1}",
        "publish.scheduled": "تمت جدولة {p0} من المنشورات الجديدة.",
        "publish.rule_incomplete": "لن تُجدول منشورات على {p0} ({p1}) حتى تراجع إعدادات النشر: {p2}.",
        "publish.suspended": "جدولة {p0} ({p1}) متوقفة لأن آخر {p2} منشورات احتاجت إلى تدخل. أصلح المشكلة وأعد نشر أحدها، أو عدّل قاعدة الحساب للاستئناف.",
        "publish.limit": "لن يُنشر «{p0}» على {p1} لأن الفيديو لا يطابق مواصفات المنصة. راجع المدة والحجم والأبعاد.",
        "account.meta_deleted": "حُذفت بيانات حساب {p0} بناءً على طلب Meta، بما فيها {p1} فيديو مستوردًا.",
        "import.uploaded": "تم رفع «{title}»؛ جارٍ تجهيزه للنشر.",
        "source.consent_needed": "الاستيراد غير الرسمي عبر yt-dlp متوقف حتى تؤكد ملكية المحتوى وتقبل المخاطر. يمكنك استخدام رفع ملف أو Google Drive.",
        "source.blocked": "لم يُفحص المصدر «{p0}»: {p1}",
        "source.paused": "توقف المصدر «{p0}» بعد {p1} عمليات فحص فاشلة متتالية. آخر خطأ: {p2} استخدم رفع ملف أو Google Drive عند الحاجة.",
        "source.check_failed": "تعذّر البحث عن فيديوهات جديدة في المصدر «{p0}»: {p1}",
        "import.original_missing": "الملف الأصلي غير موجود. أعد استيراد الفيديو أو رفعه.",
        "import.encode_failed": "تعذّر ترميز الفيديو. قد يكون الملف تالفًا؛ أعد رفع نسخة صالحة.",
        "import.ready": "الفيديو «{p0}» جاهز ({p1}). {p2}",
        "import.duplicate": "الفيديو «{p0}» مطابق للفيديو «{p1}» في المكتبة؛ لن يُنشر مرتين.",
        "import.retry": "جارٍ إعادة محاولة الفيديو «{p0}».",
        "import.released": "تم إلغاء علامة التكرار للفيديو «{p0}»؛ جارٍ معالجته.",
        "import.giveup": "{p0} توقفت المحاولات بعد {p1} محاولات.",
        "import.retry_error": "فشلت المحاولة {p0} من {p1}: {p2} المحاولة التالية بعد نحو {p3}.",
        "import.failed": "تعذّر استيراد الفيديو «{title}»: {detail}",
        "import.duplicate_recovered": "كان «{p0}» محسوبًا نسخة من «{p1}» الذي فشلت معالجته؛ جارٍ معالجته بصورة مستقلة.",
        "import.duplicate_original_lost": "فشلت معالجة الفيديو الأصلي وملف هذه النسخة غير موجود؛ أعد رفع الفيديو.",
        "import.disk_wait": "في انتظار مساحة خالية على الخادم؛ سيُستأنف هذا الفيديو تلقائيًا.",
        "import.disk_full": "قرص الخادم شبه ممتلئ؛ الفيديو «{p0}» في الانتظار.",
        "publish.interrupted_alert": "توقف {p0} من عمليات الرفع بعد إعادة تشغيل العامل وقد تكون منشورة: {p3}. افحص الحساب قبل إعادة النشر.",
        "publish.interrupted": "توقف الرفع بسبب إعادة تشغيل العامل. افحص الحساب قبل إعادة النشر لتجنب التكرار.",
        "publish.interrupted_post": "المنشور #{p0} على {p1}: {p2}",
        "import.abandoned": "توقفت معالجة الاستيراد قبل الاكتمال. أعد محاولة هذا الفيديو أو ارفعه مجددًا.",
    }
)

AR.update(
    {
        "account.reconnect_action": "أعد ربط حساب {platform} «{name}»، ثم حاول النشر مجددًا.",
        "account.reconnect_reason": "أعد ربط الحساب لاستعادة صلاحية الوصول.",
        "account.disconnected_live": "فُصل الحساب أثناء النشر؛ قد يكون الفيديو منشورًا. افحص الحساب قبل إعادة النشر.",
        "source.failure": "تعذّر فحص المصدر. راجع إعداداته واتصال الحساب، أو استخدم رفع ملف أو Google Drive.",
        "import.problem": "تعذّرت معالجة الملف. راجع صلاحيته وصيغته والمدة والحجم، أو ارفع نسخة جديدة.",
        "youtube.privacy_note": "ضبط YouTube ظهور الفيديو على «{actual}» بدلًا من «{requested}». قد يحدث ذلك قبل مراجعة مشروع التطبيق؛ غيّره من YouTube Studio عند الحاجة.",
        "tiktok.inbox": "المسودة في صندوق وارد TikTok؛ افتح تطبيق TikTok لإكمال نشرها.",
    }
)

AR.update(
    {
        "rule.problem.tiktok_privacy_required": "اختر من يمكنه مشاهدة المنشورات (مستوى الخصوصية).",
        "rule.problem.tiktok_music_consent_required": "وافق على تأكيد استخدام الموسيقى من تيك توك.",
        "rule.problem.tiktok_branded_private": "المحتوى التجاري (Branded content) لا يمكن أن يكون خاصًا: اختر جمهورًا أوسع أو أوقف الإفصاح.",
        "rule.problem.youtube_audience_required": "حدّد هل هذه الفيديوهات موجهة للأطفال.",
        "rule.problem.youtube_privacy_invalid": "اختر إعداد الخصوصية على يوتيوب.",
    }
)

AR.update(
    {
        "publish.replan": "تغيّرت قواعد النشر؛ ستُعاد جدولة المنشور.",
        "publish.archive_cancel": "تمت أرشفة الفيديو.",
        "publish.excluded": "هذه المنصة مستبعدة من نشر هذا الفيديو.",
        "publish.validation": "راجع إعدادات الخصوصية والجمهور ومواصفات الفيديو قبل إعادة النشر.",
    }
)

AR.update(
    {
        "notify.post_needs_attention": "{n} منشورات تحتاج إلى تدخل",
        "notify.uploads_interrupted": "توقفت {n} من عمليات الرفع وقد تكون منشورة",
        "notify.account_reconnect": "{n} حسابات تحتاج إلى إعادة الربط",
        "notify.source_failing": "{n} مصادر تعذّر فحصها",
        "notify.disk_low": "قرص الخادم شبه ممتلئ؛ الاستيراد في الانتظار",
        "notify.alert": "يوجد أمر يحتاج إلى تدخلك",
    }
)
