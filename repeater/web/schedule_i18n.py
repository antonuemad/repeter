"""Schedule and preview recovery copy, AR/EN."""

COPY = {
    "schedule.daily": ("Every day", "كل يوم"),
    "schedule.custom_days": ("Choose specific days", "أيام محددة"),
    "schedule.days_help": (
        "Every day includes all seven days. Select specific days to use the checkboxes.",
        "كل يوم يشمل الأيام السبعة. اختَر أيام محددة لتغييرها من المربعات.",
    ),
    "schedule.zone_help": (
        "All times below use this zone, including daylight-saving changes.",
        "كل الأوقات بالأسفل بالتوقيت ده، بما فيه تغيير التوقيت الصيفي.",
    ),
    "schedule.extra_help": (
        "Each extra time requests another item per page. Clear a time to remove it; up to five distinct times.",
        "كل وقت إضافي يحتاج عنصرًا آخر لكل صفحة. امسح الوقت لإلغائه؛ الحد الأقصى ٥ أوقات مختلفة.",
    ),
    "schedule.repeat_help": (
        "Use new content first, then the least recently used on each page after the minimum repeat interval.",
        "يختار المحتوى الجديد أولًا، ثم الأقدم استخدامًا على كل صفحة بعد انتهاء فاصل الإعادة.",
    ),
    "schedule.once_help": (
        "Use each suitable item once on each page. When the library runs out, leave gaps until new content is ready.",
        "كل عنصر مناسب يُستخدم مرة واحدة على كل صفحة. بعدها يترك مواعيد فارغة لحين تجهيز محتوى جديد.",
    ),
    "schedule.demand": (
        "{days} days a week · {times} times a day · {weekly} requested slots per page each week",
        "{days} يوم في الأسبوع · {times} موعد في اليوم · {weekly} موعدًا مطلوبًا لكل صفحة أسبوعيًا",
    ),
    "schedule.save_help": (
        "Save to calculate a preview from this person’s library. This does not start the plan or publish content.",
        "احفظ لحساب المعاينة من مكتبة الشخص. الحفظ لا يشغّل الخطة ولا ينشر محتوى.",
    ),
    "schedule.days_invalid": (
        "Choose at least one valid day, or choose Every day.",
        "اختَر يومًا واحدًا صحيحًا على الأقل، أو اختَر كل يوم.",
    ),
    "schedule.times_invalid": (
        "Enter 1–5 different times in HH:MM format. Remove repeated times.",
        "أدخل من موعد لـ٥ مواعيد مختلفة بصيغة ساعة:دقيقة، وامسح الأوقات المكررة.",
    ),
    "schedule.start_invalid": ("Choose a valid start date.", "اختَر تاريخ بداية صحيحًا."),
    "schedule.zone_invalid": (
        "Choose a valid time zone from the list.",
        "اختَر منطقة زمنية صحيحة من القائمة.",
    ),
    "schedule.window": (
        "Regular schedule: {start} to {end} · {zone}",
        "المواعيد الأساسية من {start} إلى {end} · {zone}",
    ),
    "schedule.draft_help": (
        "This is a simulation, not a publishing queue. Each item below is a possible choice; later choices assume earlier ones succeed.",
        "دي محاكاة وليست مهام نشر. كل عنصر بالأسفل اختيار محتمل، والمواعيد اللاحقة تفترض نجاح المواعيد السابقة.",
    ),
    "schedule.reason_help": ("Reason and next step", "السبب والخطوة التالية"),
    "schedule.gaps": ("Why are there gaps?", "ليه فيه مواعيد فاضية؟"),
    "schedule.gap_count": ("{n} empty page slots", "{n} موعد صفحة فاضي"),
    "schedule.show_all": ("All times", "كل المواعيد"),
    "schedule.show_gaps": ("Times with gaps", "المواعيد التي فيها فراغات"),
    "schedule.no_gaps": (
        "No gaps in this preview. Show all times to review the content.",
        "لا توجد فراغات في المعاينة دي. اعرض كل المواعيد لمراجعة المحتوى.",
    ),
    "schedule.partial": (
        "Some pages have no item at this time. Open the time to see each page and its reason.",
        "بعض الصفحات ليس لها محتوى في هذا الموعد. افتح الموعد لمعرفة كل صفحة وسبب الفراغ.",
    ),
    "schedule.fix_content": ("Review their content", "راجع محتوى الشخص"),
    "schedule.fix_schedule": ("Review days and rotation", "راجع الأيام والروتيشن"),
    "schedule.fix_accounts": ("Review their pages", "راجع صفحات الشخص"),
    "schedule.fix_activity": ("Review unresolved activity", "راجع النشاط غير المحسوم"),
    "schedule.fix_slot": ("Review this time", "راجع الموعد ده"),
    "schedule.gap_help_cooldown": (
        "The selected or available content is still within its repeat interval. Add suitable content or review the times and any explicit item choice. The interval is never shortened automatically.",
        "المحتوى المختار أو المتاح لهذا الموعد لم يكمل فاصل الإعادة. أضف محتوى مناسبًا أو راجع المواعيد واختيار العنصر إن كنت حددته. الفاصل لا يتم تقصيره تلقائيًا.",
    ),
    "schedule.gap_help_exhausted": (
        "Once-per-page mode has used all remaining suitable items in recorded history or earlier preview slots. Add content or explicitly choose rotation.",
        "وضع مرة واحدة استهلك العناصر المناسبة في السجل أو في المواعيد السابقة من المعاينة. أضف محتوى أو اختَر التدوير بنفسك.",
    ),
    "schedule.gap_help_pending": (
        "Remaining suitable items have pending or uncertain publication results. Resolve their activity first; changing the repeat interval cannot bypass this hold.",
        "العناصر المناسبة المتبقية عليها نشر معلّق أو نتيجة غير مؤكدة. راجع نشاطها أولًا؛ تغيير فاصل الإعادة لا يتخطى التعليق.",
    ),
    "schedule.gap_help_account_disconnected": (
        "This page was disconnected. Reconnect and select it for this person before recalculating.",
        "الصفحة مفصولة. أعد ربطها واختيارها للشخص قبل إعادة الحساب.",
    ),
    "schedule.gap_help_account_reconnect": (
        "Authorization expired or needs renewal. Reconnect the account, then recalculate.",
        "التفويض انتهى أو محتاج تجديد. أعد ربط الحساب ثم احسب المعاينة مجددًا.",
    ),
    "schedule.gap_help_account_unavailable": (
        "This page belongs to another person now. Review the selected pages.",
        "الصفحة تتبع شخصًا آخر حاليًا. راجع الصفحات المختارة.",
    ),
    "schedule.gap_help_excluded": (
        "You excluded this page’s item at this time. Review the time to restore the automatic choice.",
        "تم استبعاد موعد هذه الصفحة. راجع الموعد لاستعادة الاختيار التلقائي.",
    ),
    "schedule.gap_help_replacement_unavailable": (
        "The item selected for this time is no longer suitable or available. Choose another item or restore automatic selection.",
        "العنصر المحدد لهذا الموعد لم يعد متاحًا أو مناسبًا. اختَر عنصرًا آخر أو استعد الاختيار التلقائي.",
    ),
    "schedule.gap_help_processing": (
        "Content is still downloading or being prepared. Check its progress, then recalculate when it is Ready.",
        "المحتوى ما زال قيد التنزيل أو التجهيز. تابع حالته ثم أعد الحساب لما يبقى جاهزًا.",
    ),
    "schedule.gap_help_import_failed": (
        "Content preparation failed. Open the failed items to see the reason and retry.",
        "تجهيز المحتوى فشل. افتح العناصر التي فشلت لمعرفة السبب وإعادة المحاولة.",
    ),
    "schedule.gap_help_empty_library": (
        "There is no ready content in this person’s library. Add files, select a folder or finish importing.",
        "لا يوجد محتوى جاهز في مكتبة الشخص. أضف ملفات أو اختَر فولدرًا أو أكمل الاستيراد.",
    ),
    "schedule.gap_help_incompatible": (
        "Ready content does not match this page’s supported format, dimensions, length or permissions. Review the incompatible-item details.",
        "المحتوى الجاهز غير مناسب لصيغة الصفحة أو أبعادها أو مدة المحتوى أو صلاحياتها. راجع تفاصيل العناصر غير المناسبة.",
    ),
    "schedule.gap_help_same_origin": (
        "All ready items came from this same account. Repeater avoids sending them back to their source. Add other originals.",
        "كل العناصر الجاهزة جاءت من الحساب نفسه. النظام يتجنب إعادتها لمصدرها. أضف محتوى أصليًا آخر.",
    ),
    "schedule.gap_help_excluded_content": (
        "The ready items are excluded from this platform in their library settings. Review those exclusions.",
        "العناصر الجاهزة مستبعدة من هذه المنصة في إعدادات المكتبة. راجع الاستبعادات.",
    ),
    "schedule.gap_help_review_content": (
        "Review item formats, platform exclusions, source accounts and once-per-page history. None of the remaining items is suitable here.",
        "راجع صيغة العناصر واستبعادات المنصة وحساب المصدر وسجل المرة الواحدة؛ لا يوجد عنصر متبقٍ مناسب لهذه الصفحة.",
    ),
    "schedule.gap_help_no_eligible": (
        "No remaining suitable content can fill this page’s time. Review this person’s library.",
        "لا يوجد محتوى مناسب متبقٍ لهذا الموعد. راجع مكتبة الشخص.",
    ),
    "schedule.available_help": (
        "This is the earliest eligibility time, not a reserved publication. The next suitable selected time may be later.",
        "ده أقرب وقت للسماح بالإعادة، وليس موعد نشر محجوزًا. الموعد المختار المناسب التالي قد يكون بعده.",
    ),
    "schedule.off_day": (
        "This weekday is not selected in the person’s schedule.",
        "اليوم ده غير مختار في مواعيد الشخص.",
    ),
    "schedule.before_start": ("This plan starts on {at}.", "الخطة تبدأ يوم {at}."),
    "schedule.outside_window": (
        "This day is outside the current 30-day preview.",
        "اليوم ده خارج فترة المعاينة الحالية ذات الـ٣٠ يومًا.",
    ),
    "schedule.elapsed": (
        "All selected times for this day have passed. Only future times appear.",
        "كل المواعيد المختارة لهذا اليوم انتهت. المعاينة تعرض المواعيد القادمة فقط.",
    ),
    "schedule.clock_gap": (
        "Selected local times do not exist on this day because the clock moves forward. Choose another time.",
        "الأوقات المحلية المختارة غير موجودة في اليوم ده بسبب تقديم الساعة. اختَر وقتًا آخر.",
    ),
    "schedule.no_pages": (
        "Select pages for this person to calculate their times.",
        "اختَر صفحات للشخص لحساب مواعيده.",
    ),
    "schedule.no_upcoming": (
        "No future preview time remains on this day. A moved time may appear on another day.",
        "لا يوجد موعد قادم متبقٍ في اليوم ده. الموعد المنقول قد يظهر في يوم آخر.",
    ),
    "schedule.gap_title_processing": ("Content is being prepared", "المحتوى قيد التجهيز"),
    "schedule.gap_title_import_failed": ("Preparation failed", "تجهيز المحتوى فشل"),
    "schedule.gap_title_empty_library": ("Library has no ready content", "لا يوجد محتوى جاهز"),
    "schedule.gap_title_incompatible": ("Content is incompatible with this page", "المحتوى غير مناسب للصفحة"),
    "schedule.gap_title_same_origin": ("Content came from this same account", "المحتوى جاء من نفس الحساب"),
    "schedule.gap_title_excluded_content": (
        "Content excluded from this platform",
        "المحتوى مستبعد من هذه المنصة",
    ),
    "schedule.gap_title_review_content": ("Review content eligibility", "راجع مناسبة المحتوى للصفحة"),
}
