"""Bilingual instructions for the implemented customer journey, not platform promises.

Each pair is Arabic, then English. Keep chapter IDs stable for shared help links.
"""

from dataclasses import dataclass

Copy = tuple[str, str]


@dataclass(frozen=True)
class Chapter:
    id: str
    title: Copy
    intro: Copy
    steps: tuple[Copy, ...]
    note: Copy
    destination: str
    action: Copy
    admin_only: bool = False


CHAPTERS = (
    Chapter(
        "access",
        ("الدعوة والدخول واستعادة كلمة المرور", "Invitation, sign-in and password recovery"),
        (
            "ابدأ بالدعوة التي وصلتك من مسؤول Repeater أو مالك مساحة العمل.",
            "Start with the invitation from the Repeater administrator or your workspace owner.",
        ),
        (
            (
                "افتح رابط الدعوة كاملًا من البريد، أو الرابط الخاص الذي أرسله المسؤول لك.",
                "Open the complete invitation link from your email, or the private link shared by the administrator.",
            ),
            (
                "اضغط متابعة، ثم عيّن كلمة مرورك وأكّدها. لا تشارك الرابط أو كلمة المرور مع أي شخص.",
                "Continue, then choose and confirm your password. Keep the link and password private.",
            ),
            (
                "سجّل الدخول، واقرأ سياسة الخصوصية والشروط ووافق بنفسك قبل دخول مساحة العمل.",
                "Sign in, read the privacy policy and terms, and accept them yourself before entering the workspace.",
            ),
            (
                "لو نسيت كلمة المرور، سجّل الخروج وافتح «نسيت كلمة المرور؟» في شاشة الدخول. اكتب بريد حسابك واتبع رابط الاستعادة في البريد.",
                "If you forget your password, sign out and choose Forgot password on the sign-in screen. Enter your account email and follow the recovery link.",
            ),
            (
                "لو الرابط منتهي أو مستخدم، اطلب دعوة جديدة أو رابط استعادة جديدًا. راجع البريد غير المرغوب فيه لو الرسالة لم تصل.",
                "If a link has expired or was used, request a new invitation or recovery link. Check spam if the message is missing.",
            ),
        ),
        (
            "الدعوة والاستعادة لتجهيز حساب Repeater فقط؛ ربط Google أو Meta أو TikTok خطوة منفصلة.",
            "Invitation and recovery set up your Repeater account. Connecting Google, Meta or TikTok is a separate step.",
        ),
        "home",
        ("افتح الرئيسية", "Open Home"),
    ),
    Chapter(
        "navigation",
        ("تعرّف على مساحة العمل", "Find your way around"),
        (
            "مساحة العمل تجمع فريقك، وبداخلها مكتبة وصفحات وخطة مستقلة لكل شخص أو براند.",
            "Your workspace brings the team together, with a separate library, pages and plan for each person or brand.",
        ),
        (
            (
                "في «الرئيسية» ستجد خطوتك الجاية للشخص المختار وعدد المحتوى الجاهز والمشاكل التي تحتاج انتباهك.",
                "Home shows the next setup task for the selected person, ready-content counts and issues needing attention.",
            ),
            (
                "افتح «الأشخاص» لاختيار شخص آخر أو إضافة شخص. راجع اسمه أعلى الصفحة قبل تعديل مكتبته أو جدوله.",
                "Open People to switch people or add someone. Check the name at the top before editing their library or schedule.",
            ),
            (
                "امشِ بالترتيب: الأشخاص ← الحسابات المختارة ← المحتوى ← المواعيد ← المعاينة والتقويم.",
                "Follow this order: person → selected accounts → content → times → preview and calendar.",
            ),
            (
                "من أعلى الصفحة غيّر اللغة أو المظهر. على الموبايل افتح «القائمة الرئيسية» لإظهار الأقسام.",
                "Change the language or theme at the top. On mobile, expand Main menu to show the sections.",
            ),
        ),
        (
            "«أدوات متقدمة» فيها المصادر والقواعد وقائمة الانتظار؛ معالج تجهيز الشخص هو البداية الأسهل.",
            "Advanced tools contains Sources, Rules and the actual queue. The person setup wizard is the easiest starting point.",
        ),
        "people",
        ("افتح الأشخاص", "Open People"),
    ),
    Chapter(
        "person",
        ("أضف شخصًا أو علامة تجارية", "Add a person or brand"),
        (
            "أنشئ شخصًا لكل صاحب محتوى، حتى تظل صفحاته ومكتبته منفصلة عن الآخرين.",
            "Create one person per content owner to keep their pages and library separate.",
        ),
        (
            ("افتح «الأشخاص» ثم «إضافة شخص أو علامة تجارية».", "Open People, then Add a person or brand."),
            (
                "اكتب اسمًا واضحًا، ثم اضغط «إنشاء الشخص وربط حساباته».",
                "Enter a clear name, then choose Create person and connect accounts.",
            ),
            (
                "كمّل خطوات المعالج الأربع: الصفحات، المحتوى، المواعيد، المعاينة.",
                "Complete the four wizard steps: pages, content, times and preview.",
            ),
            (
                "احفظ تعديلات كل خطوة قبل الانتقال. للعودة لاحقًا افتح الشخص من «الأشخاص»؛ سيقترح النظام أول خطوة ناقصة.",
                "Save each step before leaving it. Reopen the person from People later; the next incomplete step is suggested.",
            ),
        ),
        (
            "تجهيز الشخص وحفظ خطته لا يفعّلان النشر. يمكنك الخروج واستكمال التجهيز لاحقًا.",
            "Creating a person and saving the plan do not activate publishing. You can leave and resume later.",
        ),
        "new_person",
        ("أضف شخصًا", "Add a person"),
    ),
    Chapter(
        "accounts",
        ("اربط الحسابات واختر الصفحات", "Connect accounts and select pages"),
        (
            "الربط يتم من شاشة المنصة الرسمية؛ العميل لا يحتاج إنشاء تطبيق مطوّر.",
            "Connections use the platform's own authorization screen. Customers do not need to create developer apps.",
        ),
        (
            (
                "من خطوة «الصفحات» افتح «ربط حساب آخر» إذا لم يكن الحساب موجودًا بالفعل.",
                "In the Pages step, expand Connect another account if the account is not already listed.",
            ),
            (
                "اختر Google / YouTube للقناة، أو Meta لصفحات Facebook والحسابات الاحترافية المرتبطة، أو Instagram للربط المباشر بحساب احترافي.",
                "Choose Google / YouTube for a channel, Meta for Facebook Pages and linked professional accounts, or Instagram to connect a professional account directly.",
            ),
            (
                "سجّل الدخول بنفسك في شاشة المنصة، وراجع الحساب والصفحات والصلاحيات ثم أكمل التفويض. لا تدخل كلمة مرور المنصة داخل Repeater.",
                "Sign in yourself on the platform, review the account, assets and permissions, then authorize. Never enter a platform password inside Repeater.",
            ),
            (
                "بعد الرجوع، حدّد علامات الاختيار بجوار صفحات هذا الشخص فقط، ثم احفظ الصفحات وانتقل للمحتوى.",
                "On return, check only this person's pages, then save the selection and continue to Content.",
            ),
            (
                "لو ظهر «يحتاج إعادة الربط»، أعد التفويض للحساب الصحيح. لو الصفحة غير موجودة، راجع صلاحيتك عليها واختيارها في شاشة Meta، أو تواصل مع المسؤول عند ظهور قيد اعتماد.",
                "If an account needs reconnection, authorize the correct account again. If a page is missing, check your access and Meta asset selection, or contact the administrator about approval restrictions.",
            ),
        ),
        (
            "TikTok في التجربة للربط وقراءة الفيديوهات العامة فقط: لا تمنح صلاحية النشر أو رفع المسودات. ظهور الحساب كمتصل لا يعني أن محتواه استُورد أو أنه اختير للخطة. الصفحة المرتبطة بشخص آخر لا تظهر للاختيار هنا.",
            "During the pilot, TikTok is for connection and public-video reading only: do not grant posting or draft-upload access. Connected does not mean imported or selected for the plan. Pages assigned to another person are not offered here.",
        ),
        "accounts",
        ("افتح الحسابات", "Open Accounts"),
    ),
    Chapter(
        "connection-management",
        ("راجع الحسابات وقراءة TikTok وفصل الربط", "Review connections, TikTok reading and disconnection"),
        (
            "صفحة الحسابات تعرض الاتصالات الحالية وحالتها، بشكل مستقل عن اختيار الصفحات لكل شخص.",
            "Accounts shows current connections and their status, separately from the pages selected for each person.",
        ),
        (
            (
                "افتح «الحسابات» وراجع اسم الحساب والمنصة والحالة قبل استخدامه. استخدم إعادة الربط عند انتهاء الصلاحية أو تغيرها.",
                "Open Accounts and check the account name, platform and status before using it. Reconnect when authorization expires or changes.",
            ),
            (
                "لحساب TikTok المربوط للقراءة، افتح قائمة فيديوهاته العامة، ثم حدّث القائمة أو انتقل لباقي النتائج من الأزرار المتاحة.",
                "For a read-connected TikTok account, open its public-video list, then refresh or page through results using the available controls.",
            ),
            (
                "قراءة TikTok تعرض بيانات الفيديوهات وروابطها فقط؛ لا تضيف ملفات إلى المكتبة ولا تنشر ولا ترفع مسودات.",
                "TikTok reading shows video metadata and links only. It does not add files to the library, publish or upload drafts.",
            ),
            (
                "لإزالة صفحة من خطة شخص، ارجع لخطوة الصفحات وألغِ اختيارها ثم احفظ. هذا لا يفصل اتصال المنصة ولا ينقل الصفحة تلقائيًا لشخص آخر.",
                "To remove a page from a plan, uncheck it in Pages and save. This does not disconnect the platform or automatically reassign the page to someone else.",
            ),
            (
                "لفصل الاتصال نفسه يحتاج المالك فتح «فصل الربط»، وقراءة أثره ثم التأكيد. استخدمه فقط إذا كنت تقصد إيقاف استخدام هذا الاتصال.",
                "To disconnect the platform itself, the owner opens Disconnect, reads the consequences and confirms. Use it only when you intend to stop using that connection.",
            ),
        ),
        (
            "فصل الربط لا يحذف منشورًا من المنصة ولا يثبت محو محتواها. لو حساب TikTok غير متاح لعميل، راجع قيود التطبيق أو Sandbox مع المسؤول؛ لا تطلب صلاحيات نشر لتجربة القراءة.",
            "Disconnecting does not delete platform posts or prove platform data erasure. If TikTok is unavailable to a customer, check app or Sandbox restrictions with the administrator; do not request posting access to test reading.",
        ),
        "connected_accounts",
        ("راجع الحسابات المتصلة", "Review connected accounts"),
    ),
    Chapter(
        "drive",
        ("اختر فولدر Google Drive واستورد محتواه", "Choose a Google Drive folder and import"),
        (
            "الفولدر مصدر يدخل المحتوى إلى مكتبة الشخص. ربط Drive منفصل عن ربط YouTube.",
            "A folder brings content into the person's library. Drive authorization is separate from YouTube.",
        ),
        (
            (
                "افتح خطوة «المحتوى» للشخص، ثم خيار Google Drive، واربط حساب Google الذي يستطيع قراءة الفولدر.",
                "Open the person's Content step, expand Google Drive and connect a Google account that can read the folder.",
            ),
            (
                "راجع صلاحية قراءة وتنزيل ملفات Drive في شاشة Google ووافق بنفسك لو مناسبة لك.",
                "Review Drive read and download access on Google's screen and authorize it yourself if appropriate.",
            ),
            (
                "اختر حساب Drive ثم تصفّح الفولدرات، أو الصق رابط الفولدر. فعّل استيراد الصور إذا أردتها، ثم احفظ الاختيار.",
                "Choose the Drive account and browse folders, or paste a folder link. Include images if wanted, then save the choice.",
            ),
            (
                "اضغط بدء الاستيراد بشكل منفصل. الحفظ وحده لا ينزّل الملفات. بعد ذلك حدّث حالة المكتبة حتى تظهر العناصر «جاهزة».",
                "Start importing separately. Saving alone does not download files. Refresh the library status until items are Ready.",
            ),
            (
                "لإيقاف متابعة الفولدر اضغط إيقاف الاستيراد. العناصر التي بدأت معالجتها قد تكمل التجهيز.",
                "Pause importing to stop further folder checks. Files already being prepared may finish.",
            ),
        ),
        (
            "اختر فولدر محتوى الشخص نفسه واستخدم فقط مواد تملك حق استخدامها. لو الفولدر لا يظهر، تأكد أن حساب Drive المختار يستطيع فتحه. لكل شخص فولدر واحد في المعالج الحالي.",
            "Choose this person's own content folder and use only material you have rights to use. If it is missing, check that the selected Google account can open it. The current wizard saves one folder per person.",
        ),
        "content",
        ("افتح خطوة المحتوى", "Open the Content step"),
    ),
    Chapter(
        "manual-content",
        ("أضف فيديو أو صورة أو بوست نصّي", "Add a video, image or text post"),
        (
            "Drive اختياري؛ يمكنك تجهيز المكتبة برفع ملفات من جهازك أو كتابة بوست.",
            "Drive is optional. Upload files from your device or compose a text post instead.",
        ),
        (
            (
                "افتح خطوة «المحتوى» أو «المكتبة»، وتأكد أن الشخص الصحيح مختار.",
                "Open Content or Library and check the selected person.",
            ),
            (
                "للفيديو أو الصورة افتح خيار الرفع، اختر الملف، ثم العنوان والوصف وشكل المحتوى المتاح، وأرسل الملف.",
                "For video or images, expand Upload, choose a file, enter a title and caption, choose an available format and submit.",
            ),
            (
                "للنص افتح كتابة بوست، واكتب محتوى البوست ثم احفظه. البوستات النصية موجهة لصفحات Facebook.",
                "For text, expand Compose a post, enter the body and save. Text posts target Facebook Pages.",
            ),
            (
                "انتظر اكتمال تجهيز الملف؛ «قيد التجهيز» لا يدخل المعاينة بعد. افتح أي عنصر فاشل لمعرفة السبب قبل إعادة المحاولة.",
                "Wait for preparation to finish. Processing items do not enter the preview yet. Open failed items to check the cause before retrying.",
            ),
        ),
        (
            "رفع الملف هنا يحفظه في مكتبة Repeater فقط، ولا يرفعه كمسودة على أي منصة. عند خطأ في النموذج قد تحتاج اختيار الملف مرة أخرى.",
            "This upload stores the file in Repeater's library only. It does not upload a platform draft. After a form error, you may need to select the file again.",
        ),
        "library",
        ("افتح المكتبة", "Open Library"),
    ),
    Chapter(
        "platform-import",
        ("تابع الفيديوهات الجديدة أو استورد آخر ٣٠ فيديو", "Follow new videos or import the latest 30"),
        (
            "مصادر المنصات المتاحة لمكتبة الشخص حاليًا هي صفحات Facebook وحسابات Instagram الاحترافية المدعومة.",
            "Person-library platform sources currently support Facebook Pages and supported professional Instagram accounts.",
        ),
        (
            (
                "في خطوة المحتوى افتح «استيراد من منصة» ثم إعداد مصادر الشخص.",
                "In Content, expand Import from a platform and open the person's source settings.",
            ),
            (
                "اختر الحساب المصدر المملوك لك. اختر «آخر ٣٠ فيديو ثم متابعة الجديد» أو «الجديد فقط».",
                "Select an owned source account. Choose latest 30 videos then follow new content, or new content only.",
            ),
            (
                "احفظ المصدر، ثم اضغط بدء أو استئناف المتابعة بشكل منفصل. يمكنك فحصه الآن أو إيقافه من نفس القسم.",
                "Save the source, then explicitly start or resume monitoring. You can check now or pause from the same section.",
            ),
            (
                "راجع آخر فحص ونتائج الاستيراد. الفيديو الذي أصبح جاهزًا يدخل معاينة الروتيشن عند تحديثها، مع استبعاد حسابه المصدر كوجهة لنفس الفيديو.",
                "Check the last scan and import results. Ready videos enter the refreshed rotation preview, excluding their original source account as a destination for that video.",
            ),
        ),
        (
            "هذه المصادر تستورد الفيديوهات والريلز التي تتيح المنصة ملفها، وليس الصور أو الألبومات أو الستوريز. قائمة TikTok للقراءة وليست تنزيلًا للمكتبة، ولا يوجد استيراد رسمي لأصول YouTube هنا؛ استخدم الملفات الأصلية أو Drive.",
            "These sources import available video/reel files, not images, albums or stories. TikTok's list is read-only and does not download to the library. YouTube originals are not officially imported here; use original files or Drive.",
        ),
        "content",
        ("افتح مصادر الشخص من خطوة المحتوى", "Open Content to manage person sources"),
    ),
    Chapter(
        "library",
        ("نظّم المكتبة وحدّد شكل المحتوى", "Organize the library and content format"),
        (
            "كل عنصر له شخص وشكل محتوى ومنصات متاحة؛ الجاهزية وحدها لا تضمن توافقه مع كل صفحة.",
            "Each item has a person, format and allowed platforms. Ready does not mean compatible with every page.",
        ),
        (
            (
                "اختر الشخص في «المكتبة»، واستخدم الحالة والبحث للوصول للعنصر. «محتوى غير مخصص» ليس مكتبة الشخص.",
                "Select a person in Library, then use search and status filters. Unassigned content is separate from a person's library.",
            ),
            (
                "افتح العنصر لمعاينته وتعديل العنوان والوصف والمنصات المتاحة وشكل المحتوى.",
                "Open an item to preview it and edit its title, caption, allowed platforms and format.",
            ),
            (
                "الفيديو يمكن تجهيزه كريل/فيديو قصير أو ستوري، والصورة كبوست أو ستوري، والنص كبوست على Facebook. الصور والنصوص غير مفعّلين لـ YouTube وTikTok هنا.",
                "Video can be a reel/short video or story; images can be posts or stories; text is a Facebook post. Images and text are not enabled for YouTube or TikTok here.",
            ),
            (
                "راجع أسباب عدم التوافق الظاهرة. ستوري Instagram يحتاج حساب Business معروف النوع، والستوريز في التجربة تحتاج محتوى رأسيًا بنسبة 9:16.",
                "Read any compatibility reasons. Instagram Stories need a known Business account type; pilot stories require vertical 9:16 media.",
            ),
            (
                "احفظ التعديل ثم حدّث المعاينة لترى تأثيره على المواعيد القادمة. لإبعاد عنصر عن منصة، ألغِ اختيارها في إعدادات العنصر ثم احفظ.",
                "Save changes and refresh the preview to see their effect on future slots. To exclude an item from a platform, uncheck that platform in the item's settings and save.",
            ),
            (
                "راجع حالات التكرار والفشل وتفاصيلها. استخدم إعادة المحاولة أو الأرشفة أو الحذف فقط عندما يظهر الإجراء لدورك وحالة العنصر، وبعد قراءة التأكيد. يمكن استعادة العنصر المؤرشف من تفاصيله؛ استبعاد موعد من التقويم بديل لحذف المحتوى كله.",
                "Inspect duplicate and failed states. Use Retry, Archive or Delete only when available for your role and the item's state, after reading the confirmation. Archived items can be restored from their details. Excluding a calendar slot is an alternative to deleting the whole item.",
            ),
        ),
        (
            "وصف الستوري ملاحظة في المكتبة، وليس كتابة فوق الصورة أو الفيديو. لا توجد إضافة تلقائية لموسيقى أو ملصقات أو روابط أو ألبومات متعددة العناصر. تعديل الشكل لا يمسح سجل استخدام العنصر.",
            "Story captions are library notes, not overlays. Automatic music, stickers, links and carousel albums are not included. Changing format does not erase an item's usage history.",
        ),
        "library",
        ("نظّم المكتبة", "Organize Library"),
    ),
    Chapter(
        "rotation",
        ("حدّد المواعيد وقواعد الروتيشن", "Set times and rotation"),
        (
            "الخطة توزّع المحتوى الجاهز والمتوافق على الصفحات المختارة، بتسلسل مستقل لكل صفحة.",
            "The plan assigns ready, compatible content to selected pages, with an independent sequence for each page.",
        ),
        (
            (
                "من الشخص افتح خطوة «المواعيد». حدّد أيام الأسبوع ووقت النشر اليومي؛ يمكنك إضافة مواعيد أخرى من الخيارات المتاحة.",
                "Open the person's Times step. Select weekdays and a daily time; add more times using the available options.",
            ),
            (
                "راجع المنطقة الزمنية وتاريخ البداية وترتيب المكتبة داخل الخيارات الإضافية. المواعيد تُعرض بتوقيت الخطة.",
                "Check the timezone, start date and library order in the extra options. Times use the plan's timezone.",
            ),
            (
                "اختر مرة واحدة لكل صفحة، أو الروتيشن المتكرر. في التكرار حدّد أقل فاصل قبل إعادة نفس العنصر على نفس الصفحة، من يوم إلى ٣٦٥ يومًا.",
                "Choose once per page or repeating rotation. For repeats, set a minimum interval of 1–365 days before the same item returns on the same page.",
            ),
            (
                "احفظ وافتح المعاينة. في الروتيشن تأتي العناصر غير المستخدمة أولًا، ثم الأقدم استخدامًا عندما ينتهي فاصلها.",
                "Save and open the preview. Rotation uses unseen items first, then the least recently used when its interval has elapsed.",
            ),
        ),
        (
            "اليوم في فاصل التكرار يعني ٢٤ ساعة كاملة. نقص المحتوى يترك مواعيد فارغة بدل كسر الفاصل. مثال توضيحي: ٣ عناصر ومرة يوميًا مع فاصل ٣٠ يومًا لن تملأ شهرًا كاملًا على نفس الصفحة.",
            "A repeat day means a full 24 hours. Too little content leaves gaps instead of shortening the interval. Example: three items, one daily slot and a 30-day repeat interval cannot fill a month on the same page.",
        ),
        "times",
        ("اضبط مواعيد الشخص", "Set this person's times"),
    ),
    Chapter(
        "preview",
        ("راجع معاينة الـ٣٠ يوم", "Review the 30-day preview"),
        (
            "المعاينة محاكاة قابلة للتغيير، وليست قائمة منشورات أُرسلت أو حُجزت للنشر.",
            "The preview is a changing simulation, not a list of sent or committed publications.",
        ),
        (
            (
                "افتح «معاينة ٣٠ يوم» للشخص بعد اختيار الصفحات وتجهيز المحتوى والمواعيد.",
                "Open the person's 30-day preview after selecting pages, preparing content and saving times.",
            ),
            (
                "راجع عدد المواعيد الممتلئة والفارغة، ثم افتح تفاصيل الموعد لترى الصفحة والعنصر المقترح وهل هو أول استخدام أم تكرار.",
                "Review filled and empty slot totals, then expand a slot to inspect the page, proposed item and first-use or repeat status.",
            ),
            (
                "لو الموعد فارغ، اقرأ السبب: محتوى غير جاهز، شكل غير متوافق، فاصل تكرار لم ينتهِ، أو حساب يحتاج إعادة ربط.",
                "For empty slots, read the reason: content not ready, incompatible format, an unelapsed repeat interval, or an account needing reconnection.",
            ),
            (
                "ارجع للخطوة المطلوبة وعدّلها، ثم حدّث المعاينة. استخدم الصفحات التالية لرؤية بقية المواعيد وافتح تقويم الشخص للصورة المجمّعة.",
                "Return to the relevant step, make changes and refresh. Page through the remaining slots and open the person's calendar for the combined view.",
            ),
        ),
        (
            "فتح المعاينة لا يستهلك المحتوى ولا ينشئ مهام نشر. المحتوى الجديد الجاهز والتعديلات قد يغيّران النتيجة. الخطة تظل متوقفة أثناء قفل النشر.",
            "Opening a preview does not consume content or create publishing jobs. Newly ready content and edits can change the result. The plan remains stopped while publishing is locked.",
        ),
        "preview",
        ("افتح معاينة الشخص", "Open this person's preview"),
    ),
    Chapter(
        "calendar",
        ("عدّل موعدًا أو استبدل محتواه أو استبعده", "Move, replace or exclude a calendar slot"),
        (
            "التقويم يجمع معاينات الخطط ويتيح تعديل عنصر بعينه بدل تغيير الخطة كلها.",
            "The calendar combines plan previews and lets you edit one slot without changing the whole plan.",
        ),
        (
            (
                "افتح «التقويم»، وحدّد الشخص والفترة التي تريد مراجعتها.",
                "Open Calendar and select the person and period to review.",
            ),
            (
                "افتح تعديل الموعد المطلوب، ثم غيّر التاريخ والوقت بتوقيت الخطة.",
                "Open the desired slot's editor and change its date and time in the plan's timezone.",
            ),
            (
                "لاستبدال المحتوى اختر عنصرًا متاحًا من مكتبة نفس الشخص، أو اترك الاختيار التلقائي، ثم احفظ.",
                "To replace content, choose an available item from the same person's library, or leave automatic selection, then save.",
            ),
            (
                "لاستبعاد هذا الموعد اضغط الاستبعاد. للرجوع عن تعديل محفوظ استخدم إعادة الضبط إن كانت ظاهرة.",
                "Exclude the slot to omit it. Use Reset when available to undo a saved override.",
            ),
            (
                "ارجع للتقويم وراجع النتيجة؛ العنصر البديل ما زال يحتاج استيفاء التوافق وفاصل التكرار.",
                "Return to Calendar and check the result. Replacement content must still satisfy compatibility and repeat rules.",
            ),
        ),
        (
            "استبعاد موعد لا يحذف المحتوى من المكتبة. التغييرات على الصفحات أو المواعيد أو الروتيشن توقف الخطة للمراجعة وقد تجعل تعديلات المواعيد القديمة غير مطابقة؛ راجع التقويم بعدها.",
            "Excluding a slot does not delete library content. Changing pages, times or rotation stops the plan for review and may invalidate old slot overrides; review Calendar afterward.",
        ),
        "calendar",
        ("افتح التقويم", "Open Calendar"),
    ),
    Chapter(
        "safety",
        ("افهم قفل النشر والفرق بين المعاينة والتنفيذ", "Understand the publishing lock and actual queue"),
        (
            "فترة التجربة الحالية تسمح بالربط والاستيراد والتجهيز والمعاينة فقط.",
            "The current testing period allows connections, imports, preparation and previews only.",
        ),
        (
            (
                "تأكد من ظهور شريط «فترة الاختبار: النشر ممنوع على كل الحسابات» أعلى صفحات مساحة العمل.",
                "Check the Testing period: publishing is disabled banner at the top of workspace pages.",
            ),
            (
                "استخدم المعاينة والتقويم لمراجعة الخطة. لا تحاول تفعيل خطة أو قاعدة ولا تستخدم نشر الآن.",
                "Use Preview and Calendar to inspect the plan. Do not activate a plan or rule, or use Publish now.",
            ),
            (
                "«قائمة الانتظار» داخل الأدوات المتقدمة تعرض الحجوزات الفعلية إن وُجدت، وهي مختلفة عن محاكاة الـ٣٠ يوم.",
                "The actual queue under Advanced tools shows real reservations if any exist. It is separate from the 30-day simulation.",
            ),
            (
                "لو ظهر سجل نتيجة غير مؤكدة، راجع الحالة مع المسؤول قبل إعادة المحاولة لتجنب نشر مكرر عندما يُسمح بالتشغيل مستقبلًا.",
                "If an outcome is uncertain, review it with the administrator before retrying, to prevent duplicates when operation is allowed in the future.",
            ),
        ),
        (
            "ممنوع أي نشر فعلي، بما فيه الخاص وغير المدرج ومنشورات الاختبار، وأي رفع مسودات للمنصات. تفعيل النشر مستقبلًا قرار منفصل للمسؤول بعد انتهاء الحظر، وليس خطوة مطلوبة لإكمال التجربة.",
            "No real publishing is allowed, including private, unlisted or test posts, and no platform draft uploads. Future activation is a separate administrator decision after the restriction ends, not a requirement for completing the trial.",
        ),
        "queue",
        ("راجع قائمة الانتظار فقط", "Inspect the actual queue only"),
    ),
    Chapter(
        "activity",
        ("تابع الحالة والتنبيهات وحل المشاكل", "Monitor status, alerts and common problems"),
        (
            "ابدأ بسبب المشكلة الظاهر في النظام، ثم عالج الخطوة المسؤولة عنها.",
            "Start with the reason shown in the app and fix the relevant step.",
        ),
        (
            (
                "راجع تنبيهات الرئيسية و«السجل» لمعرفة أخطاء الاستيراد أو الحسابات التي تحتاج إعادة ربط.",
                "Check Home alerts and Activity for import errors or accounts needing reconnection.",
            ),
            (
                "حساب غير ظاهر: تأكد من الحساب المستخدم في التفويض، صلاحيتك على الصفحة، وعدم تخصيصها لشخص آخر. قيود اعتماد التطبيق تحتاج المسؤول.",
                "Missing account: check the authorized login, your page access and whether another person owns the assignment. App approval restrictions need the administrator.",
            ),
            (
                "استيراد بلا نتائج: راجع الفولدر وصلاحية القراءة وحالة المصدر. مصادر المنصات قد لا تتيح تنزيل بعض الفيديوهات، وقد تكون العناصر مكررة بالفعل.",
                "Empty import: check the folder, read access and source state. Platforms may not expose some video files, or the items may already be duplicates.",
            ),
            (
                "معاينة فارغة: تأكد من حفظ الصفحات، وجود عناصر جاهزة لنفس الشخص، توافق شكلها، والمواعيد وفاصل التكرار.",
                "Empty preview: check saved pages, ready content for the same person, format compatibility, times and repeat interval.",
            ),
            (
                "رسالة بريد لم تصل: راجع البريد الصحيح ومجلد الرسائل غير المرغوب فيها، ثم اطلب من المسؤول مراجعة حالة الإرسال قبل تكرار الطلب.",
                "Missing email: check the address and spam folder, then ask the administrator to inspect delivery before repeating the request.",
            ),
        ),
        (
            "لو محتاج مساعدة ابعت للمسؤول اسم الشخص والوقت ونص الخطأ أو لقطة شاشة بدون بيانات حساسة. لا ترسل كلمة مرور أو توكن أو رابط دعوة أو استعادة.",
            "For help, share the person's name, time and error text or a screenshot without sensitive data. Never share passwords, tokens, invitations or recovery links.",
        ),
        "activity",
        ("افتح السجل", "Open Activity"),
    ),
    Chapter(
        "advanced",
        ("المصادر والقواعد والأدوات المتقدمة", "Sources, rules and advanced tools"),
        (
            "هذه أدوات متابعة وتفاصيل إضافية؛ لا تحتاجها لتجهيز أول شخص من المعالج.",
            "These tools offer additional details and monitoring. They are not needed to set up the first person in the wizard.",
        ),
        (
            (
                "افتح «أدوات متقدمة» في القائمة الجانبية، ثم «المصادر» لمراجعة حالة كل مصدر وآخر فحص وأخطائه.",
                "Expand Advanced tools, then Sources to review source status, last checks and errors.",
            ),
            (
                "المصدر التابع لشخص يقودك لإعداداته عند ذلك الشخص. بدء الاستيراد أو المتابعة وإيقافهما منفصل عن تشغيل خطة النشر.",
                "A person-owned source links back to that person's settings. Starting or pausing imports is separate from running a publishing plan.",
            ),
            (
                "«القواعد» فيها تفاصيل الوجهة مثل إعدادات YouTube أو TikTok والقوالب والفواصل. راجعها مع المسؤول عند الحاجة، مع إبقاء القواعد متوقفة خلال التجربة.",
                "Rules contains destination settings such as YouTube or TikTok options, templates and gaps. Review them with the administrator when needed, keeping rules stopped during testing.",
            ),
            (
                "استخدم مواعيد الشخص في المعالج لتخطيطه؛ لا تنشئ مسارًا موازيًا لنفس صفحاته من المحتوى غير المخصص.",
                "Use the person's wizard times for their plan. Do not create a parallel path for the same pages using unassigned content.",
            ),
        ),
        (
            "إعادة الربط لا تعني تشغيل قاعدة أو خطة. أي إعداد خاص بموافقة منصة لا يتجاوز قفل النشر العام.",
            "Reconnecting does not start a rule or plan. Platform-specific consent settings do not bypass the global publishing lock.",
        ),
        "sources",
        ("راجع المصادر", "Inspect Sources"),
    ),
    Chapter(
        "settings",
        ("إعدادات مساحة العمل", "Workspace settings"),
        (
            "الإعدادات العامة تخص مساحة العمل؛ كل خطة شخص تحتفظ أيضًا بمنطقتها الزمنية ومواعيدها.",
            "General settings apply to the workspace. Each person plan also has its own timezone and times.",
        ),
        (
            (
                "افتح «إعدادات مساحة العمل» من القائمة الجانبية لمراجعة المنطقة الزمنية وفاصل المنصات وأفق التخطيط.",
                "Open Workspace settings to review the timezone, cross-platform gap and planning horizon.",
            ),
            (
                "عدّل القيم المسموح بها لدورك ثم احفظ. لا تفترض أن تغيير توقيت مساحة العمل غيّر توقيت خطة محفوظة؛ راجع خطوة مواعيد الشخص.",
                "Edit values allowed by your role and save. Do not assume changing the workspace timezone changed a saved plan; check the person's Times step.",
            ),
            (
                "خيار إيقاف كل النشر يخص مساحة العمل. أبقه مفعّلًا خلال التجربة؛ القفل العام على السيرفر يظل مانعًا إضافيًا للنشر.",
                "Pause all publishing applies to the workspace. Keep it paused during testing; the server-wide lock is an additional publishing barrier.",
            ),
        ),
        (
            "إيقاف النشر لا يوقف الاستيراد تلقائيًا؛ أوقف المصدر نفسه إذا أردت وقف جلب محتوى جديد.",
            "Pausing publishing does not automatically pause imports. Pause the source itself to stop fetching new content.",
        ),
        "workspace",
        ("افتح إعدادات مساحة العمل", "Open Workspace settings"),
    ),
    Chapter(
        "members",
        ("الأعضاء والصلاحيات وأمان الحساب والإغلاق", "Members, permissions, account security and closure"),
        (
            "مالك مساحة العمل يدير وصول الفريق. دعوة عضو تختلف عن إضافة شخص صاحب محتوى.",
            "The workspace owner manages team access. Inviting a member is different from adding a content person.",
        ),
        (
            (
                "من إعدادات مساحة العمل افتح «الأعضاء» لمراجعة البريد والدور لكل عضو.",
                "Open Members in Workspace settings to review each member's email and role.",
            ),
            (
                "المالك يستطيع دعوة محرر بالبريد، ومراجعة الدعوات المعلقة وإلغاء غير المستخدم منها. المحرر يعمل على المحتوى، وإدارة الوصول للمالك.",
                "The owner can invite an editor by email, review pending invitations and revoke unused ones. Editors work on content; owners manage access.",
            ),
            (
                "لتعديل دور عضو اختر الدور المتاح وأكّد بكلمة مرورك. لا يمكن إسقاط آخر مالك نشط؛ عيّن مالكًا آخر أولًا عند الحاجة.",
                "To change a member's role, choose an available role and confirm your password. The last active owner cannot be removed; appoint another owner first when needed.",
            ),
            (
                "لو تشك في جلسات مفتوحة استخدم «إنهاء الجلسات الأخرى» وأكّد بكلمة مرورك؛ ستنتهي الجلسات الأخرى وتظل جلستك الحالية مسجّلة الدخول.",
                "If you suspect open sessions, choose End other sessions and confirm your password. Other sessions end while your current session remains signed in.",
            ),
            (
                "إغلاق مساحة العمل متاح للمالك في قسم الإغلاق: اقرأ أثره، واكتب اسم المساحة وكلمة مرورك للتأكيد. تواصل مع المسؤول قبل إغلاق مساحة تحتاج بياناتها.",
                "Workspace closure is available to the owner in the closure section. Read the consequences and confirm with the workspace name and password. Contact the administrator before closing a workspace whose data you need.",
            ),
        ),
        (
            "الإغلاق يوقف مساحة العمل والوصول إليها؛ لا تعتبره إثباتًا لحذف كل نسخة احتياطية. طلبات الخصوصية وحذف البيانات تُراجع وفق السياسة مع الدعم.",
            "Closure stops the workspace and its access; it is not proof that every backup was deleted. Privacy and data-deletion requests are handled under the policy with support.",
        ),
        "members",
        ("افتح الأعضاء والأمان", "Open Members and security"),
    ),
    Chapter(
        "billing",
        ("الباقة وحدود الاستخدام والفواتير", "Plan, usage limits and invoices"),
        (
            "صفحة الباقة توضّح الاستخدام والحدود الحالية، والدفع هنا بتحويل بنكي يراجعه المسؤول.",
            "Billing shows current usage and limits. Payments use bank transfer reviewed by an administrator.",
        ),
        (
            (
                "من إعدادات مساحة العمل افتح «الباقة والفواتير» وراجع حدود الأشخاص والحسابات والأعضاء والتخزين المتاحة في باقتك.",
                "Open Billing in Workspace settings and review your plan's people, account, member and storage limits.",
            ),
            (
                "لو وصلت لحد الاستخدام، نظّم المحتوى أو تواصل مع المسؤول بدل تكرار محاولة الإضافة.",
                "If you reach a limit, organize your content or contact the administrator rather than repeatedly retrying the addition.",
            ),
            (
                "عندما تكون الأسعار وبيانات البنك متاحة، يطلب المالك الباقة، وينفّذ التحويل خارج Repeater، ثم يسجّل مرجع التحويل في الفاتورة.",
                "When prices and bank details are configured, the owner requests a plan, makes the transfer outside Repeater and records its reference on the invoice.",
            ),
            (
                "تابع حالة الفاتورة حتى يراجع المسؤول التحويل. إرسال المرجع لا يثبت السداد ولا يغيّر الباقة وحده.",
                "Track the invoice while the administrator reviews the transfer. Submitting a reference does not confirm payment or change the plan by itself.",
            ),
        ),
        (
            "لو الأسعار أو بيانات البنك غير مضبوطة، سيظهر أن الدفع غير جاهز. لا ترسل بيانات بنكية أو تدفع لقيم غير معروضة ومعتمدة.",
            "If prices or bank details are missing, payment is shown as unavailable. Do not submit bank information or pay unconfigured, unapproved amounts.",
        ),
        "billing",
        ("راجع الباقة والفواتير", "Review Billing"),
    ),
    Chapter(
        "administration",
        ("للمسؤول: العملاء والدعوات والتحصيل", "Administrator: customers, invitations and payments"),
        (
            "هذا القسم خاص بمسؤول المنصة، ولا يظهر للعملاء العاديين.",
            "This section is for platform administrators and is hidden from ordinary customers.",
        ),
        (
            (
                "من «إدارة المنصة» افتح العملاء، وأنشئ مساحة مستقلة للعميل وبريد مالكها ولغتها. المساحة الجديدة تبدأ متوقفة.",
                "Under Platform administration, open Customers and create a separate customer workspace with its owner email and language. New workspaces start paused.",
            ),
            (
                "أصدر الدعوة وراجع حالة إرسالها. يمكن نسخ الرابط الخاص عند الحاجة؛ لا تضعه في سجل عام أو لقطة شاشة مشتركة.",
                "Issue an invitation and inspect delivery status. Copy the private link if needed; never put it in a public log or shared screenshot.",
            ),
            (
                "راجع طلبات استعادة الدخول التي تحتاج تدخلك من قائمة الاستعادة، وتحقق من الطلب قبل إصدار وصول جديد.",
                "Review recovery requests needing intervention in the recovery queue, and verify the request before issuing access.",
            ),
            (
                "في إدارة الباقات اضبط الأسعار والحدود المعتمدة فقط، وراجع مرجع التحويل وإثباته خارج النظام قبل اعتماد الفاتورة أو رفضها.",
                "In plan administration, configure only approved prices and limits. Verify transfer references and payment outside the app before approving or rejecting invoices.",
            ),
        ),
        (
            "إدارة العملاء أو اعتماد تحويل لا يرفعان قفل النشر. لا تبدأ أي خطة أو قاعدة أثناء التجربة.",
            "Customer administration and payment approval do not lift the publishing lock. Do not start plans or rules during testing.",
        ),
        "customers",
        ("افتح إدارة العملاء", "Open Customer administration"),
        True,
    ),
    Chapter(
        "operations",
        ("للمسؤول: البريد والتنبيهات والنسخ والاستعادة", "Administrator: mail, alerts, backups and recovery"),
        (
            "مراقبة التشغيل والاستعادة مسؤولية إدارية، وليست خطوة مطلوبة من العميل.",
            "Operational monitoring and recovery are administrator tasks, not customer setup steps.",
        ),
        (
            (
                "افتح «حالة التشغيل» لمراجعة صحة الخدمات وطابور البريد والمصادر وحالة النسخ الاحتياطي.",
                "Open Operations to inspect service health, mail delivery, sources and backup status.",
            ),
            (
                "عند فشل رسالة راجع حالة الإرسال والسبب. لا تختبر باستخدام بيانات عميل حقيقي؛ استخدم بريد الاختبار المعتمد.",
                "For failed mail, inspect delivery status and the reason. Use the approved test mailbox rather than real customer data.",
            ),
            (
                "راجع وصول تنبيهات العطل والتعافي من المراقبة الخارجية إلى بريد المسؤول؛ لا تعتبر غياب التنبيه دليلًا على سلامة النظام.",
                "Check that external outage and recovery alerts reach the administrator. No alert by itself does not prove the system is healthy.",
            ),
            (
                "راجع النسخ المحلية والخارجية. الاستعادة الكاملة تشمل قاعدة البيانات والوسائط ومفاتيح فك التشفير المحمية؛ نسخ قاعدة البيانات وحدها لا يكفي.",
                "Review local and off-site backups. Full recovery needs the database, media and protected decryption keys. A database dump alone is insufficient.",
            ),
            (
                "نفّذ تدريب الاستعادة واختبارات السعة في بيئة معزولة ببيانات تجريبية ومحاكاة، وتحقق من قفل النشر قبل وبعد أي تحديث أو استعادة.",
                "Run recovery drills and capacity tests in isolation with synthetic data and mocks. Verify the publishing lock before and after updates or restores.",
            ),
        ),
        (
            "الاستعادة الفعلية وإعداد البريد ومفاتيح الخدمات تتم بإجراءات التشغيل على السيرفر، وليس بزر استعادة تلقائي داخل دليل الاستخدام. لا تضع الأسرار أو كلمات المرور هنا.",
            "Real restores, mail setup and service credentials use server runbooks, not an automatic restore button in this guide. Never put secrets or passwords here.",
        ),
        "operations",
        ("افتح حالة التشغيل", "Open Operations"),
        True,
    ),
)


def chapters_for(lang: str, *, is_admin: bool) -> list[dict]:
    index = 0 if lang == "ar" else 1
    return [
        {
            "id": chapter.id,
            "title": chapter.title[index],
            "intro": chapter.intro[index],
            "steps": [step[index] for step in chapter.steps],
            "note": chapter.note[index],
            "destination": chapter.destination,
            "action": chapter.action[index],
        }
        for chapter in CHAPTERS
        if is_admin or not chapter.admin_only
    ]
