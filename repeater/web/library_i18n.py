"""Library management copy in English and Arabic."""

COPY = {
    "library.filter_excluded": ("Excluded", "مستبعد"),
    "bulk.search": ("Search this library", "ابحث في المكتبة دي"),
    "bulk.search_hint": ("Title or caption", "عنوان المحتوى أو نصه"),
    "bulk.find": ("Search", "بحث"),
    "bulk.clear_search": ("Clear search", "مسح البحث"),
    "bulk.no_matches": (
        "No items match these filters. Clear the search or choose another status.",
        "مفيش عناصر مطابقة للاختيارات دي. امسح البحث أو اختَر حالة تانية.",
    ),
    "bulk.show_active": ("Show active content", "عرض محتوى المكتبة"),
    "bulk.manage": ("Manage selected items", "إدارة العناصر المحددة"),
    "bulk.select": ("Select", "تحديد"),
    "bulk.selected": ("{count} selected", "تم تحديد {count} عنصر"),
    "bulk.select_page": ("Select this page", "تحديد عناصر الصفحة"),
    "bulk.clear": ("Clear selection", "إلغاء التحديد"),
    "bulk.selection_help": (
        "Select up to 24 items from this page, then review your changes. Selection clears when you change page, person or filters.",
        "حدّد لحد ٢٤ عنصرًا من الصفحة دي، ثم راجع التغييرات. التحديد بيتلغي عند تغيير الصفحة أو الشخص أو الفلتر.",
    ),
    "bulk.action": ("Action", "الإجراء"),
    "bulk.choose": ("Choose an action", "اختَر الإجراء"),
    "bulk.choice_exclude": ("Exclude", "استبعاد"),
    "bulk.choice_archive": ("Archive", "أرشفة"),
    "bulk.choice_include": ("Remove exclusion", "إلغاء الاستبعاد"),
    "bulk.choice_restore": ("Restore", "استعادة"),
    "bulk.action_exclude": ("Exclude from rotation", "استبعاد من الروتيشن"),
    "bulk.action_archive": ("Archive", "أرشفة"),
    "bulk.action_include": ("Remove platform exclusions", "إلغاء استبعاد المنصات"),
    "bulk.action_restore": ("Restore from archive", "استعادة من الأرشيف"),
    "bulk.scope": ("Platforms", "المنصات"),
    "bulk.all_short": ("All platforms", "كل المنصات"),
    "bulk.all": ("All supported platforms", "كل المنصات المدعومة"),
    "bulk.review": ("Review changes", "مراجعة التغييرات"),
    "bulk.review_title": ("Review selected library items", "راجع عناصر المكتبة المحددة"),
    "bulk.review_count": (
        "{count} selected · {changed} will change",
        "تم تحديد {count} عنصر · سيتغير {changed} عنصر",
    ),
    "bulk.confirm": ("Confirm changes", "تأكيد التغييرات"),
    "bulk.edit_selection": ("Edit selection", "تعديل التحديد"),
    "bulk.back": ("Back to this library", "الرجوع للمكتبة دي"),
    "bulk.excluded": ("Excluded from: {platforms}", "مستبعد من: {platforms}"),
    "bulk.excluded_all": ("Excluded from all rotation platforms", "مستبعد من الروتيشن على كل المنصات"),
    "bulk.unselectable": (
        "Manage when preparation finishes; duplicates stay out of rotation.",
        "الإدارة الجماعية بعد اكتمال التجهيز؛ المكرر مستبعد أصلًا من الروتيشن.",
    ),
    "bulk.help_exclude": (
        "Keep the files in the library and exclude these items from the selected platforms. Other platform choices stay unchanged.",
        "تفضل الملفات في المكتبة، والعناصر تتستبعد من المنصات المحددة. اختيارات المنصات الأخرى تفضل كما هي.",
    ),
    "bulk.help_include": (
        "Remove exclusions only for the selected platforms. This does not override format limits, account permissions, source-account protection or repeat intervals.",
        "نلغي الاستبعاد للمنصات المحددة فقط. ده لا يتخطى حدود الصيغة أو صلاحيات الحساب أو حماية حساب المصدر أو فاصل الإعادة.",
    ),
    "bulk.help_archive": (
        "Move these items to Archived and remove them from future rotation on every platform. Files remain on the server. Restore them from Archived when needed.",
        "ننقل العناصر للأرشيف ونخرجها من الروتيشن القادم على كل المنصات. الملفات تفضل على السيرفر، وتقدر ترجعها من فلتر مؤرشف.",
    ),
    "bulk.help_restore": (
        "Return archived items with ready files or text to the library. Their saved platform exclusions remain. Items needing preparation can be restored individually from their details.",
        "نرجّع العناصر المؤرشفة التي ملفاتها أو نصوصها جاهزة للمكتبة. استبعادات المنصات المحفوظة تفضل كما هي. لو عنصر محتاج تجهيز، استعده منفردًا من تفاصيله.",
    ),
    "bulk.safety": (
        "Changes stop this person’s plan for review and cancel untouched reservations. They do not start imports, activate a plan or request publishing. Published content and uncertain or started attempts remain in history.",
        "التغييرات توقف خطة الشخص للمراجعة وتلغي الحجوزات التي لم تبدأ. لا تبدأ استيرادًا أو تشغّل خطة أو تطلب نشرًا. المنشور سابقًا والمحاولات التي بدأت أو نتيجتها غير محسومة تفضل في السجل.",
    ),
    "bulk.no_change": (
        "These choices already match. Nothing will be changed or paused.",
        "الاختيارات مطابقة للمحفوظ. لن يتغير شيء ولن تتوقف الخطة.",
    ),
    "bulk.saved": (
        "Changed {count} items. No publishing was requested. Review the person’s preview.",
        "تم تعديل {count} عنصر. لم نطلب أي نشر. راجع معاينة الشخص.",
    ),
    "bulk.saved_mixed": (
        "Changed {count} items; {unchanged} were already unchanged. No publishing was requested. Review the person’s preview.",
        "تم تعديل {count} عنصر؛ {unchanged} عنصر لم يحتج تغييرًا. لم نطلب أي نشر. راجع معاينة الشخص.",
    ),
    "bulk.selection_invalid": (
        "Select 1–{max} different items from this library, then review again.",
        "حدّد من عنصر إلى {max} عنصرًا مختلفًا من المكتبة دي، ثم راجع تاني.",
    ),
    "bulk.action_invalid": ("Choose an action and a valid platform scope.", "اختَر إجراءً ونطاق منصات صحيحًا."),
    "bulk.unavailable": (
        "“{title}” is being prepared or is a duplicate. Select ready, failed or archived items for bulk management.",
        "«{title}» قيد التجهيز أو مكرر. اختَر عناصر جاهزة أو فشلت أو مؤرشفة للإدارة الجماعية.",
    ),
    "bulk.archived_only": (
        "To restore, select only archived items. Use the Archived filter.",
        "للاستعادة، اختَر عناصر مؤرشفة فقط من فلتر مؤرشف.",
    ),
    "bulk.restore_file": (
        "“{title}” has no ready file or text. Open its details to check or prepare it before restoring in bulk.",
        "«{title}» ليس له ملف أو نص جاهز. افتح تفاصيله للفحص أو التجهيز قبل الاستعادة الجماعية.",
    ),
    "bulk.expired": (
        "This review expired or is invalid. Return to the library and select the items again. Nothing changed.",
        "المراجعة انتهت أو غير صالحة. ارجع للمكتبة وحدّد العناصر تاني. لم يتغير شيء.",
    ),
    "bulk.stale": (
        "The selected items or person plan changed after review. Review the current library again; nothing was applied.",
        "العناصر المحددة أو خطة الشخص اتغيرت بعد المراجعة. راجع المكتبة الحالية تاني؛ لم يتم تطبيق شيء.",
    ),
    "bulk.audit_exclude": (
        "Updated exclusions for {count} library items; no publication requested.",
        "تم تحديث استبعاد {count} عنصر من المكتبة؛ دون طلب نشر.",
    ),
    "bulk.audit_include": (
        "Removed selected platform exclusions for {count} library items; no publication requested.",
        "تم إلغاء استبعاد المنصات المحددة لـ {count} عنصر؛ دون طلب نشر.",
    ),
    "bulk.audit_archive": (
        "Archived {count} library items, retaining their files; no publication requested.",
        "تمت أرشفة {count} عنصر مع الاحتفاظ بالملفات؛ دون طلب نشر.",
    ),
    "bulk.audit_restore": (
        "Restored {count} archived library items; no plan activated.",
        "تمت استعادة {count} عنصر من الأرشيف؛ دون تشغيل خطة.",
    ),
}
