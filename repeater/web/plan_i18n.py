"""Copy for the person-led draft planning flow; kept together in both supported languages."""

from repeater.web.media_i18n import COPY as MEDIA_COPY
from repeater.web.schedule_i18n import COPY as SCHEDULE_COPY

COPY = {
    "plan.execution": ("Plan execution", "تشغيل الخطة"),
    "plan.running": ("Plan enabled", "الخطة مفعّلة"),
    "plan.stopped": ("Plan stopped", "الخطة متوقفة"),
    "plan.pause": ("Stop this plan", "إيقاف الخطة"),
    "plan.start_execution": ("Start this plan", "تشغيل الخطة"),
    "plan.actual_schedule": ("Open the actual queue", "فتح طابور النشر الفعلي"),
    "plan.owner_execution": (
        "Only the workspace owner can start or stop this plan.",
        "مالك مساحة العمل فقط يقدر يشغّل الخطة أو يوقفها.",
    ),
    "plan.execution_locked": (
        "Testing lock: plan activation and all publication requests are blocked on this server. Preview remains available.",
        "قفل الاختبار: تشغيل الخطط وكل طلبات النشر ممنوعة على السيرفر. المعاينة متاحة.",
    ),
    "plan.workspace_paused": (
        "The workspace is paused. Resume it in Workspace settings before starting a plan.",
        "مساحة العمل متوقفة. استأنفها من إعدادات مساحة العمل قبل تشغيل الخطة.",
    ),
    "plan.execution_help": (
        "After activation, the worker reserves upcoming slots from this person's library. Confirmed publications determine the repeat gap. Editing pages, times or rotation stops the plan for review. The preview never starts it.",
        "بعد التشغيل، النظام بيحجز المواعيد القادمة من مكتبة الشخص. النشر المؤكد بيحدد فاصل التكرار. تعديل الصفحات أو المواعيد أو الروتيشن بيوقف الخطة للمراجعة. فتح المعاينة لا يشغّلها.",
    ),
    "plan.started": (
        "Plan enabled. The worker will fill upcoming eligible slots.",
        "تم تفعيل الخطة. النظام هيملأ المواعيد القادمة المناسبة.",
    ),
    "plan.paused": (
        "Plan stopped; untouched reservations canceled. Already sent content may be live.",
        "تم إيقاف الخطة وإلغاء الحجوزات التي لم تُرسل. المحتوى الذي أُرسل بالفعل قد يكون منشورًا.",
    ),
    "plan.latest30_option": ("Latest 30 + new videos", "آخر ٣٠ فيديو والجديد"),
    "plan.new_option": ("New videos only", "الفيديوهات الجديدة فقط"),
    "plan.platforms": ("Import from their platforms", "الاستيراد من منصاته"),
    "plan.platform_help": (
        "Choose an owned Instagram professional account or Facebook Page. Import up to its latest 30 videos and reels, then check for new videos about every 15 minutes. Ready videos join this person’s rotation preview automatically.",
        "اختار حساب Instagram احترافي أو صفحة Facebook بتملكها. استورد آخر ٣٠ فيديو وريل بحد أقصى، وبعدها بنتابع الجديد كل ١٥ دقيقة تقريبًا. الفيديوهات الجاهزة بتدخل معاينة روتيشن الشخص تلقائيًا.",
    ),
    "plan.platform_limits": (
        "Only videos the platform makes available can be downloaded. Photos, carousel albums and stories are excluded. For unavailable files or YouTube originals, use Drive. TikTok import is deferred.",
        "التنزيل للفيديوهات اللي المنصة بتتيح ملفاتها فقط. الصور والألبومات والستوريز مش ضمن الاستيراد. للملفات غير المتاحة أو أصول YouTube استخدم Drive. استيراد TikTok مؤجل.",
    ),
    "plan.source_account": ("Source account", "حساب المصدر"),
    "plan.pick_source": ("Select an account", "اختار حسابًا"),
    "plan.import_mode": ("On the first check", "في أول فحص"),
    "plan.latest30": ("Latest 30 videos, then follow new videos", "آخر ٣٠ فيديو، وبعدها متابعة الجديد"),
    "plan.new_only": ("New videos from when monitoring starts", "الفيديوهات الجديدة من وقت بدء المتابعة"),
    "plan.add_source": ("Save source", "حفظ المصدر"),
    "plan.source_save_help": (
        "Saving assigns this account to this person; it does not start importing. Start monitoring below when ready. This source account is excluded when rotating its own videos.",
        "الحفظ بيخصص الحساب للشخص ده، من غير بدء الاستيراد. شغّل المتابعة بالأسفل لما تكون جاهز. حساب المصدر بيتستبعد من روتيشن فيديوهاته الأصلية.",
    ),
    "plan.no_source_accounts": (
        "No other eligible accounts. Connect Instagram or Facebook above; accounts assigned to another person cannot be chosen.",
        "مفيش حسابات أخرى متاحة. اربط Instagram أو Facebook بالأعلى؛ الحساب المخصص لشخص آخر مش متاح للاختيار.",
    ),
    "plan.no_platform_sources": (
        "No platform sources yet. You can use Drive, platform sources, or both.",
        "لسه مفيش مصادر منصات. تقدر تستخدم Drive أو مصادر المنصات أو الاتنين.",
    ),
    "plan.watch_on": ("Monitoring on · about every 15 minutes", "المتابعة شغّالة · كل ١٥ دقيقة تقريبًا"),
    "plan.watch_off": ("Monitoring paused", "المتابعة متوقفة"),
    "plan.watch_start": ("Start / resume monitoring", "بدء / استئناف المتابعة"),
    "plan.watch_pause": ("Pause monitoring", "إيقاف المتابعة مؤقتًا"),
    "plan.watch_check": ("Check now", "فحص الآن"),
    "plan.watch_help": (
        "Pausing stops new checks; files already queued can finish. Resuming keeps the same library and does not repeat the initial archive import. After three failed checks monitoring pauses; reconnect or resolve the error, then resume.",
        "الإيقاف بيوقف الفحوص الجديدة؛ الملفات اللي دخلت الطابور ممكن تكمل. الاستئناف بيحافظ على المكتبة من غير إعادة استيراد الأرشيف الأول. بعد ٣ فحوص فاشلة المتابعة بتتوقف؛ أعد الربط أو عالج الخطأ، ثم استأنف.",
    ),
    "plan.source_totals": (
        "{total} imported · {ready} ready · {pending} preparing · {failed} failed · {duplicate} duplicates",
        "{total} مستورد · {ready} جاهز · {pending} قيد التجهيز · {failed} فاشل · {duplicate} مكرر",
    ),
    "plan.sources_summary": ("{n} saved sources", "{n} مصادر محفوظة"),
    "plan.platform_invalid": (
        "Choose an active Instagram or Facebook account belonging to this person. Reconnect it if needed; start monitoring before checking now.",
        "اختار حساب Instagram أو Facebook متصل وتابع للشخص ده. أعد ربطه لو محتاج؛ ابدأ المتابعة قبل الفحص الآن.",
    ),
    "plan.platform_exists": (
        "This source is already saved. Use its monitoring controls below.",
        "المصدر ده محفوظ بالفعل. استخدم أدوات المتابعة الخاصة بيه بالأسفل.",
    ),
    "plan.source_busy": (
        "This account has active legacy imports or pending publications. Pause the old source and resolve pending publications before assigning it.",
        "الحساب ده عليه مصدر استيراد قديم شغّال أو نشر معلّق. أوقف المصدر القديم وعالج النشر المعلّق قبل تخصيصه.",
    ),
    "plan.slot_filled": ("{n}/{total} pages with content", "{n}/{total} صفحة بمحتوى"),
    "nav.plans": ("People & plans", "الأشخاص والخطط"),
    "plan.intro": (
        "Keep each person's videos, images and text posts with their pages and schedule.",
        "اجمع محتوى كل شخص من فيديوهات وصور وبوستات نصية مع صفحاته وجدوله.",
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
        "Use the person's plan to schedule their content; manual publishing is unavailable here.",
        "استخدم خطة الشخص لجدولة محتواه؛ النشر اليدوي غير متاح هنا.",
    ),
    "plan.scope": (
        "Build each person’s library with videos, images and text posts. Choose post, story or short-video formats and preview rotation on compatible pages. Publishing stays locked during testing.",
        "كوّن مكتبة كل شخص من فيديوهات وصور وبوستات نصية. اختار بوست أو ستوري أو فيديو قصير، وعاين الروتيشن على الصفحات المتوافقة. النشر مقفول أثناء الاختبار.",
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
        "Optional: use a Drive folder, or upload files and write posts in this person’s library. Imports include videos directly inside the folder and, if selected below, images. Subfolders are excluded. Saving checks access only; it does not download files.",
        "اختياري: استخدم فولدر Drive، أو ارفع ملفات واكتب بوستات في مكتبة الشخص. الاستيراد يشمل الفيديوهات داخل الفولدر مباشرةً، والصور لو اخترتها بالأسفل. الفولدرات الفرعية مش ضمن الاستيراد. الحفظ بيتحقق من الوصول فقط، من غير تنزيل ملفات.",
    ),
    "plan.backfill": ("Existing files to import (0–200)", "عدد الملفات الحالية للاستيراد (٠–٢٠٠)"),
    "plan.backfill_help": (
        "The newest matching files up to this count on the first import, then newly added files. 0 starts with future additions only.",
        "أحدث ملفات مطابقة بحد أقصى العدد ده في أول استيراد، وبعدها الملفات المضافة حديثًا. صفر يعني الإضافات القادمة فقط.",
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
        "One item per selected time, for each page. Rotation settings decide when content can return; a slot stays empty if no item is eligible.",
        "عنصر واحد في كل موعد لكل صفحة. إعدادات الروتيشن بتحدد إمتى المحتوى ممكن يرجع؛ الموعد بيفضل فاضي لو مفيش عنصر مناسب.",
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
    "plan.rotation": ("Automatic rotation", "الروتيشن التلقائي"),
    "plan.rotation_mode": ("How to use the library", "طريقة استخدام المكتبة"),
    "plan.mode_once": ("Once per page", "مرة واحدة لكل صفحة"),
    "plan.mode_repeat": ("Keep rotating the library", "تدوير المكتبة باستمرار"),
    "plan.rotation_help": (
        "Choose unused content first, then the least recently used on each page. Library order breaks ties. The plan includes newly ready, compatible items whenever you preview it.",
        "الأولوية للمحتوى اللي ما اتستخدمش على الصفحة، وبعدها الأقدم استخدامًا عليها. ترتيب المكتبة بيفصل بين المتساويين. أي عنصر جديد جاهز ومتوافق بيدخل في الحساب تلقائيًا عند المعاينة.",
    ),
    "plan.repeat_days": (
        "Minimum days before repeating on the same page",
        "أقل عدد أيام قبل إعادة نفس المحتوى على نفس الصفحة",
    ),
    "plan.repeat_help": (
        "1–365 days; each day means 24 full hours. Applies when rotation is selected, separately for each page. A small library leaves gaps rather than shortening this wait.",
        "من يوم لـ٣٦٥ يوم؛ اليوم يعني ٢٤ ساعة كاملة. يُطبّق عند اختيار التدوير، بشكل مستقل لكل صفحة. لو المكتبة صغيرة، بنسيب مواعيد فاضية بدل تقصير الفاصل.",
    ),
    "plan.rotation_safe": (
        "Saving rotation settings stops an active plan for review. Start it separately when publishing is available. Previewing does not consume library content.",
        "حفظ إعدادات الروتيشن بيوقف الخطة المفعّلة للمراجعة. شغّلها بشكل منفصل لما النشر يبقى متاح. المعاينة ما بتسجّلش عناصر المكتبة كمحتوى مستخدم.",
    ),
    "plan.rotation_invalid": (
        "Choose once per page or automatic rotation, and a repeat interval from 1 to 365 days.",
        "اختار مرة واحدة لكل صفحة أو تدوير المكتبة، وفاصل إعادة من يوم لـ٣٦٥ يوم.",
    ),
    "plan.rotation_summary": ("Repeat interval: {days} days per page", "فاصل الإعادة: {days} يوم لكل صفحة"),
    "plan.rotation_counts": (
        "{repeated} repeated page slots · {waiting} slots waiting for the repeat interval",
        "{repeated} موعد صفحة لإعادة محتوى · {waiting} موعد في انتظار انتهاء الفاصل",
    ),
    "plan.first_use": ("First use in this page’s recorded history", "أول استخدام في سجل الصفحة المتاح"),
    "plan.repeat_use": ("Repeat", "إعادة محتوى"),
    "plan.previous_preview": ("Previous planned use: {at}", "الاستخدام السابق في المعاينة: {at}"),
    "plan.previous_actual": ("Last recorded publication: {at}", "آخر نشر مسجّل: {at}"),
    "plan.reason_cooldown": ("Waiting for the repeat interval", "في انتظار انتهاء فاصل الإعادة"),
    "plan.eligible_after": ("Eligible again from {at}", "متاح للإعادة بدايةً من {at}"),
    "plan.reason_exhausted": ("All eligible items used once", "كل العناصر المناسبة اتستخدمت مرة"),
    "plan.reason_no_eligible": ("No eligible content in this library", "مفيش محتوى مناسب في المكتبة"),
    "plan.reason_pending": (
        "Remaining items have pending or unresolved publications",
        "المحتوى المتبقي عليه نشر معلّق أو نتيجة غير محسومة",
    ),
    "plan.reason_account_unavailable": (
        "This page is no longer assigned to this person",
        "الصفحة لم تعد مخصّصة للشخص ده",
    ),
    "plan.reason_account_disconnected": (
        "Account disconnected — connect it again",
        "الحساب مفصول — اربطه من جديد",
    ),
    "plan.reason_account_reconnect": (
        "Authorization needs renewal — reconnect this account",
        "التفويض محتاج تجديد — أعد ربط الحساب",
    ),
    "plan.review_pages": ("Review this person’s pages", "راجع صفحات الشخص"),
    "plan.pending_uses": (
        "{n} item/page pairs are held until pending or uncertain publications are resolved. Rotation never bypasses them.",
        "تم تعليق {n} حالة محتوى/صفحة لحين حسم النشر المعلّق أو غير المؤكد. الروتيشن ما بيتخطّاش الحالات دي.",
    ),
    "plan.imports": ("Import files into their library", "استيراد ملفات لمكتبته"),
    "plan.import_help": (
        "Importing downloads and prepares videos and optionally images from the folder. Start it separately when ready. The publishing lock stays on.",
        "الاستيراد بينزّل ويجهّز فيديوهات الفولدر والصور لو اخترت ضمها. شغّله بشكل منفصل لما تكون جاهز. قفل النشر بيفضل شغّال.",
    ),
    "plan.import_start": ("Start importing files", "بدء استيراد الملفات"),
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
        "A fresh simulation from the current library and recorded publication history. Later slots assume earlier draft slots succeed. Viewing it never records a use or creates publishing jobs. Platform approvals and publishing settings are still required before launch.",
        "محاكاة جديدة من المكتبة الحالية وسجل النشر المتاح. المواعيد اللاحقة بتفترض نجاح المواعيد السابقة في المسودة. فتحها ما بيسجّلش استخدامًا وما بينشئش مهام نشر. الإطلاق ما زال محتاج موافقات المنصات وإعداداتها.",
    ),
    "plan.coverage": (
        "{ready} ready items · {filled} filled page slots · {empty} empty page slots",
        "{ready} عنصر جاهز · {filled} موعد صفحة بمحتوى · {empty} موعد صفحة فاضي",
    ),
    "plan.empty_slot": (
        "Check why each page has no content assigned",
        "راجع سبب عدم التجهيز لكل صفحة",
    ),
    "plan.no_targets": (
        "Select pages to see where each item would go.",
        "اختار الصفحات عشان تشوف كل محتوى هيروح فين.",
    ),
    "plan.no_videos": (
        "No ready content yet. Your times appear below; add content or wait for preparation to finish.",
        "مفيش محتوى جاهز لسه. المواعيد ظاهرة بالأسفل؛ أضف محتوى أو انتظر اكتمال تجهيزه.",
    ),
    "plan.no_slots": (
        "No future slots in this 30-day window. Adjust the days or times.",
        "مفيش مواعيد قادمة في فترة الـ٣٠ يوم دي. عدّل الأيام أو المواعيد.",
    ),
    "plan.slot_count": ("{n} page destinations", "{n} وجهة نشر"),
    "plan.excluded": (
        "{n} item/page combinations excluded by format, previous use or content overrides.",
        "استُبعدت {n} حالة محتوى/صفحة بسبب التنسيق أو الاستخدام السابق أو استثناءات المحتوى.",
    ),
    "plan.warnings": (
        "Accounts needing attention before any future publishing",
        "حسابات محتاجة مراجعة قبل أي نشر لاحق",
    ),
    "plan.truncated": (
        "Preview uses the first 1,000 ready items in your selected order.",
        "المعاينة بتستخدم أول ١٠٠٠ عنصر جاهز حسب الترتيب المختار.",
    ),
    "plan.preview_limits": (
        "Times use this plan’s zone. Nonexistent daylight-saving times are skipped; repeated times occur once. Execution also checks destination settings, minimum gaps, pending work and upload quotas. Open the actual queue for committed reservations.",
        "المواعيد حسب منطقة الخطة. مواعيد التوقيت الصيفي غير الموجودة بتتخطّى والمتكررة بتحصل مرة. التنفيذ بيراجع إعدادات الوجهة وأقل فاصل والعمل المعلّق وحصص الرفع. افتح طابور النشر الفعلي للحجوزات المؤكدة.",
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

COPY.update(
    {
        "wizard.title": ("Set up your rotation", "جهّز روتيشن محتواك"),
        "wizard.intro": (
            "Connect your pages, choose your content and preview the next 30 days.",
            "اربط صفحاتك، اختار محتواك، وشوف معاينة الـ٣٠ يوم الجايين.",
        ),
        "wizard.exit": ("Exit setup", "خروج والرجوع لاحقًا"),
        "wizard.progress": ("Setup steps", "خطوات الإعداد"),
        "wizard.accounts": ("Accounts & pages", "الحسابات والصفحات"),
        "wizard.content": ("Content", "المحتوى"),
        "wizard.schedule": ("Rotation", "الروتيشن"),
        "wizard.preview": ("Preview", "المعاينة"),
        "wizard.person_title": ("Whose content are we preparing?", "هنجهّز محتوى مين؟"),
        "wizard.person_help": (
            "Use the name of the person or brand. Their library and selected pages stay together.",
            "اكتب اسم الشخص أو البراند. مكتبته وصفحاته المختارة هيفضلوا مع بعض.",
        ),
        "wizard.create": ("Create person and connect accounts", "إنشاء الشخص وربط حساباته"),
        "wizard.saved_help": (
            "Each save keeps your progress. You can return to any step; save changes before leaving it.",
            "كل ضغطة حفظ بتحتفظ بتقدمك. تقدر ترجع لأي خطوة؛ احفظ التعديلات قبل مغادرتها.",
        ),
        "wizard.locked": (
            "Publishing is locked. You can prepare content and preview; nothing will be published.",
            "قفل النشر شغّال. جهّز المحتوى وعاين الجدول؛ مش هيتنشر أي شيء.",
        ),
        "wizard.preview_only": (
            "This setup saves your choices and previews them. It does not activate publishing.",
            "الإعداد ده بيحفظ اختياراتك ويعرض معاينتها، من غير تشغيل النشر.",
        ),
        "wizard.accounts_title": ("Connect and choose your pages", "اربط حساباتك واختار الصفحات"),
        "wizard.accounts_help": (
            "Sign in with each platform and approve only the accounts you want to use. You "
            "return here to choose this person’s pages.",
            "سجّل الدخول في كل منصة ووافق على الحسابات اللي عايز تستخدمها فقط. هترجع هنا "
            "لاختيار صفحات الشخص ده.",
        ),
        "wizard.connected": ("{n} available connections", "{n} حساب متصل متاح"),
        "wizard.not_connected": ("No available connection yet", "مفيش حساب متصل متاح لسه"),
        "wizard.reconnect": (
            "Some accounts need to be connected again before selecting them.",
            "في حسابات محتاجة إعادة ربط قبل اختيارها.",
        ),
        "wizard.manage_connection": ("Connect / reconnect", "ربط حساب أو إعادة الربط"),
        "wizard.connect": ("Connect account", "ربط الحساب"),
        "wizard.pages_title": ("Which pages belong in this rotation?", "أنهي صفحات تدخل في الروتيشن؟"),
        "wizard.pages_help": (
            "Select at least one page below and save. Connecting a platform alone does not select its pages.",
            "حدّد صفحة واحدة على الأقل بالأسفل واحفظ. ربط المنصة لوحده مش بيختار صفحاتها.",
        ),
        "wizard.pick_pages": (
            "Select at least one active page to continue, or connect an account above.",
            "اختار صفحة متصلة واحدة على الأقل عشان تكمل، أو اربط حسابًا بالأعلى.",
        ),
        "wizard.save_pages": ("Save pages and choose content", "حفظ الصفحات واختيار المحتوى"),
        "wizard.content_title": ("Where is your original content?", "محتواك الأصلي موجود فين؟"),
        "wizard.content_help": (
            "Choose a Drive folder, or add files manually below. Ready items join this "
            "person’s library and rotation preview.",
            "اختار فولدر من Drive، أو أضف ملفاتك يدويًا بالأسفل. المحتوى الجاهز بيدخل مكتبة "
            "الشخص ومعاينة الروتيشن.",
        ),
        "wizard.drive_permission": (
            "Google currently asks for read/download access to Drive files. Repeater "
            "imports from the folder you select. Review the permission before approving.",
            "Google بيطلب حاليًا صلاحية قراءة وتنزيل ملفات Drive. Repeater بيستورد من "
            "الفولدر اللي تختاره. راجع الصلاحية قبل الموافقة.",
        ),
        "wizard.change_folder": ("Selected folder", "الفولدر المختار"),
        "wizard.import_title": ("Prepare the folder’s content", "جهّز محتوى الفولدر"),
        "wizard.import_help": (
            "Saving the folder does not start downloads. Start importing when ready, then "
            "refresh to see preparation progress. Importing never unlocks publishing.",
            "حفظ الفولدر مش بيبدأ التنزيل. ابدأ الاستيراد لما تكون جاهز، وبعدها حدّث الحالة "
            "لمتابعة التجهيز. الاستيراد ما بيفتحش قفل النشر.",
        ),
        "wizard.library_status": (
            "Library: {ready} ready · {processing} preparing · {failed} failed",
            "المكتبة: {ready} جاهز · {processing} قيد التجهيز · {failed} تعذّر تجهيزه",
        ),
        "wizard.last_check": ("Last folder check: {at}", "آخر فحص للفولدر: {at}"),
        "wizard.refresh": ("Refresh status", "تحديث الحالة"),
        "wizard.alternative": ("Upload files or use another source", "رفع ملفات أو استخدام مصدر آخر"),
        "wizard.manual_help": (
            "Add videos, images or text in this person’s library, then use “Return to setup” "
            "to continue here.",
            "أضف فيديوهات أو صورًا أو نصوصًا في مكتبة الشخص، وبعدها اضغط «الرجوع للإعداد» عشان تكمل هنا.",
        ),
        "wizard.to_schedule": ("Continue to rotation", "متابعة لإعداد الروتيشن"),
        "wizard.back": ("Previous step", "الخطوة السابقة"),
        "wizard.empty_help": (
            "You can set your times now. The preview will show empty slots until content is ready.",
            "تقدر تحدد المواعيد دلوقتي. المعاينة هتعرض مواعيد فاضية لحد ما المحتوى يبقى جاهز.",
        ),
        "wizard.schedule_title": ("When should content rotate?", "المحتوى يتوزّع إمتى؟"),
        "wizard.schedule_help": (
            "Start with one time on your chosen days. The repeat interval applies separately "
            "to each page; gaps stay empty when no content is eligible.",
            "ابدأ بموعد واحد في الأيام اللي تختارها. فاصل التكرار بيتحسب لكل صفحة لوحدها؛ "
            "الموعد بيفضل فاضي لو مفيش محتوى مناسب.",
        ),
        "wizard.daily_time": ("Time on each selected day", "الوقت في كل يوم مختار"),
        "wizard.extra_times": ("More than once a day", "أكثر من مرة في اليوم"),
        "wizard.order": ("Library order", "ترتيب المحتوى"),
        "wizard.preview_title": ("Your next 30 days", "معاينة الـ٣٠ يوم الجايين"),
        "wizard.preview_help": (
            "Review each item, destination and repeat. Empty slots and incompatible formats "
            "are explained below.",
            "راجع كل محتوى وصفحته وتكراره. المواعيد الفاضية والصيغ غير المناسبة موضّحة بالأسفل.",
        ),
        "wizard.refresh_preview": ("Recalculate preview", "إعادة حساب المعاينة"),
        "wizard.add_content": (
            "No ready content yet — return to content preparation",
            "مفيش محتوى جاهز لسه — ارجع لتجهيز المحتوى",
        ),
        "wizard.finish": ("Finish setup", "إنهاء الإعداد"),
        "wizard.edit_schedule": ("Edit rotation times", "تعديل مواعيد الروتيشن"),
        "wizard.advanced": ("Advanced settings", "الإعدادات المتقدمة"),
        "wizard.resume": ("Return to setup", "الرجوع للإعداد"),
        "wizard.start": ("Start guided setup", "بدء الإعداد خطوة بخطوة"),
        "wizard.folder_options": ("Import options", "خيارات الاستيراد"),
    }
)

COPY["wizard.preview_notes"] = ("How this preview works", "إزاي بنحسب المعاينة؟")

COPY.update(
    {
        "nav.plans": ("People", "الأشخاص"),
        "nav.overview": ("Home", "الرئيسية"),
        "nav.calendar": ("Calendar", "الجدول"),
        "simple.change_person": ("Choose another person", "اختيار شخص آخر"),
        "simple.home_title": ("Your next step", "خطوتك الجاية"),
        "simple.home_intro": (
            "Keep the pages, content and schedule for each person together. Start with the first unfinished step.",
            "حسابات كل شخص ومحتواه ومواعيده في مكان واحد. كمّل أول خطوة ناقصة.",
        ),
        "simple.next_person": ("Add your first person", "أضف أول شخص"),
        "simple.next_accounts": ("Choose their pages", "اختار صفحاته"),
        "simple.next_content": ("Add their content", "أضف محتواه"),
        "simple.next_schedule": ("Set their times", "حدّد مواعيده"),
        "simple.person_summary": (
            "{pages} selected pages · {items} ready items",
            "{pages} صفحة مختارة · {items} محتوى جاهز",
        ),
        "simple.ready_preview": ("Ready to preview", "جاهز للمعاينة"),
        "simple.times": ("Edit times", "تعديل المواعيد"),
        "simple.more_activity": ("Activity and workspace health", "النشاط وحالة مساحة العمل"),
        "simple.upload": ("Upload from your device", "ارفع من جهازك"),
        "simple.upload_hint": ("A video or image you already have", "فيديو أو صورة موجودة عندك"),
        "simple.file_details": ("Title and caption (optional)", "العنوان والوصف (اختياري)"),
        "simple.text_hint": ("Write a Facebook text post", "اكتب بوست نصي لفيسبوك"),
        "simple.drive": ("Use a Google Drive folder", "اختار فولدر Google Drive"),
        "simple.drive_hint": (
            "Import your files and check for new ones",
            "استورد ملفاتك وتابع الجديد في الفولدر",
        ),
        "simple.platform_import": ("Import from a connected platform", "استورد من منصة مربوطة"),
        "simple.platform_hint": ("Owned Facebook or Instagram content", "محتواك على فيسبوك أو إنستجرام"),
        "simple.platform_help": (
            "Facebook and Instagram support owned-content imports. Open source settings to choose the account and recent items. TikTok currently offers public-video reading only; YouTube import is unavailable.",
            "تقدر تستورد محتواك من فيسبوك وإنستجرام. افتح إعدادات المصدر لاختيار الحساب والمحتوى الأخير. تيكتوك متاح لقراءة قائمة الفيديوهات العامة حاليًا، واستيراد يوتيوب غير متاح.",
        ),
        "simple.library_help": (
            "Add content, open an item to edit it, then preview where it will appear.",
            "أضف محتواك، وافتح أي عنصر لتعديله، وبعدها عاين توزيعه على الصفحات.",
        ),
        "simple.library_for": ("Whose library?", "مكتبة مين؟"),
        "simple.open_library": ("Open library", "فتح المكتبة"),
        "simple.open_calendar": ("Open their calendar", "فتح جدول الشخص"),
        "simple.zone": (
            "All times use {zone}. Change it in extra options below.",
            "كل المواعيد بتوقيت {zone}. تقدر تغيّره من الخيارات الإضافية بالأسفل.",
        ),
        "simple.schedule_options": (
            "Start date, time zone and content order",
            "تاريخ البداية والتوقيت وترتيب المحتوى",
        ),
        "simple.connect_more": ("Connect another account", "ربط حساب جديد"),
        "simple.preview_details": ("Distribution and repeat details", "تفاصيل التوزيع والتكرار"),
        "simple.assign_person": ("Choose a person for this page", "اختيار شخص للصفحة"),
        "overview.step_schedule": ("Choose the days and times", "حدّد الأيام والمواعيد"),
        "overview.step_schedule_help": (
            "A daily time is ready by default. Review it before your preview.",
            "في موعد يومي مبدئي جاهز. راجعه قبل معاينة الجدول.",
        ),
        "wizard.title": ("Set up a person", "تجهيز شخص"),
        "wizard.schedule": ("Times", "المواعيد"),
        "wizard.pages_title": ("Which pages should receive their content?", "محتواه يروح لأنهي صفحات؟"),
        "wizard.to_schedule": ("Continue to times", "متابعة لتحديد المواعيد"),
        "wizard.edit_schedule": ("Edit times", "تعديل المواعيد"),
        "wizard.content_help": (
            "Choose one way to add your content. You can add more later, and your library stays with this person.",
            "اختار طريقة لإضافة محتواك. تقدر تضيف المزيد بعدين، والمكتبة بتفضل خاصة بالشخص ده.",
        ),
    }
)

COPY.update(
    {
        "nav.library": ("Content", "المحتوى"),
        "simple.person_summary": (
            "Connected pages: {pages} · Compatible items: {items}",
            "{pages} صفحة مختارة متصلة · {items} عنصر متوافق",
        ),
        "hub.accounts": ("Pages", "الصفحات"),
        "hub.people": ("All people", "كل الأشخاص"),
        "hub.content": ("Content", "المحتوى"),
        "hub.schedule": ("Times", "المواعيد"),
        "hub.preview": ("Preview", "المعاينة"),
        "hub.navigation": ("This person's pages, content and schedule", "صفحات الشخص ومحتواه وجدوله"),
        "hub.summary": (
            "Connected pages: {pages} · Compatible library items: {items}",
            "{pages} صفحة مختارة متصلة · {items} عنصر متوافق من المكتبة",
        ),
        "hub.next": ("Next:", "الخطوة الجاية:"),
        "hub.prepared": (
            "Preparation is ready. Review the preview; it does not start publishing.",
            "التجهيز جاهز. راجع المعاينة؛ فتحها لا يشغّل النشر.",
        ),
        "hub.uncovered": (
            "{n} selected pages have no compatible content yet",
            "{n} صفحة مختارة لسه محتاجة محتوى متوافق",
        ),
        "hub.complete": ("Prepared", "جاهز"),
        "hub.review": ("Needs review", "يحتاج مراجعة"),
        "hub.default_schedule": (
            "These are suggested daily times. Review and save them to confirm this section.",
            "دي مواعيد يومية مقترحة. راجعها واحفظها عشان تأكد الإعداد.",
        ),
        "hub.recent": ("Recent content", "آخر محتوى أضفته"),
        "hub.all_content": ("Manage all content", "إدارة كل المحتوى"),
        "hub.saved_help": (
            "Save each change before switching sections. Connecting or saving never starts publishing.",
            "احفظ التعديل قبل تغيير التبويب. الربط والحفظ لا يشغّلان النشر.",
        ),
        "hub.open_person": ("Open person", "فتح الشخص"),
        "hub.settings": ("Settings & activity", "الإعدادات والسجل"),
        "hub.unsupported_format": ("Not supported for this content format", "غير متاح لشكل المحتوى ده"),
        "hub.recovery_help": (
            "Enter your account email to request a password-reset link.",
            "اكتب بريد حسابك لطلب رابط استعادة كلمة المرور.",
        ),
        "hub.recovery_received": (
            "If this email is eligible, a reset link will be sent. Check your inbox and spam folder. Contact support if it does not arrive.",
            "لو البريد مؤهل للاستعادة، هيوصلك رابط. راجع الوارد والبريد غير المرغوب فيه، وتواصل مع الدعم لو الرسالة ما وصلتش.",
        ),
        "overview.setup_progress": ("{done} of 3 preparation sections ready", "{done} من 3 إعدادات جاهزة"),
        "plan.warnings": (
            "Pages that need reconnection or reassignment",
            "صفحات محتاجة إعادة ربط أو مراجعة التخصيص",
        ),
        "accounts.connect_youtube_help": (
            "Connect your channel. Use Drive or upload original files to add its content to your library.",
            "اربط قناتك. لإضافة محتواها للمكتبة، استخدم الملفات الأصلية من Drive أو ارفعها من جهازك.",
        ),
        "library.no_posts": (
            "This item has no actual publishing reservations. It may appear in the rotation preview.",
            "العنصر ده ملوش حجوزات نشر فعلية. ممكن يظهر في معاينة الجدول.",
        ),
    }
)

COPY.update(
    {
        "import.search": ("Search folders by name", "ابحث باسم الفولدر"),
        "import.search_help": (
            "Search folders accessible to the selected Google account. Or leave the name blank to browse.",
            "ابحث عن فولدر متاح لحساب Google المختار، أو اترك الاسم فارغًا لعرض الفولدرات.",
        ),
        "import.paste": ("Have a folder link?", "معاك رابط الفولدر؟"),
        "import.paste_help": (
            "Paste the folder link, then check and save it. Nothing is downloaded yet.",
            "الصق رابط الفولدر ثم تحقّق منه واحفظه. لن يتم تنزيل ملفات في هذه الخطوة.",
        ),
        "import.folder_saved": (
            "Folder saved. Review it below, then start importing. Publishing remains separate.",
            "تم حفظ الفولدر. راجعه ثم اضغط بدء الاستيراد. النشر خطوة منفصلة.",
        ),
        "import.started": (
            "Import requested. Progress appears in the import status below; no content is published.",
            "تم طلب الاستيراد. تابع حالته بالأسفل؛ لا يتم نشر أي محتوى.",
        ),
        "import.pause_saved": (
            "Folder monitoring paused. Files already being prepared may finish.",
            "تم إيقاف متابعة الفولدر. الملفات التي بدأ تجهيزها قد تكمل المعالجة.",
        ),
        "import.scope": (
            "Only supported files directly inside this folder are imported. Subfolders are not included.",
            "يتم استيراد الملفات المدعومة داخل الفولدر مباشرةً. الفولدرات الفرعية غير مشمولة.",
        ),
        "import.no_matches": (
            "No matching folders on this page. Try another name, check the Google account, or paste a folder link.",
            "لا توجد فولدرات مطابقة في هذه الصفحة. جرّب اسمًا آخر أو راجع حساب Google أو الصق رابط الفولدر.",
        ),
        "import.results": (
            "Choose one folder. Selection checks access and saves it; importing starts only when you ask.",
            "اختَر فولدرًا واحدًا. الاختيار يتحقق من الوصول ويحفظه؛ الاستيراد يبدأ بطلب منك فقط.",
        ),
        "import.selected": ("Selected folder", "الفولدر المختار"),
        "import.open_drive": ("Open folder in Drive", "افتح الفولدر في Drive"),
        "import.choose_help": (
            "Choose the folder, then start importing below. Your originals stay in Drive.",
            "اختَر الفولدر ثم ابدأ الاستيراد بالأسفل. ملفاتك الأصلية تفضل في Drive.",
        ),
        "import.title": ("Import progress", "حالة الاستيراد"),
        "import.refresh": ("Refresh status", "تحديث الحالة"),
        "import.auto": (
            "Status updates automatically while this page is visible. No publishing takes place.",
            "الحالة تتحدث تلقائيًا والصفحة مفتوحة. لا يتم نشر أي محتوى.",
        ),
        "import.refresh_failed": (
            "Status could not update. Use Refresh status or reload the page.",
            "تعذّر تحديث الحالة. اضغط تحديث الحالة أو أعد تحميل الصفحة.",
        ),
        "import.saved": ("Ready to import", "جاهز لبدء الاستيراد"),
        "import.saved_help": (
            "Folder saved; importing has not started. Use Start importing in the Google Drive section.",
            "الفولدر محفوظ والاستيراد لم يبدأ. اضغط بدء الاستيراد في قسم Google Drive.",
        ),
        "import.queued": ("Waiting for the worker", "في انتظار بدء الفحص"),
        "import.queued_help": (
            "The check is queued. Item counts appear after the source has been checked.",
            "تمت إضافة طلب الفحص للطابور. أعداد العناصر تظهر بعد انتهاء فحص المصدر.",
        ),
        "import.scheduled": ("Next check scheduled", "الفحص القادم مجدول"),
        "import.scheduled_help": (
            "The next check is scheduled. Ready files remain available in the library.",
            "هناك فحص قادم مجدول. الملفات الجاهزة تفضل متاحة في المكتبة.",
        ),
        "import.checking": ("Checking for files", "جاري فحص الملفات"),
        "import.checking_help": (
            "The source is being checked. The final number of files is not known yet.",
            "جاري فحص المصدر. العدد النهائي للملفات غير معروف بعد.",
        ),
        "import.importing": ("Downloading files", "جاري تنزيل الملفات"),
        "import.importing_help": (
            "Files are entering this person’s library. They still need preparation before preview.",
            "الملفات تدخل مكتبة الشخص. تحتاج تجهيزًا قبل ظهورها في معاينة الجدول.",
        ),
        "import.processing": ("Preparing files", "جاري تجهيز الملفات"),
        "import.processing_help": (
            "Downloaded files are being checked and prepared. Large videos can take longer.",
            "يتم فحص وتجهيز الملفات التي نزلت. الفيديوهات الكبيرة قد تحتاج وقتًا أطول.",
        ),
        "import.watching": ("Up to date", "المكتبة محدثة"),
        "import.watching_help": (
            "Ready items are in the library. Monitoring continues for new supported files.",
            "العناصر الجاهزة في المكتبة. المتابعة مستمرة للملفات الجديدة المدعومة.",
        ),
        "import.empty": ("No ready items yet", "لا توجد عناصر جاهزة بعد"),
        "import.empty_help": (
            "Review the import mode and counts below. Check that supported originals are directly in the folder.",
            "راجع وضع الاستيراد والأعداد بالأسفل، وتأكد أن الملفات الأصلية المدعومة داخل الفولدر مباشرةً.",
        ),
        "import.empty_scan": (
            "The last completed check found no supported files. Add videos or supported images directly to this folder; subfolders are not scanned.",
            "آخر فحص مكتمل لم يجد ملفات مدعومة. أضف فيديوهات أو صورًا مدعومة داخل الفولدر مباشرةً؛ الفولدرات الفرعية لا تُفحص.",
        ),
        "import.new_only_help": (
            "New files only: existing files were skipped. Add a new supported file to import it.",
            "تم اختيار الملفات الجديدة فقط: الملفات الموجودة سابقًا لم تُستورد. أضف ملفًا جديدًا مدعومًا لاستيراده.",
        ),
        "import.paused_help": (
            "New checks are stopped. Files already downloading or processing may still finish.",
            "تم إيقاف الفحوصات الجديدة. الملفات التي بدأ تنزيلها أو تجهيزها قد تكمل المعالجة.",
        ),
        "import.failed": ("Some files need attention", "بعض الملفات تحتاج مراجعة"),
        "import.failed_help": (
            "Open a failed item to see the reason and retry it. Other ready items remain usable.",
            "افتح العنصر الذي فشل لمعرفة السبب وإعادة المحاولة. العناصر الجاهزة الأخرى تفضل متاحة.",
        ),
        "import.error": ("Source check failed", "تعذّر فحص المصدر"),
        "import.error_help": (
            "Check access to the folder or source account, then retry using the source controls. Ready files are preserved.",
            "راجع الوصول للفولدر أو حساب المصدر، ثم أعد المحاولة من أزرار التحكم في المصدر. الملفات الجاهزة محفوظة.",
        ),
        "import.reconnect": ("Reconnect required", "محتاج إعادة ربط"),
        "import.reconnect_help": (
            "The source account is disconnected or its access expired. Reconnect it, then resume importing.",
            "حساب المصدر مفصول أو انتهت صلاحيته. أعد ربطه ثم استأنف الاستيراد.",
        ),
        "import.reconnect_action": ("Review source accounts", "مراجعة حسابات المصادر"),
        "import.details": ("Technical details", "تفاصيل الخطأ"),
        "import.counts": (
            "{total} tracked · {ready} ready · {importing} downloading · {processing} preparing",
            "الإجمالي: {total} · جاهز: {ready} · تنزيل: {importing} · تجهيز: {processing}",
        ),
        "import.failed_count": ("{n} failed", "فشل: {n}"),
        "import.duplicate_count": ("{n} duplicates", "مكرر: {n}"),
        "import.archived_count": ("{n} archived", "مؤرشف: {n}"),
        "import.counts_help": (
            "Counts belong to this source only. Repeated checks do not add the same item again.",
            "الأعداد تخص المصدر ده فقط. تكرار الفحص لا يضيف نفس العنصر مرة أخرى.",
        ),
        "import.failed_item": ("Review failed item", "مراجعة عنصر فشل"),
        "import.last_success": ("Last completed check: {at}", "آخر فحص مكتمل: {at}"),
        "import.last_scan": (
            "Last check: {listed} supported files found; {created} new items added.",
            "آخر فحص: {listed} ملف مدعوم؛ تمت إضافة {created} عنصر جديد.",
        ),
        "import.drive_reconnect": (
            "Google Drive access expired. Reconnect Google Drive below, then try again. Your folder entry is kept.",
            "انتهت صلاحية الوصول إلى Google Drive. أعد ربطه بالأسفل ثم حاول مجددًا. اختيار الفولدر لم يضِع.",
        ),
        "import.drive_rate": (
            "Google Drive is limiting requests. Wait a few minutes, then search or save again.",
            "Google Drive يحدّ الطلبات مؤقتًا. انتظر دقائق ثم أعد البحث أو الحفظ.",
        ),
        "import.drive_access": (
            "This folder is unavailable to the selected Google account. Check the account and folder link, and ask its owner for access if needed.",
            "الفولدر غير متاح لحساب Google المختار. راجع الحساب ورابط الفولدر، واطلب من مالكه صلاحية الوصول عند الحاجة.",
        ),
        "import.drive_retry": (
            "Could not reach Google Drive. Your choices are kept. Retry the search or save shortly.",
            "تعذّر الاتصال بـ Google Drive. اختياراتك محفوظة هنا. أعد البحث أو الحفظ بعد قليل.",
        ),
    }
)

COPY.update(
    {
        "import.paused": ("Monitoring paused", "المتابعة متوقفة"),
        "import.auto_stopped": (
            "Automatic updates paused after 20 minutes. Refresh status to continue.",
            "التحديث التلقائي اتوقف بعد ٢٠ دقيقة. اضغط تحديث الحالة للمتابعة.",
        ),
    }
)
COPY.update(
    {
        "import.drive_link": (
            "Choose a folder from the search results or paste its Google Drive folder link.",
            "اختَر فولدرًا من نتائج البحث أو الصق رابط الفولدر من Google Drive.",
        )
    }
)
COPY.update(
    {
        "import.retry": ("Retry folder check", "إعادة محاولة فحص الفولدر"),
        "import.previous_error": ("Previous check error", "خطأ الفحص السابق"),
    }
)
COPY.update(
    {
        "import.platform_saved_help": (
            "Source saved; monitoring has not started. Use Start monitoring in the connected-platform section.",
            "المصدر محفوظ والمتابعة لم تبدأ. اضغط بدء المتابعة في قسم المنصة المربوطة.",
        )
    }
)
COPY.update(SCHEDULE_COPY)
COPY.update(MEDIA_COPY)
EN = {k: v[0] for k, v in COPY.items()}
AR = {k: v[1] for k, v in COPY.items()}
