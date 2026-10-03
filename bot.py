import os
import json
import re
import asyncio
import html
import hashlib
import urllib.request
import urllib.parse
import difflib
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

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


# ============================================================
# VEXA FOOTBALL BOT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHANNEL_USERNAME = "@fcnewsss"

AI_MODEL = "gpt-6-luna"

NEWS_INTERVAL = 600
FIRST_NEWS_DELAY = 30

DAILY_DIGEST_HOUR = 21
DAILY_DIGEST_MINUTE = 0

MAX_ARTICLES = 9
MAX_FEED_ITEMS_PER_SOURCE = 10

IMAGE_TIMEOUT = 12
MAX_IMAGE_SIZE = 12 * 1024 * 1024

STATE_FILE = Path("vexa_state.json")
IMAGE_CACHE_DIR = Path("vexa_images")

IMAGE_CACHE_DIR.mkdir(exist_ok=True)

LOCAL_TZ = ZoneInfo("Europe/Budapest")


# ============================================================
# RSS SOURCES
# ============================================================

RSS_FEEDS = [
    {
        "name": "BBC Sport",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    },
    {
        "name": "The Guardian",
        "url": "https://www.theguardian.com/football/rss",
    },
    {
        "name": "ESPN",
        "url": "https://www.espn.com/espn/rss/soccer/news",
    },
]


# ============================================================
# MEMORY
# ============================================================

sent_links = set()
sent_title_keys = set()
sent_content_keys = set()

recent_articles = []

client = (
    AsyncOpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_API_KEY
    else None
)


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "sent_links": [],
        "sent_title_keys": [],
        "sent_content_keys": [],
        "users": {},
        "last_digest_date": "",
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        base = default_state()
        base.update(data)

        return base

    except Exception as e:
        print("STATE LOAD ERROR:", e)
        return default_state()


state = load_state()

sent_links.update(
    state.get("sent_links", [])
)

sent_title_keys.update(
    state.get("sent_title_keys", [])
)

sent_content_keys.update(
    state.get("sent_content_keys", [])
)


def save_state():
    state["sent_links"] = list(sent_links)[-5000:]
    state["sent_title_keys"] = list(sent_title_keys)[-5000:]
    state["sent_content_keys"] = list(sent_content_keys)[-5000:]

    try:
        STATE_FILE.write_text(
            json.dumps(
                state,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    except Exception as e:
        print("STATE SAVE ERROR:", e)


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value):
    if not value:
        return ""

    value = html.unescape(str(value))

    value = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def limit_text(text, max_chars):
    text = clean_text(text)

    if len(text) <= max_chars:
        return text

    return (
        text[: max_chars - 1]
        .rstrip()
        + "…"
    )


def escape_html(text):
    return html.escape(
        str(text or ""),
        quote=False,
    )


def normalize_title(title):
    title = clean_text(title).lower()

    # حذف لینک و URL
    title = re.sub(
        r"https?://\S+",
        " ",
        title,
    )

    # یکسان‌سازی حروف فارسی
    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ۀ": "ه",
        "ة": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
    }

    for old, new in replacements.items():
        title = title.replace(old, new)

    # حذف اعداد و نشانه‌های اضافی
    title = re.sub(
        r"[^\wآ-ی]+",
        " ",
        title,
        flags=re.UNICODE,
    )

    # حذف فاصله اضافی
    title = re.sub(
        r"\s+",
        " ",
        title,
    ).strip()

    return title


def title_words(title):
    normalized = normalize_title(title)

    return {
        word
        for word in normalized.split()
        if len(word) >= 3
    }


def title_similarity(title1, title2):
    a = normalize_title(title1)
    b = normalize_title(title2)

    if not a or not b:
        return 0

    sequence_score = difflib.SequenceMatcher(
        None,
        a,
        b,
    ).ratio()

    words_a = title_words(a)
    words_b = title_words(b)

    if words_a and words_b:
        intersection = len(
            words_a.intersection(words_b)
        )

        union = len(
            words_a.union(words_b)
        )

        word_score = (
            intersection / union
            if union
            else 0
        )
    else:
        word_score = 0

    return max(
        sequence_score,
        word_score,
    )


def canonicalize_url(url):
    if not url:
        return ""

    try:
        parsed = urllib.parse.urlsplit(
            url.strip()
        )

        query = urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )

        ignored = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "utm_id",
            "fbclid",
            "gclid",
            "mc_cid",
            "mc_eid",
        }

        filtered = [
            (key, value)
            for key, value in query
            if key.lower() not in ignored
        ]

        new_query = urllib.parse.urlencode(
            filtered
        )

        clean = urllib.parse.urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                new_query,
                "",
            )
        )

        return clean

    except Exception:
        return url.strip()


def make_content_key(title):
    normalized = normalize_title(title)

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def make_absolute_url(base_url, url):
    if not url:
        return None

    return urllib.parse.urljoin(
        base_url,
        url,
    )


def safe_filename(url):
    digest = hashlib.sha1(
        url.encode("utf-8")
    ).hexdigest()

    return f"{digest}.img"


def now_local():
    return datetime.now(
        LOCAL_TZ
    )


# ============================================================
# ADVANCED DUPLICATE DETECTION
# ============================================================

def is_duplicate_article(
    link,
    title,
    source=None,
    existing_articles=None,
):
    canonical_link = canonicalize_url(
        link
    )

    title_key = normalize_title(
        title
    )

    content_key = make_content_key(
        title
    )

    # 1. Exact URL
    if canonical_link in sent_links:
        return True

    # 2. Exact normalized title
    if title_key in sent_title_keys:
        return True

    # 3. Exact content hash
    if content_key in sent_content_keys:
        return True

    # 4. Compare against recent in-memory news
    pool = (
        existing_articles
        if existing_articles is not None
        else recent_articles
    )

    for article in pool:

        old_link = canonicalize_url(
            article.get("link", "")
        )

        if (
            canonical_link
            and old_link
            and canonical_link == old_link
        ):
            return True

        old_title = (
            article.get("title_original")
            or article.get("title")
            or ""
        )

        similarity = title_similarity(
            title,
            old_title,
        )

        # Very similar titles are considered duplicates.
        if similarity >= 0.88:
            return True

        # For different sources, a slightly lower
        # similarity is enough if most words match.
        if (
            source
            and article.get("source")
            and source != article.get("source")
            and similarity >= 0.93
        ):
            return True

    return False


def mark_article_sent(article):
    link = canonicalize_url(
        article.get("link", "")
    )

    title = (
        article.get("title_original")
        or article.get("title")
        or ""
    )

    title_key = normalize_title(
        title
    )

    content_key = make_content_key(
        title
    )

    if link:
        sent_links.add(link)

    if title_key:
        sent_title_keys.add(
            title_key
        )

    if content_key:
        sent_content_keys.add(
            content_key
        )

    save_state()


# ============================================================
# IMAGE SYSTEM
# ============================================================

def add_image_candidate(
    candidates,
    url,
    score,
):
    if not url:
        return

    url = html.unescape(
        str(url)
    ).strip()

    url = url.replace(
        "&amp;",
        "&",
    )

    if url.startswith("//"):
        url = "https:" + url

    if not url.startswith(
        (
            "http://",
            "https://",
        )
    ):
        return

    candidates.append(
        (
            score,
            url,
        )
    )


def get_rss_image_candidates(entry):
    candidates = []

    media_content = (
        entry.get("media_content")
        or []
    )

    if isinstance(
        media_content,
        dict,
    ):
        media_content = [
            media_content
        ]

    for media in media_content:

        url = media.get("url")

        try:
            width = int(
                media.get("width")
                or 0
            )
        except Exception:
            width = 0

        score = 70

        if width >= 1600:
            score += 35
        elif width >= 1200:
            score += 25
        elif width >= 800:
            score += 15
        elif width >= 500:
            score += 5

        add_image_candidate(
            candidates,
            url,
            score,
        )

    thumbnails = (
        entry.get("media_thumbnail")
        or []
    )

    if isinstance(
        thumbnails,
        dict,
    ):
        thumbnails = [
            thumbnails
        ]

    for media in thumbnails:

        add_image_candidate(
            candidates,
            media.get("url"),
            35,
        )

    for link in (
        entry.get("links")
        or []
    ):

        href = link.get("href")
        mime = str(
            link.get("type", "")
        ).lower()

        if "image" in mime:

            add_image_candidate(
                candidates,
                href,
                65,
            )

    return candidates


def download_article_page(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; "
                "VexaFootballBot/1.0)"
            ),
            "Accept": (
                "text/html,"
                "application/xhtml+xml"
            ),
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=IMAGE_TIMEOUT,
    ) as response:

        return response.read(
            3_000_000
        ).decode(
            "utf-8",
            errors="ignore",
        )


def extract_meta_images(
    page_html,
    article_url,
):
    candidates = []

    patterns = [

        # OpenGraph
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',

        # Twitter
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',

        # Secure OG
        r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image:secure_url["\']',
    ]

    for index, pattern in enumerate(
        patterns
    ):

        try:
            matches = re.findall(
                pattern,
                page_html,
                flags=re.I,
            )
        except Exception:
            matches = []

        for match in matches:

            add_image_candidate(
                candidates,
                make_absolute_url(
                    article_url,
                    match,
                ),
                125 - index * 3,
            )

    # JSON-LD
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        flags=re.I | re.S,
    )

    for block in blocks:

        try:
            data = json.loads(
                html.unescape(
                    block.strip()
                )
            )

        except Exception:
            continue

        objects = (
            data
            if isinstance(data, list)
            else [data]
        )

        for obj in objects:

            if not isinstance(
                obj,
                dict,
            ):
                continue

            image = obj.get(
                "image"
            )

            if isinstance(
                image,
                str,
            ):

                add_image_candidate(
                    candidates,
                    make_absolute_url(
                        article_url,
                        image,
                    ),
                    110,
                )

            elif isinstance(
                image,
                list,
            ):

                for item in image:

                    if isinstance(
                        item,
                        str,
                    ):

                        add_image_candidate(
                            candidates,
                            make_absolute_url(
                                article_url,
                                item,
                            ),
                            105,
                        )

            elif isinstance(
                image,
                dict,
            ):

                add_image_candidate(
                    candidates,
                    make_absolute_url(
                        article_url,
                        image.get(
                            "url"
                        ),
                    ),
                    110,
                )

    return candidates


def choose_best_image(
    candidates
):
    if not candidates:
        return None

    seen = set()
    cleaned = []

    bad_words = {
        "logo",
        "avatar",
        "favicon",
        "sprite",
        "placeholder",
        "icon",
        "1x1",
        "pixel",
    }

    for score, url in sorted(
        candidates,
        reverse=True,
    ):

        if url in seen:
            continue

        seen.add(url)

        lowered = url.lower()

        penalty = 0

        for word in bad_words:

            if word in lowered:
                penalty += 70

        cleaned.append(
            (
                score - penalty,
                url,
            )
        )

    cleaned.sort(
        reverse=True
    )

    if not cleaned:
        return None

    return cleaned[0][1]


def get_article_image(entry):

    article_url = entry.get(
        "link"
    )

    # First: article page
    if article_url:

        try:

            page = (
                download_article_page(
                    article_url
                )
            )

            candidates = (
                extract_meta_images(
                    page,
                    article_url,
                )
            )

            best = choose_best_image(
                candidates
            )

            if best:
                return best

        except Exception as e:

            print(
                "ARTICLE IMAGE ERROR:",
                str(e)[:180],
            )

    # Second: RSS
    candidates = (
        get_rss_image_candidates(
            entry
        )
    )

    return choose_best_image(
        candidates
    )


def download_image_file(url):

    if not url:
        return None

    cache_path = (
        IMAGE_CACHE_DIR
        / safe_filename(url)
    )

    if (
        cache_path.exists()
        and cache_path.stat().st_size
        > 10_000
    ):
        return cache_path

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                "Mozilla/5.0 "
                "(compatible; "
                "VexaFootballBot/1.0)",
            "Accept":
                "image/avif,"
                "image/webp,"
                "image/jpeg,"
                "image/png,"
                "image/*;q=0.8",
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=IMAGE_TIMEOUT,
        ) as response:

            content_type = (
                response.headers.get(
                    "Content-Type"
                )
                or ""
            ).lower()

            data = response.read(
                MAX_IMAGE_SIZE + 1
            )

        if len(data) > MAX_IMAGE_SIZE:
            return None

        valid = (
            data.startswith(
                b"\xff\xd8\xff"
            )
            or data.startswith(
                b"\x89PNG\r\n\x1a\n"
            )
            or data.startswith(
                b"RIFF"
            )
            or data.startswith(
                b"GIF8"
            )
        )

        if (
            "image/" not in content_type
            and not valid
        ):
            return None

        if not valid:
            return None

        cache_path.write_bytes(
            data
        )

        return cache_path

    except Exception as e:

        print(
            "IMAGE DOWNLOAD ERROR:",
            str(e)[:180],
        )

        return None


# ============================================================
# RSS COLLECTION
# ============================================================

def collect_raw_articles():

    found = []
    seen_links = set()
    seen_titles = []

    for source in RSS_FEEDS:

        try:

            feed = feedparser.parse(
                source["url"]
            )

            for entry in feed.entries[
                :MAX_FEED_ITEMS_PER_SOURCE
            ]:

                title = clean_text(
                    entry.get(
                        "title"
                    )
                )

                link = entry.get(
                    "link"
                )

                if not title or not link:
                    continue

                canonical_link = (
                    canonicalize_url(
                        link
                    )
                )

                # Same URL inside this RSS cycle
                if (
                    canonical_link
                    in seen_links
                ):
                    continue

                # Similar title inside this RSS cycle
                duplicate = False

                for old_title in seen_titles:

                    if title_similarity(
                        title,
                        old_title,
                    ) >= 0.90:

                        duplicate = True
                        break

                if duplicate:
                    continue

                # Already sent previously
                if is_duplicate_article(
                    canonical_link,
                    title,
                    source["name"],
                ):
                    continue

                description = clean_text(
                    entry.get(
                        "summary"
                    )
                    or entry.get(
                        "description"
                    )
                    or ""
                )

                published = clean_text(
                    entry.get(
                        "published"
                    )
                    or entry.get(
                        "updated"
                    )
                    or ""
                )

                found.append(
                    {
                        "source":
                            source["name"],

                        "title_original":
                            title,

                        "summary_original":
                            limit_text(
                                description,
                                900,
                            ),

                        "link":
                            canonical_link,

                        "published":
                            published,

                        "image_url":
                            None,
                    }
                )

                seen_links.add(
                    canonical_link
                )

                seen_titles.append(
                    title
                )

        except Exception as e:

            print(
                f"RSS ERROR "
                f"[{source['name']}]:",
                e,
            )

    return found


async def collect_articles(
    max_articles=MAX_ARTICLES,
    fetch_images=True,
):

    raw = await asyncio.to_thread(
        collect_raw_articles
    )

    articles = []

    for entry in raw:

        if is_duplicate_article(
            entry["link"],
            entry["title_original"],
            entry["source"],
            existing_articles=(
                articles
            ),
        ):
            continue

        if fetch_images:

            entry["image_url"] = (
                await asyncio.to_thread(
                    get_article_image,
                    entry,
                )
            )

        articles.append(
            entry
        )

        if len(articles) >= max_articles:
            break

    return articles


# ============================================================
# AI EDITOR
# ============================================================

def ai_prompt(articles):

    payload = []

    for index, article in enumerate(
        articles
    ):

        payload.append(
            {
                "id": index,
                "source":
                    article["source"],
                "original_title":
                    article[
                        "title_original"
                    ],
                "original_summary":
                    article[
                        "summary_original"
                    ],
                "published":
                    article[
                        "published"
                    ],
            }
        )

    return f"""
تو سردبیر حرفه‌ای یک کانال خبری فوتبال فارسی هستی.

برای هر خبر:

1. عنوان را طبیعی، کوتاه و خبری بازنویسی کن.
2. خلاصه را در 2 تا 3 جمله فارسی بنویس.
3. هیچ واقعیت جدیدی اختراع نکن.
4. نام بازیکنان، مربیان، باشگاه‌ها و مسابقات را تغییر نده.
5. اگر اطلاعات ناقص است، حدس نزن.
6. لحن حرفه‌ای ولی زنده و خواندنی باشد.
7. از تیتر کلیک‌بیتی استفاده نکن.
8. نوع خبر را تشخیص بده.

category فقط یکی از:

breaking
transfer
match
player
coach
injury
record
tournament
other

importance فقط:

high
medium
low

style فقط:

classic
breaking
transfer
match
player
stats

transfer_status فقط:

official
rumor
denied
none

emoji مناسب فوتبال انتخاب کن.

اگر خبر انتقال است، رسمی بودن یا شایعه بودن را فقط
بر اساس اطلاعات خود خبر مشخص کن.

خروجی فقط JSON معتبر باشد.

ساختار:

{{
  "items": [
    {{
      "id": 0,
      "title": "...",
      "summary": "...",
      "category": "transfer",
      "importance": "high",
      "emoji": "🔄",
      "style": "transfer",
      "transfer_status": "official"
    }}
  ]
}}

اخبار:

{json.dumps(
    payload,
    ensure_ascii=False
)}
"""


async def translate_and_classify(
    articles
):

    if not articles:
        return []

    if client is None:

        print(
            "OPENAI ERROR: "
            "OPENAI_API_KEY is missing."
        )

        return []

    try:

        response = (
            await client.responses.create(
                model=AI_MODEL,
                input=ai_prompt(
                    articles
                ),
            )
        )

        raw = (
            response.output_text
            .strip()
        )

        raw = re.sub(
            r"^```json\s*",
            "",
            raw,
            flags=re.I,
        )

        raw = re.sub(
            r"\s*```$",
            "",
            raw,
        )

        data = json.loads(
            raw
        )

        items = data.get(
            "items",
            [],
        )

        result = []

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

        valid_transfer = {
            "official",
            "rumor",
            "denied",
            "none",
        }

        used_indexes = set()

        for item in items:

            try:
                idx = int(
                    item.get(
                        "id"
                    )
                )
            except Exception:
                continue

            if (
                idx < 0
                or idx >= len(articles)
            ):
                continue

            if idx in used_indexes:
                continue

            used_indexes.add(idx)

            article = dict(
                articles[idx]
            )

            article["title"] = (
                limit_text(
                    item.get(
                        "title"
                    ),
                    160,
                )
                or article[
                    "title_original"
                ]
            )

            article["summary"] = (
                limit_text(
                    item.get(
                        "summary"
                    ),
                    800,
                )
                or article[
                    "summary_original"
                ]
            )

            category = str(
                item.get(
                    "category",
                    "other",
                )
            ).lower()

            importance = str(
                item.get(
                    "importance",
                    "medium",
                )
            ).lower()

            style = str(
                item.get(
                    "style",
                    "classic",
                )
            ).lower()

            transfer_status = str(
                item.get(
                    "transfer_status",
                    "none",
                )
            ).lower()

            article["category"] = (
                category
                if category
                in valid_categories
                else "other"
            )

            article["importance"] = (
                importance
                if importance
                in valid_importance
                else "medium"
            )

            article["style"] = (
                style
                if style
                in valid_styles
                else "classic"
            )

            article["transfer_status"] = (
                transfer_status
                if transfer_status
                in valid_transfer
                else "none"
            )

            emoji = clean_text(
                item.get(
                    "emoji"
                )
            )

            article["emoji"] = (
                emoji[:4]
                if emoji
                else "⚽️"
            )

            result.append(
                article
            )

        # Fallback for missing AI results
        for index, article in enumerate(
            articles
        ):

            if index in used_indexes:
                continue

            fallback = dict(
                article
            )

            fallback["title"] = (
                article[
                    "title_original"
                ]
            )

            fallback["summary"] = (
                article[
                    "summary_original"
                ]
            )

            fallback["category"] = (
                "other"
            )

            fallback["importance"] = (
                "medium"
            )

            fallback["style"] = (
                "classic"
            )

            fallback[
                "transfer_status"
            ] = "none"

            fallback["emoji"] = "⚽️"

            result.append(
                fallback
            )

        return result

    except Exception as e:

        print(
            "===================================="
        )

        print(
            "OPENAI ERROR"
        )

        print(
            "ERROR TYPE:",
            type(e).__name__,
        )

        print(
            "ERROR:",
            e,
        )

        print(
            "===================================="
        )

        return []


# ============================================================
# NEWS FORMAT
# ============================================================

def build_news_text(article):

    title = escape_html(
        article.get("title")
    )

    summary = escape_html(
        article.get("summary")
    )

    source = escape_html(
        article.get("source")
    )

    emoji = escape_html(
        article.get(
            "emoji",
            "⚽️",
        )
    )

    category = article.get(
        "category",
        "other",
    )

    style = article.get(
        "style",
        "classic",
    )

    importance = article.get(
        "importance",
        "medium",
    )

    transfer_status = article.get(
        "transfer_status",
        "none",
    )

    if (
        category == "breaking"
        or importance == "high"
    ):

        header = (
            "🚨 <b>خبر مهم فوتبال</b>"
        )

    elif (
        category == "transfer"
        or style == "transfer"
    ):

        if transfer_status == "official":

            header = (
                "✅ <b>انتقال رسمی</b>"
            )

        elif transfer_status == "rumor":

            header = (
                "🟡 <b>"
                "نقل‌وانتقالات | شایعه"
                "</b>"
            )

        elif transfer_status == "denied":

            header = (
                "❌ <b>"
                "نقل‌وانتقالات | تکذیب"
                "</b>"
            )

        else:

            header = (
                "🔄 <b>"
                "نقل‌وانتقالات"
                "</b>"
            )

    elif (
        category == "match"
        or style == "match"
    ):

        header = (
            "🏟️ <b>گزارش مسابقه</b>"
        )

    elif (
        category == "record"
        or style == "stats"
    ):

        header = (
            "📊 <b>آمار و رکورد</b>"
        )

    elif (
        category == "player"
        or style == "player"
    ):

        header = (
            "👤 <b>"
            "دنیای بازیکنان"
            "</b>"
        )

    else:

        header = (
            f"{emoji} "
            "<b>خبر جدید فوتبال</b>"
        )

    return (
        f"{header}\n\n"
        f"📰 <b>{title}</b>\n\n"
        f"📝 {summary}\n\n"
        f"🏷 منبع: {source}"
    )


# ============================================================
# POST
# ============================================================

async def post_article(
    bot,
    article,
    target=CHANNEL_USERNAME,
):

    text = build_news_text(
        article
    )

    image_url = article.get(
        "image_url"
    )

    try:

        if image_url:

            image_path = (
                await asyncio.to_thread(
                    download_image_file,
                    image_url,
                )
            )

            if image_path:

                try:

                    with open(
                        image_path,
                        "rb",
                    ) as photo:

                        await bot.send_photo(
                            chat_id=target,
                            photo=photo,
                            caption=text,
                            parse_mode="HTML",
                            read_timeout=30,
                            write_timeout=30,
                        )

                    return True

                except Exception as e:

                    print(
                        "SEND PHOTO ERROR:",
                        e,
                    )

        await bot.send_message(
            chat_id=target,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        return True

    except Exception as e:

        print(
            "POST ARTICLE ERROR:",
            e,
        )

        return False


# ============================================================
# PIPELINE
# ============================================================

async def get_news_pipeline(
    max_articles=MAX_ARTICLES,
    fetch_images=True,
):

    articles = await collect_articles(
        max_articles=max_articles,
        fetch_images=fetch_images,
    )

    if not articles:
        return []

    translated = (
        await translate_and_classify(
            articles
        )
    )

    global recent_articles

    # ذخیره در کش جستجو
    recent_articles = (
        translated
        + recent_articles
    )

    unique = {}

    for article in recent_articles:

        key = canonicalize_url(
            article.get(
                "link",
                "",
            )
        )

        if not key:

            key = normalize_title(
                article.get(
                    "title",
                    "",
                )
            )

        unique[key] = article

    recent_articles = list(
        unique.values()
    )[:200]

    return translated


async def publish_news(
    articles,
    bot,
    target=CHANNEL_USERNAME,
):

    posted = 0

    for article in articles:

        # یک بررسی نهایی درست قبل از ارسال
        if is_duplicate_article(
            article.get(
                "link",
                "",
            ),
            article.get(
                "title_original",
                article.get(
                    "title",
                    "",
                ),
            ),
        ):
            print(
                "DUPLICATE BLOCKED:",
                article.get(
                    "title",
                    "",
                ),
            )

            continue

        ok = await post_article(
            bot,
            article,
            target,
        )

        if ok:

            mark_article_sent(
                article
            )

            posted += 1

        await asyncio.sleep(
            1.2
        )

    return posted


# ============================================================
# AUTOMATIC NEWS
# ============================================================

async def automatic_news_job(
    context
):

    try:

        print(
            "AUTO NEWS: checking..."
        )

        articles = (
            await get_news_pipeline(
                max_articles=MAX_ARTICLES,
                fetch_images=True,
            )
        )

        if not articles:

            print(
                "AUTO NEWS: "
                "no new articles."
            )

            return

        posted = await publish_news(
            articles,
            context.bot,
        )

        print(
            f"AUTO NEWS: "
            f"posted {posted} articles."
        )

    except Exception as e:

        print(
            "AUTO NEWS ERROR:",
            e,
        )


# ============================================================
# FILTERS
# ============================================================

def filter_articles(
    articles,
    mode,
):

    if mode == "important":

        return [
            a
            for a in articles
            if (
                a.get(
                    "importance"
                )
                == "high"
                or a.get(
                    "category"
                )
                == "breaking"
            )
        ]

    if mode == "transfers":

        return [
            a
            for a in articles
            if a.get(
                "category"
            )
            == "transfer"
        ]

    if mode == "matches":

        return [
            a
            for a in articles
            if a.get(
                "category"
            )
            == "match"
        ]

    if mode == "players":

        return [
            a
            for a in articles
            if a.get(
                "category"
            )
            in {
                "player",
                "coach",
                "injury",
            }
        ]

    if mode == "stats":

        return [
            a
            for a in articles
            if (
                a.get(
                    "category"
                )
                == "record"
                or a.get(
                    "style"
                )
                == "stats"
            )
        ]

    return articles


async def send_filtered_news(
    update,
    context,
    mode,
    label,
):

    await update.message.reply_text(
        f"⏳ دارم بخش «{label}» "
        "رو آماده می‌کنم... 🤖⚽️"
    )

    try:

        articles = (
            await get_news_pipeline(
                max_articles=12,
                fetch_images=True,
            )
        )

        filtered = filter_articles(
            articles,
            mode,
        )

        if not filtered:

            await update.message.reply_text(
                "فعلاً خبر تازه‌ای "
                "در این بخش پیدا نکردم. "
                "🔎⚽️"
            )

            return

        posted = 0

        for article in filtered[:6]:

            await post_article(
                context.bot,
                article,
                target=(
                    update.effective_chat.id
                ),
            )

            posted += 1

            await asyncio.sleep(
                0.8
            )

        await update.message.reply_text(
            f"✅ {posted} خبر آماده شد.",
            reply_markup=MAIN_KEYBOARD,
        )

    except Exception as e:

        print(
            "FILTER ERROR:",
            e,
        )

        await update.message.reply_text(
            "یه خطا موقع آماده‌سازی "
            "اخبار پیش اومد 😕"
        )


# ============================================================
# DAILY DIGEST
# ============================================================

def build_digest(
    articles
):

    if not articles:
        return None

    ordered = sorted(
        articles,
        key=lambda a:
            0
            if a.get(
                "importance"
            )
            == "high"
            else
            1
            if a.get(
                "importance"
            )
            == "medium"
            else 2,
    )[:7]

    lines = [
        "🌙 <b>جمع‌بندی فوتبال امروز</b>",
        "",
    ]

    for index, article in enumerate(
        ordered,
        1,
    ):

        emoji = escape_html(
            article.get(
                "emoji",
                "⚽️",
            )
        )

        title = escape_html(
            article.get(
                "title",
                "",
            )
        )

        lines.append(
            f"{index}. {emoji} "
            f"<b>{title}</b>"
        )

    lines.extend(
        [
            "",
            "🤖 جمع‌بندی توسط Vexa",
        ]
    )

    return "\n".join(
        lines
    )


async def daily_digest_job(
    context
):

    today = now_local().strftime(
        "%Y-%m-%d"
    )

    if (
        state.get(
            "last_digest_date"
        )
        == today
    ):
        return

    try:

        articles = (
            await get_news_pipeline(
                max_articles=15,
                fetch_images=False,
            )
        )

        if not articles:
            return

        digest = build_digest(
            articles
        )

        if digest:

            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=digest,
                parse_mode="HTML",
            )

            state[
                "last_digest_date"
            ] = today

            save_state()

    except Exception as e:

        print(
            "DIGEST ERROR:",
            e,
        )


# ============================================================
# SEARCH
# ============================================================

def search_cached_news(
    query
):

    query = clean_text(
        query
    ).lower()

    if not query:
        return []

    results = []

    for article in recent_articles:

        haystack = " ".join(
            [
                article.get(
                    "title",
                    "",
                ),
                article.get(
                    "summary",
                    "",
                ),
                article.get(
                    "title_original",
                    "",
                ),
            ]
        ).lower()

        if query in haystack:
            results.append(
                article
            )

    return results[:8]


async def search_news(
    update,
    context,
    query,
):

    if not query:

        await update.message.reply_text(
            "مثلاً:\n\n"
            "/search Real Madrid\n"
            "/search Mbappe\n"
            "/search نقل و انتقالات",
            reply_markup=MAIN_KEYBOARD,
        )

        return

    await update.message.reply_text(
        "🔎 دارم جستجو می‌کنم..."
    )

    results = search_cached_news(
        query
    )

    if not results:

        fresh = (
            await get_news_pipeline(
                max_articles=15,
                fetch_images=True,
            )
        )

        results = search_cached_news(
            query
        )

        if not results:

            query_lower = (
                query.lower()
            )

            results = [
                a
                for a in fresh
                if query_lower
                in (
                    a.get(
                        "title_original",
                        "",
                    ).lower()
                    + " "
                    + a.get(
                        "summary_original",
                        "",
                    ).lower()
                )
            ][:8]

    if not results:

        await update.message.reply_text(
            "چیزی با این عبارت پیدا نکردم. 😕"
        )

        return

    await update.message.reply_text(
        f"🔎 <b>نتایج جستجو برای:</b> "
        f"{escape_html(query)}",
        parse_mode="HTML",
    )

    for article in results[:6]:

        await post_article(
            context.bot,
            article,
            target=(
                update.effective_chat.id
            ),
        )

        await asyncio.sleep(
            0.7
        )


# ============================================================
# TRENDS
# ============================================================

def build_trends():

    if not recent_articles:

        return (
            "📈 هنوز داده کافی "
            "برای ترندها ندارم."
        )

    counter = {}

    stop_words = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "football",
        "news",
        "خبر",
        "فوتبال",
        "از",
        "به",
        "در",
        "و",
        "که",
        "برای",
        "با",
        "این",
        "آن",
        "یک",
    }

    for article in recent_articles[
        :100
    ]:

        text = " ".join(
            [
                article.get(
                    "title",
                    "",
                ),
                article.get(
                    "title_original",
                    "",
                ),
            ]
        )

        words = re.findall(
            r"[A-Za-z][A-Za-z0-9_-]{3,}|[آ-ی]{4,}",
            text,
        )

        for word in words:

            key = word.lower()

            if key in stop_words:
                continue

            counter[key] = (
                counter.get(
                    key,
                    0,
                )
                + 1
            )

    top = sorted(
        counter.items(),
        key=lambda x:
            x[1],
        reverse=True,
    )[:8]

    lines = [
        "📈 <b>"
        "موضوعات داغ فوتبال"
        "</b>",
        "",
    ]

    for index, (
        word,
        count,
    ) in enumerate(
        top,
        1,
    ):

        lines.append(
            f"{index}. 🔥 "
            f"{escape_html(word)} "
            f"— {count} خبر"
        )

    return "\n".join(
        lines
    )


# ============================================================
# USER PREFERENCES
# ============================================================

def user_key(update):

    if not update.effective_user:
        return None

    return str(
        update.effective_user.id
    )


def get_user_data(update):

    key = user_key(update)

    if key is None:
        return None

    users = state.setdefault(
        "users",
        {},
    )

    if key not in users:

        users[key] = {
            "subscriptions": [],
        }

    return users[key]


def user_subscriptions(
    update
):

    data = get_user_data(
        update
    )

    if not data:
        return []

    return data.get(
        "subscriptions",
        [],
    )


def save_subscription(
    update,
    item,
):

    data = get_user_data(
        update
    )

    if not data:
        return

    item = clean_text(
        item
    )

    if not item:
        return

    subs = data.setdefault(
        "subscriptions",
        [],
    )

    if item.lower() not in [
        x.lower()
        for x in subs
    ]:

        subs.append(
            item
        )

    data[
        "subscriptions"
    ] = subs[-10:]

    save_state()


def remove_subscription(
    update,
    item,
):

    data = get_user_data(
        update
    )

    if not data:
        return

    item_lower = item.lower()

    data[
        "subscriptions"
    ] = [
        x
        for x in data.get(
            "subscriptions",
            [],
        )
        if x.lower()
        != item_lower
    ]

    save_state()


# ============================================================
# KEYBOARD
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [
            "📰 آخرین اخبار",
            "🔥 اخبار مهم",
        ],
        [
            "🔄 نقل‌وانتقالات",
            "🏟️ مسابقات",
        ],
        [
            "👤 بازیکنان",
            "📊 آمار و رکورد",
        ],
        [
            "🏆 لیگ‌ها",
            "📅 جمع‌بندی امروز",
        ],
        [
            "🔎 جستجو",
            "🎯 علاقه‌مندی‌ها",
        ],
        [
            "📈 ترند فوتبال",
            "🤖 درباره Vexa",
        ],
        [
            "🆘 راهنما",
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


# ============================================================
# COMMANDS
# ============================================================

async def start(
    update,
    context,
):

    await update.message.reply_text(
        "🤖⚽️ <b>"
        "به Vexa خوش اومدی!"
        "</b>\n\n"
        "من یه دستیار خبری فوتبالی‌ام؛ "
        "اخبار، نقل‌وانتقالات، بازیکنان، "
        "آمار، جستجو و جمع‌بندی روزانه "
        "رو برات آماده می‌کنم.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_command(
    update,
    context,
):

    await update.message.reply_text(
        "🆘 <b>راهنمای Vexa</b>\n\n"

        "📰 /news — آخرین اخبار\n"
        "🔥 /important — اخبار مهم\n"
        "🔄 /transfers — نقل‌وانتقالات\n"
        "🏟️ /matches — مسابقات\n"
        "👤 /players — بازیکنان\n"
        "📊 /stats — آمار و رکورد\n"
        "🏆 /leagues — لیگ‌ها\n"
        "📅 /digest — جمع‌بندی\n"
        "🔎 /search نام — جستجو\n"
        "🎯 /subscribe تیم — علاقه‌مندی\n"
        "❌ /unsubscribe تیم — حذف\n"
        "❤️ /favorites — علاقه‌مندی‌ها\n"
        "📈 /trends — ترندها\n"
        "🤖 /about — درباره Vexa\n"
        "🧪 /testpost — تست ارسال",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def news_command(
    update,
    context,
):

    await update.message.reply_text(
        "⏳ دارم آخرین اخبار رو "
        "آماده می‌کنم... 🤖⚽️"
    )

    try:

        articles = (
            await get_news_pipeline(
                max_articles=MAX_ARTICLES,
                fetch_images=True,
            )
        )

        if not articles:

            await update.message.reply_text(
                "فعلاً خبر تازه‌ای پیدا نکردم. 🔎"
            )

            return

        for article in articles[:6]:

            await post_article(
                context.bot,
                article,
                target=(
                    update.effective_chat.id
                ),
            )

            await asyncio.sleep(
                0.8
            )

        await update.message.reply_text(
            "✅ تموم شد.",
            reply_markup=MAIN_KEYBOARD,
        )

    except Exception as e:

        print(
            "NEWS COMMAND ERROR:",
            e,
        )

        await update.message.reply_text(
            "یه خطا پیش اومد 😕"
        )


async def important_command(
    update,
    context,
):

    await send_filtered_news(
        update,
        context,
        "important",
        "اخبار مهم",
    )


async def transfers_command(
    update,
    context,
):

    await send_filtered_news(
        update,
        context,
        "transfers",
        "نقل‌وانتقالات",
    )


async def matches_command(
    update,
    context,
):

    await send_filtered_news(
        update,
        context,
        "matches",
        "مسابقات",
    )


async def players_command(
    update,
    context,
):

    await send_filtered_news(
        update,
        context,
        "players",
        "بازیکنان",
    )


async def stats_command(
    update,
    context,
):

    await send_filtered_news(
        update,
        context,
        "stats",
        "آمار و رکورد",
    )


async def leagues(
    update,
    context,
):

    await update.message.reply_text(
        "🏆 <b>بخش لیگ‌ها</b>\n\n"
        "اخبار لیگ‌ها فعال است.\n\n"
        "برای جدول زنده و نتایج دقیق، "
        "باید منبع داده ورزشی زنده "
        "به Vexa وصل شود.\n\n"
        "Vexa اطلاعات جدول را حدس نمی‌زند. ⚽️",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def digest_command(
    update,
    context,
):

    await update.message.reply_text(
        "⏳ دارم جمع‌بندی رو آماده می‌کنم..."
    )

    try:

        articles = (
            await get_news_pipeline(
                max_articles=15,
                fetch_images=False,
            )
        )

        digest = build_digest(
            articles
        )

        if not digest:

            await update.message.reply_text(
                "فعلاً داده کافی ندارم."
            )

            return

        await update.message.reply_text(
            digest,
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )

    except Exception as e:

        print(
            "DIGEST ERROR:",
            e,
        )

        await update.message.reply_text(
            "خطا در ساخت جمع‌بندی 😕"
        )


async def trends_command(
    update,
    context,
):

    await update.message.reply_text(
        build_trends(),
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def about_command(
    update,
    context,
):

    await update.message.reply_text(
        "🤖 <b>Vexa Football</b>\n\n"
        "⚽ اخبار فوتبال\n"
        "🚨 خبرهای مهم\n"
        "🔄 نقل‌وانتقالات\n"
        "📊 آمار و رکورد\n"
        "🔎 جستجو\n"
        "🎯 علاقه‌مندی‌ها\n"
        "📅 جمع‌بندی روزانه\n"
        "🖼️ تصاویر خبر\n"
        "♻️ سیستم ضدتکرار\n\n"
        "CREATE • COMPETE • CONQUER",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def search_command(
    update,
    context,
):

    query = " ".join(
        context.args
    ).strip()

    await search_news(
        update,
        context,
        query,
    )


async def subscribe_command(
    update,
    context,
):

    value = " ".join(
        context.args
    ).strip()

    if not value:

        await update.message.reply_text(
            "مثلاً:\n\n"
            "/subscribe Real Madrid\n"
            "/subscribe Mbappe"
        )

        return

    save_subscription(
        update,
        value,
    )

    await update.message.reply_text(
        f"🎯 «{escape_html(value)}» "
        "به علاقه‌مندی‌هات اضافه شد.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def unsubscribe_command(
    update,
    context,
):

    value = " ".join(
        context.args
    ).strip()

    if not value:

        await update.message.reply_text(
            "مثلاً:\n"
            "/unsubscribe Real Madrid"
        )

        return

    remove_subscription(
        update,
        value,
    )

    await update.message.reply_text(
        f"❌ «{escape_html(value)}» حذف شد.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def favorites_command(
    update,
    context,
):

    subs = user_subscriptions(
        update
    )

    if not subs:

        await update.message.reply_text(
            "🎯 هنوز چیزی به "
            "علاقه‌مندی‌هات اضافه نکردی.\n\n"
            "مثلاً:\n"
            "/subscribe Real Madrid"
        )

        return

    text = (
        "🎯 <b>"
        "علاقه‌مندی‌های تو"
        "</b>\n\n"
    )

    for item in subs:

        text += (
            f"• {escape_html(item)}\n"
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def testpost_command(
    update,
    context,
):

    await context.bot.send_message(
        chat_id=CHANNEL_USERNAME,
        text=(
            "🤖⚽️ <b>Vexa Test</b>\n\n"
            "سیستم ارسال کانال سالم است."
        ),
        parse_mode="HTML",
    )

    await update.message.reply_text(
        "✅ پست تست ارسال شد."
    )


# ============================================================
# MESSAGE ROUTER
# ============================================================

async def text_router(
    update,
    context,
):

    text = clean_text(
        update.message.text
    )

    mapping = {

        "📰 آخرین اخبار":
            news_command,

        "🔥 اخبار مهم":
            important_command,

        "🔄 نقل‌وانتقالات":
            transfers_command,

        "🏟️ مسابقات":
            matches_command,

        "👤 بازیکنان":
            players_command,

        "📊 آمار و رکورد":
            stats_command,

        "🏆 لیگ‌ها":
            leagues,

        "📅 جمع‌بندی امروز":
            digest_command,

        "🎯 علاقه‌مندی‌ها":
            favorites_command,

        "📈 ترند فوتبال":
            trends_command,

        "🤖 درباره Vexa":
            about_command,

        "🆘 راهنما":
            help_command,
    }

    if text in mapping:

        await mapping[text](
            update,
            context,
        )

        return

    if text == "🔎 جستجو":

        await update.message.reply_text(
            "🔎 برای جستجو:\n\n"
            "/search نام تیم یا بازیکن\n\n"
            "مثلاً:\n"
            "/search Real Madrid"
        )

        return

    # جستجوی طبیعی
    if (
        len(text) >= 3
        and not text.startswith("/")
    ):

        await search_news(
            update,
            context,
            text,
        )


# ============================================================
# COMMAND MENU
# ============================================================

async def post_init(
    application
):

    commands = [

        BotCommand(
            "start",
            "شروع",
        ),

        BotCommand(
            "news",
            "آخرین اخبار",
        ),

        BotCommand(
            "important",
            "اخبار مهم",
        ),

        BotCommand(
            "transfers",
            "نقل‌وانتقالات",
        ),

        BotCommand(
            "matches",
            "مسابقات",
        ),

        BotCommand(
            "players",
            "بازیکنان",
        ),

        BotCommand(
            "stats",
            "آمار و رکورد",
        ),

        BotCommand(
            "leagues",
            "لیگ‌ها",
        ),

        BotCommand(
            "digest",
            "جمع‌بندی",
        ),

        BotCommand(
            "search",
            "جستجو",
        ),

        BotCommand(
            "subscribe",
            "افزودن علاقه‌مندی",
        ),

        BotCommand(
            "unsubscribe",
            "حذف علاقه‌مندی",
        ),

        BotCommand(
            "favorites",
            "علاقه‌مندی‌ها",
        ),

        BotCommand(
            "trends",
            "ترندها",
        ),

        BotCommand(
            "about",
            "درباره Vexa",
        ),

        BotCommand(
            "help",
            "راهنما",
        ),

        BotCommand(
            "testpost",
            "تست ارسال",
        ),
    ]

    await application.bot.set_my_commands(
        commands
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing."
        )

    if not OPENAI_API_KEY:

        raise RuntimeError(
            "OPENAI_API_KEY is missing."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "news",
            news_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "important",
            important_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "transfers",
            transfers_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "matches",
            matches_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "players",
            players_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "leagues",
            leagues,
        )
    )

    application.add_handler(
        CommandHandler(
            "digest",
            digest_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "search",
            search_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "subscribe",
            subscribe_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "unsubscribe",
            unsubscribe_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "favorites",
            favorites_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "trends",
            trends_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "about",
            about_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "testpost",
            testpost_command,
        )
    )

    # Ordinary text

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_router,
        )
    )

    # ========================================================
    # JOBS
    # ========================================================

    if application.job_queue:

        application.job_queue.run_repeating(
            automatic_news_job,
            interval=NEWS_INTERVAL,
            first=FIRST_NEWS_DELAY,
            name="vexa_auto_news",
        )

        application.job_queue.run_daily(
            daily_digest_job,
            time=datetime(
                2000,
                1,
                1,
                DAILY_DIGEST_HOUR,
                DAILY_DIGEST_MINUTE,
                tzinfo=LOCAL_TZ,
            ).time(),
            name="vexa_daily_digest",
        )

    else:

        print(
            "WARNING: JobQueue unavailable."
        )

    print(
        "===================================="
    )

    print(
        "VEXA IS RUNNING"
    )

    print(
        "CHANNEL:",
        CHANNEL_USERNAME,
    )

    print(
        "AI MODEL:",
        AI_MODEL,
    )

    print(
        "NEWS INTERVAL:",
        NEWS_INTERVAL,
    )

    print(
        "DUPLICATE PROTECTION: ON"
    )

    print(
        "===================================="
    )

    application.run_polling()


if __name__ == "__main__":
    main()
