"""Mixed library and preview copy. All rich formats remain simulation-only."""

COPY = {
    "media.intro": (
        "Keep this person’s videos, images and posts together. Ready, compatible items join their rotation preview.",
        "اجمع فيديوهات الشخص وصوره وبوستاته في مكتبة واحدة. العناصر الجاهزة والمتوافقة بتدخل معاينة الروتيشن.",
    ),
    "media.file": ("Video or image file", "ملف الفيديو أو الصورة"),
    "media.original": (
        "Use an original file you have permission to reuse.",
        "استخدم ملفًا أصليًا عندك إذن بإعادة استخدامه.",
    ),
    "media.edit_help": (
        "Leave a field empty to use its original text. Format and platform choices affect the preview; saving never publishes content.",
        "سيب الحقل فاضي لاستخدام النص الأصلي. شكل المحتوى واختيار المنصات بيأثروا على المعاينة؛ الحفظ لا ينشر محتوى.",
    ),
    "media.platforms": ("Allow in these platform previews", "السماح في معاينة المنصات دي"),
    "media.delete_help": (
        "Only the workspace owner can delete this item and its files from Repeater. This does not remove any existing platform posts.",
        "مالك مساحة العمل فقط يقدر يحذف العنصر وملفاته من Repeater. ده لا يحذف أي منشورات موجودة على المنصات.",
    ),
    "media.empty_help": (
        "Add a video, image or text post above, or import files from the person’s plan.",
        "أضف فيديو أو صورة أو بوست نصي بالأعلى، أو استورد الملفات من خطة الشخص.",
    ),
    "media.add": ("Add content", "إضافة محتوى"),
    "media.add_help": (
        "Upload a video or a static JPEG, PNG or WebP image. Images: up to 20 MB / 24 megapixels. Choose one placement per item; change it later without resetting its rotation history.",
        "ارفع فيديو أو صورة ثابتة JPEG أو PNG أو WebP. الصور حتى ٢٠ ميجابايت و٢٤ ميجابكسل. اختار شكل عرض واحد لكل عنصر؛ تقدر تغيره لاحقًا من غير تصفير تاريخ الروتيشن.",
    ),
    "media.format": ("Content format", "شكل المحتوى"),
    "media.video_reel": ("Video · Reel / short video", "فيديو · ريل / فيديو قصير"),
    "media.video_story": ("Video · Story", "فيديو · ستوري"),
    "media.image_post": ("Image · Post", "صورة · بوست"),
    "media.image_story": ("Image · Story", "صورة · ستوري"),
    "media.text_post": ("Text · Post", "نص · بوست"),
    "media.post": ("Post", "بوست"),
    "media.reel": ("Reel / short video", "ريل / فيديو قصير"),
    "media.story": ("Story", "ستوري"),
    "media.compose": ("Write a text post", "كتابة بوست نصي"),
    "media.compose_help": (
        "Text-only posts enter Facebook Page previews. This saves to this person’s library only.",
        "البوستات النصية بتدخل معاينة صفحات Facebook. الحفظ هنا بيضيف لمكتبة الشخص فقط.",
    ),
    "media.save": ("Add to library", "إضافة للمكتبة"),
    "media.text_added": ("Added a text post to a person's library.", "تمت إضافة بوست نصي لمكتبة الشخص."),
    "media.person_required": (
        "Select a person in your workspace first.",
        "اختار شخصًا داخل مساحة العمل أولًا.",
    ),
    "media.invalid_format": (
        "Choose a supported content type and placement.",
        "اختار نوع محتوى وشكل عرض متوافقين.",
    ),
    "media.invalid_image": (
        "Use a static JPEG, PNG or WebP up to 20 MB and 24 megapixels.",
        "استخدم صورة ثابتة JPEG أو PNG أو WebP حتى ٢٠ ميجابايت و٢٤ ميجابكسل.",
    ),
    "media.empty_text": ("Write the post text before saving.", "اكتب نص البوست قبل الحفظ."),
    "media.compatibility": ("Where this content fits", "المحتوى مناسب لأي صفحات؟"),
    "media.eligible": ("Compatible format", "شكل متوافق"),
    "media.unsupported": (
        "This format is not available for this platform in the pilot.",
        "شكل المحتوى ده غير متاح للمنصة دي في التجربة.",
    ),
    "media.not_ready": ("Waiting for the file to finish processing.", "في انتظار انتهاء تجهيز الملف."),
    "media.permission": (
        "This account cannot manage content on the Page.",
        "الحساب ده لا يملك صلاحية إدارة محتوى الصفحة.",
    ),
    "media.business_required": (
        "Instagram Stories need a verified Business account type. Reconnect to check it; unknown and Creator types are excluded.",
        "ستوريز Instagram محتاجة نوع حساب Business مؤكد. أعد الربط للتحقق؛ حسابات Creator والنوع غير المعروف مستبعدين.",
    ),
    "media.story_vertical": (
        "Stories in this pilot need a 9:16 image or video. Prepare a vertical original; we do not crop it automatically.",
        "الستوري في التجربة محتاجة صورة أو فيديو رأسي ٩:١٦. جهّز الأصل بالمقاس ده؛ مفيش قص تلقائي.",
    ),
    "media.story_duration": (
        "Video stories in this pilot must be 3–60 seconds.",
        "ستوري الفيديو في التجربة لازم تكون من ٣ إلى ٦٠ ثانية.",
    ),
    "media.story_size": (
        "Video stories in this pilot must be at most 100 MB.",
        "ستوري الفيديو في التجربة بحد أقصى ١٠٠ ميجابايت.",
    ),
    "media.image_ratio": (
        "Instagram image posts need a ratio between 4:5 and 1.91:1. Use a prepared crop or choose Story for a vertical original.",
        "بوست صورة Instagram محتاج نسبة بين ٤:٥ و١٫٩١:١. جهّز المقاس أو اختار ستوري للأصل الرأسي.",
    ),
    "media.caption_length": (
        "Shorten the caption to 2,200 characters for Instagram.",
        "اختصر الوصف إلى ٢٢٠٠ حرف لـ Instagram.",
    ),
    "media.video_limits": (
        "The video does not meet this platform’s duration or file-size limits.",
        "الفيديو غير متوافق مع حدود المدة أو حجم الملف للمنصة.",
    ),
    "media.scope": (
        "Preview only: image posts and stories on Facebook / Instagram, text posts on Facebook, and existing short-video formats. Instagram Stories require Business accounts. YouTube image/text posts and TikTok photos are not enabled. No content is published.",
        "معاينة فقط: بوستات الصور والستوريز على Facebook وInstagram، البوستات النصية على Facebook، وأشكال الفيديو القصير الحالية. ستوريز Instagram تحتاج حساب Business. بوستات الصور والنصوص على YouTube وصور TikTok غير مفعلة. لا يتم نشر أي محتوى.",
    ),
    "media.story_caption": (
        "Story captions are library notes only; this preview does not add text overlays, links, music or stickers.",
        "وصف الستوري ملاحظة في المكتبة فقط؛ المعاينة مش بتضيف كتابة على الصورة أو روابط أو موسيقى أو ملصقات.",
    ),
    "media.excluded": ("Format exclusions ({n})", "استبعادات بسبب الشكل ({n})"),
    "media.excluded_help": (
        "Showing up to 100 item/page pairs. Compatible content still fills the same rotation queue, with the saved repeat gap.",
        "بنعرض حتى ١٠٠ حالة محتوى/صفحة. المحتوى المتوافق بيكمل في نفس طابور الروتيشن مع فاصل التكرار المحفوظ.",
    ),
    "media.drive_images": (
        "Include JPEG, PNG and WebP images from this folder",
        "ضم صور JPEG وPNG وWebP من الفولدر",
    ),
    "media.drive_help": (
        "Images enter as posts. Change an item to Story in its library details. Enabling images on an existing source imports matching unseen images on the next check; the first-check limit applies only before monitoring has started.",
        "الصور بتدخل كبوستات. غيّر العنصر لستوري من تفاصيله في المكتبة. تفعيل الصور لمصدر قائم بيستورد الصور غير المستوردة في الفحص التالي؛ حد أول فحص ينطبق فقط قبل بدء المتابعة.",
    ),
}
