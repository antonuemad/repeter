"""Copy for the person-led draft planning flow; kept together in both supported languages."""

COPY = {
    "plan.slot_filled": ("{n}/{total} pages with content", "{n}/{total} صفحة بمحتوى"),
    "nav.plans": ("People & plans", "الأشخاص والخطط"),
    "plan.intro": (
        "A person, their content folder and the pages they want to keep active. Start with a video plan.",
        "شخص، وفولدر محتواه، والصفحات اللي عايز يفضل نشط عليها. ابدأ بخطة للفيديوهات.",
    ),
    "plan.new": ("Add a person or brand", "إضافة شخص أو علامة تجارية"),
    "plan.name": ("Person or brand name", "اسم الشخص أو العلامة التجارية"),
    "plan.create": ("Create their plan", "إنشاء خطته"),
    "plan.edit": ("Set up plan", "إعداد الخطة"),
    "plan.preview": ("Preview 30 days", "معاينة ٣٠ يوم"),
    "plan.library": ("Open their library", "فتح مكتبته"),
    "plan.unassigned": ("Unassigned library", "المكتبة غير المخصصة لشخص"),
    "plan.draft": ("Draft · preview only", "مسودة · معاينة فقط"),
    "plan.draft_only": (
        "Person plans are preview-only; publishing is unavailable.",
        "خطط الأشخاص للمعاينة فقط؛ النشر غير متاح.",
    ),
    "plan.scope": (
        "This phase supports video files from Drive. Photos, stories, archive imports and automatic rotation come later.",
        "المرحلة دي لفيديوهات Drive. الصور والستوريز واستيراد أرشيف المنصات والروتيشن التلقائي في مراحل لاحقة.",
    ),
    "plan.setup_help": (
        "Save each step before moving on. Connecting an account returns you here; it does not publish or import anything.",
        "احفظ كل خطوة قبل الانتقال للي بعدها. ربط الحساب بيرجعك هنا، من غير نشر أو استيراد أي محتوى.",
    ),
    "plan.person": ("Person", "الشخص"),
    "plan.connections": ("Connect accounts", "ربط الحسابات"),
    "plan.connect_help": (
        "Sign in with the platform and choose the accounts you own. Then select the pages for this person below.",
        "سجّل الدخول من المنصة واختار الحسابات اللي بتملكها، وبعدها اختار صفحات الشخص ده بالأسفل.",
    ),
    "plan.unavailable": ("Not configured yet", "الربط غير مجهّز بعد"),
    "plan.folder": ("Choose the content folder", "اختيار فولدر المحتوى"),
    "plan.drive": ("Google Drive account", "حساب Google Drive"),
    "plan.pick_drive": ("Select a connected Drive account", "اختار حساب Drive مربوط"),
    "plan.browse": ("Browse folders", "عرض الفولدرات"),
    "plan.folder_link": ("Folder link or ID", "رابط الفولدر أو معرّفه"),
    "plan.folder_help": (
        "Only videos directly inside this folder are imported. Subfolders and photos are excluded. Saving checks access but does not download files.",
        "بنستورد الفيديوهات الموجودة داخل الفولدر مباشرةً، من غير الفولدرات الفرعية أو الصور. الحفظ بيتحقق من الوصول فقط، من غير تنزيل ملفات.",
    ),
    "plan.backfill": ("Existing videos to import (0–200)", "عدد الفيديوهات الحالية للاستيراد (٠–٢٠٠)"),
    "plan.backfill_help": (
        "The newest videos up to this count on the first import, then newly added files. 0 starts with future additions only.",
        "أحدث فيديوهات بحد أقصى العدد ده في أول استيراد، وبعدها الملفات المضافة حديثًا. صفر يعني الإضافات القادمة فقط.",
    ),
    "plan.save_folder": ("Check & save folder", "التحقق من الفولدر وحفظه"),
    "plan.choose_folder": ("Use this folder", "اختيار الفولدر"),
    "plan.no_folders": (
        "No folders on this page. Paste a folder link or try another connected account.",
        "مفيش فولدرات في الصفحة دي. الصق رابط فولدر أو جرّب حسابًا مربوطًا آخر.",
    ),
    "plan.next_folders": ("More folders", "فولدرات أخرى"),
    "plan.targets": ("Choose their pages", "اختيار صفحاته"),
    "plan.targets_help": (
        "Choose up to 100 destinations. A page belongs to one person; removing it from this plan keeps that ownership. Existing rules cannot publish to assigned pages.",
        "اختار لحد ١٠٠ وجهة. كل صفحة بتتبع شخصًا واحدًا؛ إزالتها من الخطة بتحافظ على ملكيتها للشخص. قواعد النشر القديمة مش هتنشر على الصفحات المخصصة.",
    ),
    "plan.search": ("Filter pages by name or platform", "تصفية الصفحات بالاسم أو المنصة"),
    "plan.no_accounts": (
        "Connect an account above to choose its pages.",
        "اربط حسابًا بالأعلى عشان تختار صفحاته.",
    ),
    "plan.no_matches": (
        "No matching pages. Clear the filter to see all pages.",
        "مفيش صفحات مطابقة. امسح البحث لعرض كل الصفحات.",
    ),
    "plan.save_targets": ("Save selected pages", "حفظ الصفحات المختارة"),
    "plan.schedule": ("Set the publishing schedule", "تحديد جدول النشر"),
    "plan.schedule_help": (
        "One video per selected time, for each page. The preview uses each available video once per page; it never repeats an empty library.",
        "فيديو واحد في كل موعد لكل صفحة. المعاينة بتستخدم كل فيديو متاح مرة واحدة لكل صفحة؛ المواعيد اللي مفيش محتوى ليها بتفضل فاضية.",
    ),
    "plan.timezone": ("Time zone", "المنطقة الزمنية"),
    "plan.start": ("Start date", "تاريخ البداية"),
    "plan.days": ("Publishing days", "أيام النشر"),
    "plan.times": ("Times (up to 5 per day)", "المواعيد (لحد ٥ يوميًا)"),
    "plan.time": ("Time {n}", "الموعد {n}"),
    "plan.order": ("Library order", "ترتيب المكتبة"),
    "plan.oldest": ("Oldest first", "الأقدم أولًا"),
    "plan.newest": ("Newest first", "الأحدث أولًا"),
    "plan.save_preview": ("Save & preview schedule", "حفظ ومعاينة الجدول"),
    "plan.imports": ("Bring videos into their library", "إضافة الفيديوهات لمكتبته"),
    "plan.import_help": (
        "Importing downloads and prepares videos only. Start it separately when you are ready. The publishing lock stays on.",
        "الاستيراد بينزّل ويجهّز الفيديوهات فقط. شغّله بشكل منفصل لما تكون جاهز. قفل النشر بيفضل شغّال.",
    ),
    "plan.import_start": ("Start importing videos", "بدء استيراد الفيديوهات"),
    "plan.import_pause": ("Pause imports", "إيقاف الاستيراد مؤقتًا"),
    "plan.import_on": ("Folder monitoring is on", "متابعة الفولدر شغّالة"),
    "plan.import_off": ("Imports are paused", "الاستيراد متوقف"),
    "plan.source_missing": ("Choose a Drive folder first.", "اختار فولدر Drive الأول."),
    "plan.source_locked": (
        "Importing has already started. Keep this folder and account; create another person for a separate library.",
        "الاستيراد بدأ بالفعل. احتفظ بالفولدر والحساب الحاليين؛ أنشئ شخصًا آخر لمكتبة منفصلة.",
    ),
    "plan.source_fixed": (
        "After the first import, this folder and account stay attached to this library.",
        "بعد أول استيراد، الفولدر والحساب بيفضلوا مرتبطين بالمكتبة دي.",
    ),
    "plan.saved": (
        "Saved. Nothing has been scheduled or published.",
        "تم الحفظ. لم تتم جدولة أو نشر أي محتوى.",
    ),
    "plan.stale": (
        "This plan changed in another tab. Reload before saving again.",
        "الخطة اتغيرت في تبويب تاني. أعد تحميل الصفحة قبل الحفظ مرة أخرى.",
    ),
    "plan.drive_invalid": (
        "Select an active Google Drive account and a folder first.",
        "اختار حساب Google Drive متصل وفولدر أولًا.",
    ),
    "plan.drive_failed": (
        "Could not read this Drive folder. Check the link and account access, or reconnect Drive, then retry.",
        "تعذّرت قراءة فولدر Drive. راجع الرابط وصلاحية الحساب، أو أعد ربط Drive، ثم حاول مرة أخرى.",
    ),
    "plan.targets_invalid": (
        "Select up to 100 active pages belonging to this workspace and person.",
        "اختار لحد ١٠٠ صفحة متصلة وتابعة لمساحة العمل والشخص ده.",
    ),
    "plan.account_busy": (
        "A selected page has pending publications. Resolve them before assigning it to a person.",
        "في صفحة مختارة عليها مهام نشر معلّقة. عالجها قبل تخصيص الصفحة لشخص.",
    ),
    "plan.schedule_invalid": (
        "Choose a valid time zone, start date, at least one day and 1–5 different times.",
        "اختار منطقة زمنية وتاريخًا صحيحين، ويومًا واحدًا على الأقل، ومن موعد لـ٥ مواعيد مختلفة.",
    ),
    "plan.date_invalid": (
        "Choose a start date from today through the next year.",
        "اختار تاريخ بداية من النهارده وحتى سنة قادمة.",
    ),
    "plan.preview_help": (
        "A simulation of saved choices, not a live publishing queue. No posts or jobs are created. Platform approval and publishing settings are still required before a future launch.",
        "محاكاة للاختيارات المحفوظة، وليست قائمة نشر فعلية. لا تنشئ منشورات أو مهام. إطلاق النشر لاحقًا ما زال محتاج موافقات المنصات وإعداداتها.",
    ),
    "plan.coverage": (
        "{ready} ready videos · {filled} filled page slots · {empty} empty page slots",
        "{ready} فيديو جاهز · {filled} موعد صفحة بمحتوى · {empty} موعد صفحة فاضي",
    ),
    "plan.empty_slot": ("Waiting for an eligible video", "بانتظار فيديو مناسب"),
    "plan.no_targets": (
        "Select pages to see where each video would go.",
        "اختار الصفحات عشان تشوف كل فيديو هيروح فين.",
    ),
    "plan.no_videos": (
        "The library is empty. Your times appear below; import videos separately to preview actual content.",
        "المكتبة فاضية. المواعيد ظاهرة بالأسفل؛ استورد الفيديوهات بشكل منفصل لمعاينة محتوى فعلي.",
    ),
    "plan.no_slots": (
        "No future slots in this 30-day window. Adjust the days or times.",
        "مفيش مواعيد قادمة في فترة الـ٣٠ يوم دي. عدّل الأيام أو المواعيد.",
    ),
    "plan.slot_count": ("{n} page destinations", "{n} وجهة نشر"),
    "plan.excluded": (
        "{n} video/page combinations excluded by format, previous use or content overrides.",
        "استُبعدت {n} حالة فيديو/صفحة بسبب التنسيق أو الاستخدام السابق أو استثناءات المحتوى.",
    ),
    "plan.warnings": (
        "Accounts needing attention before any future publishing",
        "حسابات محتاجة مراجعة قبل أي نشر لاحق",
    ),
    "plan.truncated": (
        "Preview uses the first 1,000 ready videos in your selected order.",
        "المعاينة بتستخدم أول ١٠٠٠ فيديو جاهز حسب الترتيب المختار.",
    ),
    "plan.preview_limits": (
        "Times use this plan’s zone. Nonexistent daylight-saving times are skipped; repeated times occur once. Legacy rotation rules do not control this draft.",
        "المواعيد حسب المنطقة الزمنية للخطة. مواعيد التوقيت الصيفي غير الموجودة بتتخطى، والمتكررة بتظهر مرة واحدة. قواعد الروتيشن القديمة لا تتحكم في المسودة.",
    ),
    "plan.rename": ("Save name", "حفظ الاسم"),
    "plan.saved_pages": ("{n} pages selected", "تم اختيار {n} صفحة"),
}
for i, (en, ar) in enumerate(
    zip(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"],
        strict=True,
    )
):
    COPY[f"plan.day_{i}"] = (en, ar)
EN = {k: v[0] for k, v in COPY.items()}
AR = {k: v[1] for k, v in COPY.items()}
