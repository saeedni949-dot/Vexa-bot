import os
import json
import re
import asyncio
import html
import urllib.request
import urllib.parse
import io
import feedparser

from openai import AsyncOpenAI

from telegram import (
    Update,
    BotCommand,
    ReplyKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHANNEL_USERNAME = "@fcnewsss"

AI_MODEL = "gpt-6-luna"

MAX_ARTICLES = 9

NEWS_INTERVAL = 600

IMAGE_TIMEOUT = 12

MAX_IMAGE_SIZE = 12 * 1024 * 1024


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set!")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set!")


client = AsyncOpenAI(
    api_key=OPENAI_API_KEY
)


# =========================================================
# RSS SOURCES
# =========================================================

RSS_FEEDS = {

    "BBC Sport":
        "https://feeds.bbci.co.uk/sport/football/rss.xml",

    "The Guardian":
        "https://www.theguardian.com/football/rss",

    "ESPN":
        "https://www.espn.com/espn/rss/soccer/news",

}


# =========================================================
# MEMORY
# =========================================================

sent_links = set()

sent_title_keys = set()


# =========================================================
# TEXT HELPERS
# =========================================================

def clean_text(text):

    if not text:
        return ""

    text = html.unescape(
        str(text)
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def limit_text(
    text,
    max_length
):

    if not text:
        return ""

    if len(text) <= max_length:
        return text

    return (
        text[:max_length]
        .rsplit(" ", 1)[0]
        + "..."
    )


def escape_html(text):

    if not text:
        return ""

    return html.escape(
        str(text),
        quote=False
    )


def normalize_title(title):

    title = clean_text(
        title
    ).lower()

    title = re.sub(
        r"[^\w\s]",
        " ",
        title
    )

    title = re.sub(
        r"\s+",
        " ",
        title
    )

    return title.strip()


# =========================================================
# URL HELPERS
# =========================================================

def make_absolute_url(
    image_url,
    article_url
):

    if not image_url:
        return None

    image_url = html.unescape(
        image_url
    ).strip()

    if image_url.startswith(
        "//"
    ):

        return "https:" + image_url


    if image_url.startswith(
        "/"
    ):

        try:

            return urllib.parse.urljoin(
                article_url,
                image_url
            )

        except Exception:

            return image_url


    if image_url.startswith(
        (
            "http://",
            "https://"
        )
    ):

        return image_url


    try:

        return urllib.parse.urljoin(
            article_url,
            image_url
        )

    except Exception:

        return image_url


# =========================================================
# IMAGE CANDIDATE SYSTEM
# =========================================================

def add_image_candidate(
    candidates,
    url,
    width=0,
    height=0,
    priority=0
):

    if not url:
        return

    try:

        width = int(
            width or 0
        )

    except Exception:

        width = 0


    try:

        height = int(
            height or 0
        )

    except Exception:

        height = 0


    score = (
        priority * 100000000
        +
        width * height
    )


    candidates.append({

        "url": url,

        "width": width,

        "height": height,

        "score": score,

    })


# =========================================================
# RSS IMAGE EXTRACTION
# =========================================================

def get_rss_image_candidates(
    entry
):

    candidates = []

    article_url = entry.get(
        "link",
        ""
    )


    try:

        # -------------------------------------------------
        # MEDIA CONTENT
        # -------------------------------------------------

        media_content = entry.get(
            "media_content",
            []
        )


        for media in media_content:

            if not isinstance(
                media,
                dict
            ):
                continue


            url = media.get(
                "url",
                ""
            )


            url = make_absolute_url(
                url,
                article_url
            )


            if not url:
                continue


            add_image_candidate(

                candidates,

                url,

                media.get(
                    "width",
                    0
                ),

                media.get(
                    "height",
                    0
                ),

                priority=10

            )


        # -------------------------------------------------
        # MEDIA THUMBNAIL
        # -------------------------------------------------

        thumbnails = entry.get(
            "media_thumbnail",
            []
        )


        for media in thumbnails:

            if not isinstance(
                media,
                dict
            ):
                continue


            url = media.get(
                "url",
                ""
            )


            url = make_absolute_url(
                url,
                article_url
            )


            if not url:
                continue


            add_image_candidate(

                candidates,

                url,

                media.get(
                    "width",
                    0
                ),

                media.get(
                    "height",
                    0
                ),

                priority=2

            )


        # -------------------------------------------------
        # ENCLOSURES
        # -------------------------------------------------

        enclosures = entry.get(
            "enclosures",
            []
        )


        for enclosure in enclosures:

            if not isinstance(
                enclosure,
                dict
            ):
                continue


            url = (

                enclosure.get(
                    "href",
                    ""
                )

                or

                enclosure.get(
                    "url",
                    ""
                )

            )


            url = make_absolute_url(
                url,
                article_url
            )


            if not url:
                continue


            media_type = enclosure.get(
                "type",
                ""
            ).lower()


            if (

                media_type.startswith(
                    "image/"
                )

                or

                re.search(
                    r"\.(jpg|jpeg|png|webp)(\?.*)?$",
                    url,
                    re.IGNORECASE
                )

            ):

                add_image_candidate(

                    candidates,

                    url,

                    0,

                    0,

                    priority=7

                )


    except Exception as e:

        print(
            "RSS IMAGE EXTRACTION ERROR:",
            repr(e)
        )


    return candidates


# =========================================================
# HTML IMAGE EXTRACTION
# =========================================================

def extract_meta_images(
    page_html,
    article_url
):

    candidates = []


    # -----------------------------------------------------
    # OG IMAGE
    # -----------------------------------------------------

    patterns = [

        (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            100
        ),

        (
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            100
        ),

        (
            r'<meta[^>]+property=["\']og:image:url["\'][^>]+content=["\']([^"\']+)["\']',
            100
        ),

        (
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image:url["\']',
            100
        ),

        (
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            90
        ),

        (
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
            90
        ),

    ]


    for pattern, priority in patterns:

        matches = re.findall(
            pattern,
            page_html,
            re.IGNORECASE
        )


        for image_url in matches:

            image_url = make_absolute_url(
                image_url,
                article_url
            )


            if image_url:

                add_image_candidate(

                    candidates,

                    image_url,

                    0,

                    0,

                    priority

                )


    # -----------------------------------------------------
    # OG IMAGE WIDTH / HEIGHT
    # -----------------------------------------------------

    width_match = re.search(

        r'<meta[^>]+property=["\']og:image:width["\'][^>]+content=["\'](\d+)["\']',

        page_html,

        re.IGNORECASE

    )


    height_match = re.search(

        r'<meta[^>]+property=["\']og:image:height["\'][^>]+content=["\'](\d+)["\']',

        page_html,

        re.IGNORECASE

    )


    if candidates:

        width = (
            int(width_match.group(1))
            if width_match
            else 0
        )

        height = (
            int(height_match.group(1))
            if height_match
            else 0
        )


        for candidate in candidates:

            candidate["width"] = width

            candidate["height"] = height

            candidate["score"] += (
                width * height
            )


    # -----------------------------------------------------
    # JSON-LD IMAGE
    # -----------------------------------------------------

    jsonld_blocks = re.findall(

        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',

        page_html,

        re.IGNORECASE | re.DOTALL

    )


    for block in jsonld_blocks:

        try:

            data = json.loads(
                block.strip()
            )


            jsonld_items = []


            if isinstance(
                data,
                dict
            ):

                jsonld_items.append(
                    data
                )


            elif isinstance(
                data,
                list
            ):

                jsonld_items.extend(
                    data
                )


            for item in jsonld_items:

                if not isinstance(
                    item,
                    dict
                ):
                    continue


                image = item.get(
                    "image"
                )


                if isinstance(
                    image,
                    str
                ):

                    image_url = make_absolute_url(
                        image,
                        article_url
                    )


                    if image_url:

                        add_image_candidate(

                            candidates,

                            image_url,

                            0,

                            0,

                            95

                        )


                elif isinstance(
                    image,
                    dict
                ):

                    image_url = (

                        image.get(
                            "url"
                        )

                        or

                        image.get(
                            "contentUrl"
                        )

                    )


                    image_url = make_absolute_url(
                        image_url,
                        article_url
                    )


                    if image_url:

                        add_image_candidate(

                            candidates,

                            image_url,

                            image.get(
                                "width",
                                0
                            ),

                            image.get(
                                "height",
                                0
                            ),

                            95

                        )


                elif isinstance(
                    image,
                    list
                ):

                    for image_item in image:

                        if isinstance(
                            image_item,
                            str
                        ):

                            image_url = make_absolute_url(
                                image_item,
                                article_url
                            )


                            if image_url:

                                add_image_candidate(

                                    candidates,

                                    image_url,

                                    0,

                                    0,

                                    95

                                )


        except Exception:
            continue


    return candidates


# =========================================================
# DOWNLOAD ARTICLE PAGE
# =========================================================

def download_article_page(
    article_url
):

    try:

        request = urllib.request.Request(

            article_url,

            headers={

                "User-Agent":
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36",

                "Accept":
                    "text/html,application/xhtml+xml",

            }

        )


        with urllib.request.urlopen(
            request,
            timeout=IMAGE_TIMEOUT
        ) as response:

            data = response.read(
                3 * 1024 * 1024
            )


            return data.decode(
                "utf-8",
                errors="ignore"
            )


    except Exception as e:

        print(
            "ARTICLE PAGE ERROR:",
            repr(e)
        )


        return None


# =========================================================
# GET BEST IMAGE URL
# =========================================================

def choose_best_image(
    candidates
):

    if not candidates:

        return None


    unique = {}


    for item in candidates:

        url = item["url"]


        if not url:

            continue


        if url not in unique:

            unique[url] = item

        else:

            if (
                item["score"]
                >
                unique[url]["score"]
            ):

                unique[url] = item


    candidates = list(
        unique.values()
    )


    candidates.sort(

        key=lambda item:
        item["score"],

        reverse=True

    )


    # Prefer reasonably large images
    for candidate in candidates:

        width = candidate.get(
            "width",
            0
        )

        height = candidate.get(
            "height",
            0
        )


        if (

            width >= 700
            and
            height >= 350

        ):

            return candidate["url"]


    return candidates[0]["url"]


# =========================================================
# FINAL IMAGE SELECTOR
# =========================================================

def get_article_image(
    entry
):

    article_url = entry.get(
        "link",
        ""
    )


    # -----------------------------------------------------
    # FIRST: ARTICLE PAGE
    # -----------------------------------------------------

    # This is intentionally before RSS thumbnails.
    # The article page usually contains the original
    # editorial image rather than a small thumbnail.

    if article_url:

        page_html = download_article_page(
            article_url
        )


        if page_html:

            page_candidates = extract_meta_images(

                page_html,

                article_url

            )


            page_image = choose_best_image(
                page_candidates
            )


            if page_image:

                print(
                    "HIGH QUALITY PAGE IMAGE:",
                    page_image
                )


                return page_image


    # -----------------------------------------------------
    # SECOND: RSS
    # -----------------------------------------------------

    rss_candidates = get_rss_image_candidates(
        entry
    )


    rss_image = choose_best_image(
        rss_candidates
    )


    if rss_image:

        print(
            "RSS IMAGE:",
            rss_image
        )


        return rss_image


    print(
        "NO IMAGE FOUND"
    )


    return None


# =========================================================
# DOWNLOAD IMAGE FILE
# =========================================================

def download_image_file(
    image_url
):

    if not image_url:

        return None


    try:

        request = urllib.request.Request(

            image_url,

            headers={

                "User-Agent":
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36",

                "Accept":
                    "image/avif,image/webp,image/apng,"
                    "image/svg+xml,image/*,*/*;q=0.8",

            }

        )


        with urllib.request.urlopen(

            request,

            timeout=IMAGE_TIMEOUT

        ) as response:


            content_type = response.headers.get(
                "Content-Type",
                ""
            ).lower()


            content_length = response.headers.get(
                "Content-Length"
            )


            if content_length:

                try:

                    if int(
                        content_length
                    ) > MAX_IMAGE_SIZE:

                        print(
                            "IMAGE TOO LARGE"
                        )

                        return None

                except Exception:

                    pass


            image_data = response.read(
                MAX_IMAGE_SIZE + 1
            )


            if len(image_data) > MAX_IMAGE_SIZE:

                print(
                    "IMAGE EXCEEDED SIZE LIMIT"
                )

                return None


            # -------------------------------------------------
            # Basic image validation
            # -------------------------------------------------

            if (

                "image/"
                not in content_type

            ):

                # Some servers don't send proper headers.
                # Check common image signatures.

                valid_signatures = (

                    image_data.startswith(
                        b"\xff\xd8\xff"
                    ),

                    image_data.startswith(
                        b"\x89PNG"
                    ),

                    image_data.startswith(
                        b"RIFF"
                    )
                    and
                    b"WEBP"
                    in image_data[:16],

                    image_data.startswith(
                        b"GIF8"
                    ),

                )


                if not any(
                    valid_signatures
                ):

                    print(
                        "DOWNLOADED FILE IS NOT AN IMAGE"
                    )

                    return None


            image_file = io.BytesIO(
                image_data
            )


            image_file.seek(0)


            image_file.name = (
                "vexa-news.jpg"
            )


            print(
                "IMAGE DOWNLOADED:",
                len(image_data),
                "bytes"
            )


            return image_file


    except Exception as e:

        print(
            "IMAGE DOWNLOAD ERROR:",
            repr(e)
        )


        return None


# =========================================================
# COLLECT NEWS
# =========================================================

def collect_articles(
    limit=MAX_ARTICLES
):

    articles = []


    for source_name, feed_url in RSS_FEEDS.items():

        try:

            feed = feedparser.parse(
                feed_url
            )


            print(
                f"Reading source: {source_name}"
            )


            for entry in feed.entries[:8]:

                title = clean_text(
                    entry.get(
                        "title",
                        ""
                    )
                )


                summary = clean_text(

                    entry.get(
                        "summary",
                        ""
                    )

                    or

                    entry.get(
                        "description",
                        ""
                    )

                )


                link = entry.get(
                    "link",
                    ""
                )


                if not title or not link:

                    continue


                title_key = normalize_title(
                    title
                )


                if (

                    link in sent_links

                    or

                    title_key in sent_title_keys

                ):

                    continue


                image_url = get_article_image(
                    entry
                )


                articles.append({

                    "source":
                        source_name,

                    "title":
                        title,

                    "summary":
                        summary,

                    "link":
                        link,

                    "image_url":
                        image_url,

                })


        except Exception as e:

            print(
                f"RSS ERROR ({source_name}): "
                f"{repr(e)}"
            )


    # =====================================================
    # REMOVE DUPLICATES
    # =====================================================

    unique_articles = []

    seen_links = set()

    seen_titles = set()


    for article in articles:

        link = article["link"]

        title_key = normalize_title(
            article["title"]
        )


        if link in seen_links:

            continue


        if title_key in seen_titles:

            continue


        seen_links.add(
            link
        )

        seen_titles.add(
            title_key
        )


        unique_articles.append(
            article
        )


    print(
        f"Collected {len(unique_articles)} "
        f"new unique articles."
    )


    return unique_articles[:limit]


# =========================================================
# AI NEWS EDITOR
# =========================================================

async def translate_news_with_ai(
    articles
):

    if not articles:

        return []


    news_text = ""


    for index, article in enumerate(
        articles,
        start=1
    ):

        news_text += f"""

===== NEWS {index} =====

SOURCE:
{article["source"]}

TITLE:
{limit_text(article["title"], 500)}

ARTICLE SUMMARY:
{limit_text(article["summary"], 3000)}

========================

"""


    prompt = f"""
تو سردبیر حرفه‌ای یک کانال تلگرامی اخبار فوتبال فارسی هستی.

خبرهای انگلیسی زیر را برای یک کانال فوتبال فارسی
به شکل طبیعی، دقیق، جذاب و حرفه‌ای بازنویسی کن.

این کار ترجمه کلمه‌به‌کلمه نیست.

مفهوم خبر را بفهم و سپس آن را مثل یک خبرنگار ورزشی
فارسی‌زبان بنویس.

قوانین:

1. هیچ اطلاعاتی که در متن اصلی نیست اضافه نکن.

2. حدس نزن.

3. اسم بازیکنان، مربیان، باشگاه‌ها، تیم‌های ملی
و مسابقات را حفظ کن.

4. عنوان حدود 8 تا 15 کلمه باشد.

5. خلاصه 2 تا 3 جمله باشد.

6. خلاصه باید مهم‌ترین بخش خبر را در همان ابتدا منتقل کند.

7. از کلیشه‌هایی مثل:
«در خبری مهم»
«اتفاقی باورنکردنی»
«هواداران شوکه شدند»
استفاده نکن.

8. لحن حرفه‌ای، ورزشی، طبیعی و بی‌طرف باشد.

9. خبرها نباید از نظر فرم و شروع جمله شبیه یکدیگر باشند.

10. نوع خبر را مشخص کن.

دسته‌بندی‌ها:

breaking
transfer
match
player
coach
injury
record
tournament
other

11. اهمیت:

high
medium
low

12. یک emoji مناسب انتخاب کن.

13. سبک انتشار:

classic
breaking
transfer
match
player
stats

14. همه خبرها را classic نکن.

15. برای خبرهای عادی از عبارت «خبر جدید فوتبال» بیش از حد استفاده نکن.

16. اگر خبر درباره نقل‌وانتقال است، اطلاعات قطعی و شایعه را با هم قاطی نکن.

17. اگر خبر درباره نتیجه یا مسابقه است، نتیجه را واضح بیان کن.

18. اگر خبر درباره مصدومیت است، فقط اطلاعات موجود در متن را بیان کن.

19. خروجی فقط JSON معتبر باشد.

20. تعداد آیتم‌ها باید دقیقاً برابر تعداد خبرهای ورودی باشد.

فرمت:

[
  {{
    "title": "عنوان فارسی",
    "summary": "خلاصه فارسی",
    "category": "transfer",
    "importance": "high",
    "emoji": "🔥",
    "style": "transfer"
  }}
]

خبرها:

{news_text}
"""


    try:

        print(
            f"Sending {len(articles)} articles "
            f"to OpenAI using model: {AI_MODEL}"
        )


        response = await client.responses.create(

            model=AI_MODEL,

            input=prompt,

        )


        result = (
            response.output_text
            .strip()
        )


        # -------------------------------------------------
        # CLEAN MARKDOWN
        # -------------------------------------------------

        result = re.sub(
            r"^```json\s*",
            "",
            result,
            flags=re.IGNORECASE
        )


        result = re.sub(
            r"^```\s*",
            "",
            result
        )


        result = re.sub(
            r"\s*```$",
            "",
            result
        )


        result = result.strip()


        data = json.loads(
            result
        )


        if not isinstance(
            data,
            list
        ):

            raise ValueError(
                "OpenAI response is not a list."
            )


        if len(data) != len(
            articles
        ):

            raise ValueError(
                "Wrong number of translated articles."
            )


        translated = []


        valid_categories = {

            "breaking",
            "transfer",
            "match",
            "player",
            "coach",
            "injury",
            "record",
            "tournament",
            "other",

        }


        valid_importance = {

            "high",
            "medium",
            "low",

        }


        valid_styles = {

            "classic",
            "breaking",
            "transfer",
            "match",
            "player",
            "stats",

        }


        for article, item in zip(
            articles,
            data
        ):

            if not isinstance(
                item,
                dict
            ):

                raise ValueError(
                    "Invalid AI article object."
                )


            title = clean_text(
                item.get(
                    "title",
                    ""
                )
            )


            summary = clean_text(
                item.get(
                    "summary",
                    ""
                )
            )


            category = clean_text(
                item.get(
                    "category",
                    "other"
                )
            ).lower()


            importance = clean_text(
                item.get(
                    "importance",
                    "medium"
                )
            ).lower()


            emoji = clean_text(
                item.get(
                    "emoji",
                    "⚽️"
                )
            )


            style = clean_text(
                item.get(
                    "style",
                    "classic"
                )
            ).lower()


            if category not in valid_categories:

                category = "other"


            if importance not in valid_importance:

                importance = "medium"


            if style not in valid_styles:

                style = "classic"


            if not title:

                title = article["title"]


            if not summary:

                summary = article["summary"]


            translated.append({

                "source":
                    article["source"],

                "title":
                    title,

                "summary":
                    summary,

                "link":
                    article["link"],

                "image_url":
                    article.get(
                        "image_url"
                    ),

                "category":
                    category,

                "importance":
                    importance,

                "emoji":
                    emoji,

                "style":
                    style,

            })


        print(
            f"Successfully processed "
            f"{len(translated)} articles."
        )


        return translated


    except Exception as e:

        print("")
        print(
            "===================================="
        )
        print(
            "OPENAI ERROR"
        )
        print(
            "ERROR TYPE:",
            type(e).__name__
        )
        print(
            "ERROR:",
            repr(e)
        )
        print(
            "===================================="
        )
        print("")


        return []


# =========================================================
# NEWS DESIGN
# =========================================================

def build_news_text(
    article
):

    title = escape_html(
        article["title"]
    )


    summary = escape_html(
        article["summary"]
    )


    source = escape_html(
        article["source"]
    )


    emoji = escape_html(
        article.get(
            "emoji",
            "⚽️"
        )
    )


    style = article.get(
        "style",
        "classic"
    )


    if style == "breaking":

        return (

            "🚨 <b>خبر فوری</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    if style == "transfer":

        return (

            "🔄 <b>نقل‌وانتقالات</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    if style == "match":

        return (

            "🏟️ <b>گزارش مسابقه</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    if style == "player":

        return (

            "👤 <b>دنیای فوتبال</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    if style == "stats":

        return (

            "📊 <b>آمار و رکورد</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    return (

        f"{emoji} <b>{title}</b>\n\n"

        f"📝 {summary}\n\n"

        f"🏷 <i>{source}</i>"

    )


# =========================================================
# TELEGRAM POST
# =========================================================

async def post_article(
    article,
    bot
):

    text = build_news_text(
        article
    )


    image_url = article.get(
        "image_url"
    )


    try:

        # -------------------------------------------------
        # DOWNLOAD ORIGINAL IMAGE
        # -------------------------------------------------

        if image_url:

            image_file = await asyncio.to_thread(

                download_image_file,

                image_url

            )


            if image_file:

                try:

                    await bot.send_photo(

                        chat_id=CHANNEL_USERNAME,

                        photo=image_file,

                        caption=text,

                        parse_mode="HTML",

                    )


                    print(
                        "POSTED WITH ORIGINAL IMAGE:",
                        article["title"]
                    )


                    return True


                except Exception as image_error:

                    print(
                        "PHOTO UPLOAD FAILED:",
                        repr(image_error)
                    )


        # -------------------------------------------------
        # TEXT FALLBACK
        # -------------------------------------------------

        await bot.send_message(

            chat_id=CHANNEL_USERNAME,

            text=text,

            parse_mode="HTML",

        )


        print(
            "POSTED AS TEXT:",
            article["title"]
        )


        return True


    except Exception as e:

        print(
            "TELEGRAM POST ERROR:",
            repr(e)
        )


        return False


# =========================================================
# KEYBOARD
# =========================================================

def get_main_keyboard():

    keyboard = [

        [
            "📰 آخرین اخبار",
            "🔥 اخبار مهم",
        ],

        [
            "🔄 نقل‌وانتقالات",
            "🏟️ مسابقات",
        ],

        [
            "👤 اخبار بازیکنان",
            "📊 آمار و رکورد",
        ],

        [
            "🤖 درباره Vexa",
            "🆘 راهنما",
        ],

    ]


    return ReplyKeyboardMarkup(

        keyboard,

        resize_keyboard=True

    )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "سلام 👋🔥\n\n"

        "من <b>Vexa</b> هستم 🤖⚽️\n\n"

        "دستیار اخبار فوتبال فارسی.\n\n"

        "از منوی پایین می‌تونی بخش موردنظرت رو انتخاب کنی "
        "یا از دستورات استفاده کنی.\n\n"

        "📰 آخرین اخبار\n"
        "🔥 اخبار مهم\n"
        "🔄 نقل‌وانتقالات\n"
        "🏟️ مسابقات\n"
        "👤 بازیکنان\n"
        "📊 آمار و رکوردها\n\n"

        "🚀 آماده‌ام!",

        parse_mode="HTML",

        reply_markup=get_main_keyboard()

    )


# =========================================================
# HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "🤖 <b>راهنمای Vexa</b>\n\n"

        "📰 /news\n"
        "آخرین اخبار فوتبال\n\n"

        "🔥 /important\n"
        "اخبار مهم\n\n"

        "🔄 /transfers\n"
        "نقل‌وانتقالات\n\n"

        "🏟️ /matches\n"
        "اخبار مسابقات\n\n"

        "👤 /players\n"
        "اخبار بازیکنان\n\n"

        "📊 /stats\n"
        "آمار و رکوردها\n\n"

        "🤖 /about\n"
        "درباره Vexa\n\n"

        "🧪 /testpost\n"
        "تست ارسال",

        parse_mode="HTML",

        reply_markup=get_main_keyboard()

    )


# =========================================================
# ABOUT
# =========================================================

async def about_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "🤖 <b>Vexa</b>\n\n"

        "Vexa یک دستیار خبری فوتبال است که اخبار "
        "منابع مختلف را جمع‌آوری می‌کند، بررسی و "
        "بازنویسی می‌کند و در کانال منتشر می‌کند. ⚽️\n\n"

        "📰 اخبار فوتبال\n"
        "🔄 نقل‌وانتقالات\n"
        "🏟️ مسابقات\n"
        "👤 بازیکنان\n"
        "📊 آمار و رکوردها\n"
        "🖼️ تصاویر خبر\n\n"

        "هدف Vexa اینه که خبرها سریع، خوانا و متنوع باشن. 🚀",

        parse_mode="HTML",

        reply_markup=get_main_keyboard()

    )


# =========================================================
# FILTERED NEWS
# =========================================================

async def send_filtered_news(
    update,
    context,
    filter_type=None
):

    await update.message.reply_text(

        "⏳ دارم خبرهای جدید رو بررسی می‌کنم... "
        "🤖⚽️"

    )


    articles = collect_articles(
        limit=MAX_ARTICLES
    )


    if not articles:

        await update.message.reply_text(

            "❌ فعلاً خبر جدیدی پیدا نکردم."

        )

        return


    translated = await translate_news_with_ai(
        articles
    )


    if not translated:

        await update.message.reply_text(

            "❌ فعلاً نتونستم اخبار رو آماده کنم."

        )

        return


    if filter_type == "important":

        filtered = [

            article

            for article in translated

            if article.get(
                "importance"
            ) == "high"

        ]


    elif filter_type == "transfers":

        filtered = [

            article

            for article in translated

            if article.get(
                "category"
            ) == "transfer"

        ]


    elif filter_type == "matches":

        filtered = [

            article

            for article in translated

            if article.get(
                "category"
            ) in {
                "match",
                "tournament"
            }

        ]


    elif filter_type == "players":

        filtered = [

            article

            for article in translated

            if article.get(
                "category"
            ) in {
                "player",
                "coach",
                "injury"
            }

        ]


    elif filter_type == "stats":

        filtered = [

            article

            for article in translated

            if article.get(
                "category"
            ) == "record"

        ]


    else:

        filtered = translated


    if not filtered:

        await update.message.reply_text(

            "ℹ️ در این بررسی خبر مناسبی برای این بخش "
            "پیدا نشد.\n\n"
            "چند دقیقه دیگه دوباره امتحان کن. ⚽️"

        )

        return


    posted = 0


    for article in filtered:

        success = await post_article(

            article,

            context.bot

        )


        if success:

            posted += 1

            sent_links.add(
                article["link"]
            )

            sent_title_keys.add(
                normalize_title(
                    article["title"]
                )
            )


        await asyncio.sleep(1)


    await update.message.reply_text(

        f"✅ {posted} خبر ارسال شد. ⚽️🔥"

    )


# =========================================================
# COMMANDS
# =========================================================

async def news_command(
    update,
    context
):

    await send_filtered_news(
        update,
        context,
        None
    )


async def important_command(
    update,
    context
):

    await send_filtered_news(
        update,
        context,
        "important"
    )


async def transfers_command(
    update,
    context
):

    await send_filtered_news(
        update,
        context,
        "transfers"
    )


async def matches_command(
    update,
    context
):

    await send_filtered_news(
        update,
        context,
        "matches"
    )


async def players_command(
    update,
    context
):

    await send_filtered_news(
        update,
        context,
        "players"
    )


async def stats_command(
    update,
    context
):

    await send_filtered_news(
        update,
        context,
        "stats"
    )


# =========================================================
# TEST POST
# =========================================================

async def testpost(
    update,
    context
):

    test_article = {

        "source":
            "Vexa",

        "title":
            "تست سیستم جدید انتشار Vexa",

        "summary":
            "این یک پیام آزمایشی برای بررسی اتصال بات "
            "به کانال و سیستم جدید انتشار است. 🤖⚽️",

        "link":
            "test",

        "image_url":
            None,

        "category":
            "other",

        "importance":
            "medium",

        "emoji":
            "🧪",

        "style":
            "classic",

    }


    success = await post_article(

        test_article,

        context.bot

    )


    if success:

        await update.message.reply_text(

            "✅ پیام تست با موفقیت ارسال شد."

        )

    else:

        await update.message.reply_text(

            "❌ ارسال پیام تست ناموفق بود."

        )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update,
    context
):

    text = update.message.text


    button_map = {

        "📰 آخرین اخبار":
            news_command,

        "🔥 اخبار مهم":
            important_command,

        "🔄 نقل‌وانتقالات":
            transfers_command,

        "🏟️ مسابقات":
            matches_command,

        "👤 اخبار بازیکنان":
            players_command,

        "📊 آمار و رکورد":
            stats_command,

        "🤖 درباره Vexa":
            about_command,

        "🆘 راهنما":
            help_command,

    }


    handler = button_map.get(
        text
    )


    if handler:

        await handler(
            update,
            context
        )


# =========================================================
# AUTOMATIC NEWS
# =========================================================

async def automatic_news(
    context
):

    print(
        "===================================="
    )

    print(
        "CHECKING FOR NEW FOOTBALL NEWS..."
    )


    articles = collect_articles(
        limit=MAX_ARTICLES
    )


    if not articles:

        print(
            "No new articles found."
        )

        return


    print(
        f"Found {len(articles)} new articles."
    )


    translated = await translate_news_with_ai(
        articles
    )


    if not translated:

        print(
            "News processing failed."
        )

        return


    for article in translated:

        success = await post_article(

            article,

            context.bot

        )


        if success:

            sent_links.add(
                article["link"]
            )

            sent_title_keys.add(
                normalize_title(
                    article["title"]
                )
            )


            print(
                "Posted:",
                article["title"]
            )


        await asyncio.sleep(1)


    print(
        "NEWS CHECK FINISHED."
    )

    print(
        "===================================="
    )


# =========================================================
# TELEGRAM COMMAND MENU
# =========================================================

async def post_init(
    application
):

    commands = [

        BotCommand(
            "start",
            "شروع Vexa"
        ),

        BotCommand(
            "news",
            "آخرین اخبار فوتبال"
        ),

        BotCommand(
            "important",
            "اخبار مهم"
        ),

        BotCommand(
            "transfers",
            "نقل‌وانتقالات"
        ),

        BotCommand(
            "matches",
            "اخبار مسابقات"
        ),

        BotCommand(
            "players",
            "اخبار بازیکنان"
        ),

        BotCommand(
            "stats",
            "آمار و رکوردها"
        ),

        BotCommand(
            "about",
            "درباره Vexa"
        ),

        BotCommand(
            "help",
            "راهنمای Vexa"
        ),

        BotCommand(
            "testpost",
            "تست ارسال"
        ),

    ]


    await application.bot.set_my_commands(
        commands
    )


# =========================================================
# MAIN
# =========================================================

def main():

    app = (

        Application.builder()

        .token(BOT_TOKEN)

        .post_init(post_init)

        .build()

    )


    # =====================================================
    # COMMAND HANDLERS
    # =====================================================

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    app.add_handler(
        CommandHandler(
            "news",
            news_command
        )
    )


    app.add_handler(
        CommandHandler(
            "important",
            important_command
        )
    )


    app.add_handler(
        CommandHandler(
            "transfers",
            transfers_command
        )
    )


    app.add_handler(
        CommandHandler(
            "matches",
            matches_command
        )
    )


    app.add_handler(
        CommandHandler(
            "players",
            players_command
        )
    )


    app.add_handler(
        CommandHandler(
            "stats",
            stats_command
        )
    )


    app.add_handler(
        CommandHandler(
            "about",
            about_command
        )
    )


    app.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )


    app.add_handler(
        CommandHandler(
            "testpost",
            testpost
        )
    )


    # =====================================================
    # BUTTON HANDLER
    # =====================================================

    app.add_handler(

        MessageHandler(

            filters.TEXT
            &
            ~filters.COMMAND,

            button_handler

        )

    )


    # =====================================================
    # AUTOMATIC NEWS
    # =====================================================

    app.job_queue.run_repeating(

        automatic_news,

        interval=NEWS_INTERVAL,

        first=30,

    )


    print(
        "===================================="
    )

    print(
        "VEXA BOT IS RUNNING..."
    )

    print(
        "===================================="
    )


    app.run_polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    main()
