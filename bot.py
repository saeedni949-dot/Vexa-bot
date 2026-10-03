import asyncio
import difflib
import hashlib
import html
import json
import os
import re
import urllib.parse
import urllib.request

from datetime import datetime, time
from io import BytesIO
from zoneinfo import ZoneInfo

import feedparser
from openai import AsyncOpenAI

from telegram import (
    InputFile,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHANNEL_USERNAME = "@fcnewsss"

AI_MODEL = "gpt-6-luna"

NEWS_INTERVAL = 600
FIRST_NEWS_DELAY = 30

DAILY_DIGEST_HOUR = 21
DAILY_DIGEST_MINUTE = 0

LOCAL_TZ = ZoneInfo("Europe/Budapest")

STATE_FILE = "vexa_state.json"

MAX_ARTICLES = 8
MAX_TRANSFER_ARTICLES = 6

MAX_RECENT_ARTICLES = 250
MAX_TRANSFER_HISTORY = 300

MAX_IMAGE_BYTES = 12 * 1024 * 1024
HTTP_TIMEOUT = 15


# =========================================================
# NORMAL FOOTBALL RSS
# =========================================================

RSS_FEEDS = [
    {
        "name": "BBC Sport",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "kind": "football",
    },
    {
        "name": "The Guardian",
        "url": "https://www.theguardian.com/football/rss",
        "kind": "football",
    },
    {
        "name": "ESPN",
        "url": "https://www.espn.com/espn/rss/soccer/news",
        "kind": "football",
    },
]


# =========================================================
# TRANSFER SOURCES
# =========================================================

TRANSFER_FEEDS = [
    {
        "name": "Sky Sports Transfers",
        "url": "https://www.skysports.com/rss/12040",
        "kind": "transfer",
    },
    {
        "name": "The Guardian Transfers",
        "url": "https://www.theguardian.com/football/transfer-window/rss",
        "kind": "transfer",
    },
    {
        "name": "The Guardian Rumour Mill",
        "url": "https://www.theguardian.com/football/series/rumourmill/rss",
        "kind": "transfer",
    },
    {
        "name": "ESPN Transfers",
        "url": "https://www.espn.com/espn/rss/soccer/news",
        "kind": "transfer",
    },
]


# =========================================================
# GLOBAL STATE
# =========================================================

sent_links = set()
sent_title_keys = set()
sent_content_keys = set()

transfer_event_keys = set()

recent_articles = []
recent_transfers = []

users = {}

last_digest_date = ""

openai_client = None


# =========================================================
# BASIC HELPERS
# =========================================================

def now_local():
    return datetime.now(LOCAL_TZ)


def safe_text(value):
    if value is None:
        return ""

    return str(value).strip()


def clean_html_text(value):
    if not value:
        return ""

    value = html.unescape(
        str(value)
    )

    value = re.sub(
        r"<br\s*/?>",
        "\n",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"</p\s*>",
        "\n",
        value,
        flags=re.IGNORECASE,
    )

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


def normalize_title(title):
    title = safe_text(
        title
    ).lower()

    title = re.sub(
        r"https?://\S+",
        "",
        title,
    )

    title = re.sub(
        r"[^\w\s]",
        " ",
        title,
        flags=re.UNICODE,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    )

    return title.strip()


def title_words(title):
    return {
        word
        for word in normalize_title(title).split()
        if len(word) >= 3
    }


def title_similarity(title1, title2):

    a = normalize_title(title1)
    b = normalize_title(title2)

    if not a or not b:
        return 0.0

    sequence_score = (
        difflib.SequenceMatcher(
            None,
            a,
            b,
        ).ratio()
    )

    words_a = title_words(a)
    words_b = title_words(b)

    if words_a and words_b:

        jaccard = (
            len(words_a & words_b)
            /
            len(words_a | words_b)
        )

    else:

        jaccard = 0.0

    return max(
        sequence_score,
        jaccard,
    )


def make_content_key(title):

    normalized = normalize_title(
        title
    )

    if not normalized:
        return ""

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


# =========================================================
# URL
# =========================================================

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
            (
                key,
                value,
            )
            for key, value in query
            if key.lower()
            not in ignored
        ]

        new_query = urllib.parse.urlencode(
            filtered
        )

        return urllib.parse.urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                new_query,
                "",
            )
        )

    except Exception:

        return url.strip()


# =========================================================
# STATE
# =========================================================

def load_state():

    global sent_links
    global sent_title_keys
    global sent_content_keys
    global transfer_event_keys
    global users
    global last_digest_date

    if not os.path.exists(
        STATE_FILE
    ):

        print(
            "STATE: no state file."
        )

        return

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        sent_links = set(
            data.get(
                "sent_links",
                [],
            )
        )

        sent_title_keys = set(
            data.get(
                "sent_title_keys",
                [],
            )
        )

        sent_content_keys = set(
            data.get(
                "sent_content_keys",
                [],
            )
        )

        transfer_event_keys = set(
            data.get(
                "transfer_event_keys",
                [],
            )
        )

        users = data.get(
            "users",
            {},
        )

        last_digest_date = data.get(
            "last_digest_date",
            "",
        )

        print(
            "STATE LOADED:",
            len(sent_links),
            "links |",
            len(sent_title_keys),
            "titles |",
            len(transfer_event_keys),
            "transfer events",
        )

    except Exception as e:

        print(
            "STATE LOAD ERROR:",
            type(e).__name__,
            e,
        )


def save_state():

    data = {
        "sent_links": list(
            sent_links
        )[-5000:],

        "sent_title_keys": list(
            sent_title_keys
        )[-5000:],

        "sent_content_keys": list(
            sent_content_keys
        )[-5000:],

        "transfer_event_keys": list(
            transfer_event_keys
        )[-3000:],

        "users": users,

        "last_digest_date": (
            last_digest_date
        ),
    }

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as e:

        print(
            "STATE SAVE ERROR:",
            type(e).__name__,
            e,
        )


# =========================================================
# NORMAL DUPLICATE SYSTEM
# =========================================================

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

    if (
        canonical_link
        and canonical_link in sent_links
    ):
        return True

    if (
        title_key
        and title_key in sent_title_keys
    ):
        return True

    if (
        content_key
        and content_key in sent_content_keys
    ):
        return True

    if existing_articles is not None:

        for article in existing_articles:

            old_link = canonicalize_url(
                article.get(
                    "link",
                    "",
                )
            )

            if (
                canonical_link
                and old_link
                and canonical_link == old_link
            ):
                return True

            old_title = (
                article.get(
                    "title_original",
                    "",
                )
                or
                article.get(
                    "title",
                    "",
                )
            )

            if (
                title_similarity(
                    title,
                    old_title,
                )
                >= 0.90
            ):
                return True

    return False


def mark_article_sent(article):

    link = canonicalize_url(
        article.get(
            "link",
            "",
        )
    )

    title = (
        article.get(
            "title_original",
            "",
        )
        or
        article.get(
            "title",
            "",
        )
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


# =========================================================
# TRANSFER EVENT KEY
# =========================================================

def normalize_entity(value):

    if not value:
        return ""

    value = safe_text(
        value
    ).lower()

    value = re.sub(
        r"[^\w\s]",
        " ",
        value,
        flags=re.UNICODE,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def make_transfer_event_key(
    article
):

    player = normalize_entity(
        article.get(
            "player",
            "",
        )
    )

    from_club = normalize_entity(
        article.get(
            "from_club",
            "",
        )
    )

    to_club = normalize_entity(
        article.get(
            "to_club",
            "",
        )
    )

    transfer_type = normalize_entity(
        article.get(
            "transfer_type",
            "",
        )
    )

    status = normalize_entity(
        article.get(
            "transfer_status",
            "",
        )
    )

    if not player:
        return ""

    raw = "|".join(
        [
            player,
            from_club,
            to_club,
            transfer_type,
            status,
        ]
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def is_duplicate_transfer(
    article,
    existing_articles=None,
):

    event_key = make_transfer_event_key(
        article
    )

    if (
        event_key
        and event_key in transfer_event_keys
    ):

        return True

    if existing_articles is not None:

        player = normalize_entity(
            article.get(
                "player",
                "",
            )
        )

        to_club = normalize_entity(
            article.get(
                "to_club",
                "",
            )
        )

        status = normalize_entity(
            article.get(
                "transfer_status",
                "",
            )
        )

        if player:

            for old in existing_articles:

                old_player = normalize_entity(
                    old.get(
                        "player",
                        "",
                    )
                )

                old_to_club = normalize_entity(
                    old.get(
                        "to_club",
                        "",
                    )
                )

                old_status = normalize_entity(
                    old.get(
                        "transfer_status",
                        "",
                    )
                )

                if (
                    player == old_player
                    and
                    to_club == old_to_club
                    and
                    status == old_status
                ):

                    if (
                        title_similarity(
                            article.get(
                                "title",
                                ""
                            )
                            or
                            article.get(
                                "title_original",
                                ""
                            ),
                            old.get(
                                "title",
                                ""
                            )
                            or
                            old.get(
                                "title_original",
                                ""
                            ),
                        )
                        >= 0.78
                    ):

                        return True

    return False


def mark_transfer_sent(
    article
):

    event_key = make_transfer_event_key(
        article
    )

    if event_key:

        transfer_event_keys.add(
            event_key
        )

    save_state()


# =========================================================
# HTTP
# =========================================================

def make_request(
    url,
    accept="*/*",
):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/128.0 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": (
            "en-US,en;q=0.9"
        ),
        "Referer": (
            "https://www.google.com/"
        ),
    }

    return urllib.request.Request(
        url,
        headers=headers,
    )


# =========================================================
# RSS IMAGE
# =========================================================

def extract_image_from_entry(
    entry
):

    try:

        media_content = entry.get(
            "media_content",
            [],
        )

        candidates = []

        for media in media_content:

            url = safe_text(
                media.get(
                    "url",
                    "",
                )
            )

            width = media.get(
                "width",
                0,
            )

            try:
                width = int(width)
            except Exception:
                width = 0

            if url:

                candidates.append(
                    (
                        width,
                        url,
                    )
                )

        if candidates:

            candidates.sort(
                key=lambda x: x[0],
                reverse=True,
            )

            return candidates[0][1]

        thumbnails = entry.get(
            "media_thumbnail",
            [],
        )

        candidates = []

        for media in thumbnails:

            url = safe_text(
                media.get(
                    "url",
                    "",
                )
            )

            width = media.get(
                "width",
                0,
            )

            try:
                width = int(width)
            except Exception:
                width = 0

            if url:

                candidates.append(
                    (
                        width,
                        url,
                    )
                )

        if candidates:

            candidates.sort(
                key=lambda x: x[0],
                reverse=True,
            )

            return candidates[0][1]

        for enclosure in entry.get(
            "enclosures",
            [],
        ):

            url = (
                safe_text(
                    enclosure.get(
                        "href",
                        "",
                    )
                )
                or
                safe_text(
                    enclosure.get(
                        "url",
                        "",
                    )
                )
            )

            media_type = safe_text(
                enclosure.get(
                    "type",
                    "",
                )
            ).lower()

            if (
                url
                and
                (
                    not media_type
                    or
                    media_type.startswith(
                        "image/"
                    )
                )
            ):

                return url

    except Exception as e:

        print(
            "RSS IMAGE ERROR:",
            type(e).__name__,
            e,
        )

    return None


# =========================================================
# HTML IMAGE EXTRACTION
# =========================================================

def normalize_image_url(
    image_url,
    base_url,
):

    if not image_url:
        return None

    image_url = (
        image_url
        .strip()
        .replace(
            "&amp;",
            "&",
        )
    )

    if image_url.startswith(
        "//"
    ):

        parsed = urllib.parse.urlsplit(
            base_url
        )

        image_url = (
            parsed.scheme
            + ":"
            + image_url
        )

    elif image_url.startswith(
        "/"
    ):

        image_url = urllib.parse.urljoin(
            base_url,
            image_url,
        )

    elif not image_url.startswith(
        (
            "http://",
            "https://",
        )
    ):

        image_url = urllib.parse.urljoin(
            base_url,
            image_url,
        )

    return image_url


def extract_meta_image(
    html_text,
    page_url,
):

    if not html_text:
        return None

    patterns = [

        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',

        r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)["\']',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image:secure_url["\']',

        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',

        r'<meta[^>]+name=["\']twitter:image:src["\'][^>]+content=["\']([^"\']+)["\']',

        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image:src["\']',
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            html_text,
            flags=re.IGNORECASE,
        )

        for value in matches:

            value = normalize_image_url(
                value,
                page_url,
            )

            if value:

                return value

    return None


def extract_srcset_images(
    html_text,
    page_url,
):

    if not html_text:
        return []

    results = []

    matches = re.findall(
        r'(?:srcset|data-srcset)=["\']([^"\']+)["\']',
        html_text,
        flags=re.IGNORECASE,
    )

    for srcset in matches:

        for item in srcset.split(","):

            item = item.strip()

            if not item:
                continue

            parts = item.split()

            url = parts[0]

            width = 0

            if len(parts) > 1:

                match = re.search(
                    r"(\d+)w",
                    parts[1],
                )

                if match:

                    try:
                        width = int(
                            match.group(1)
                        )
                    except Exception:
                        width = 0

            url = normalize_image_url(
                url,
                page_url,
            )

            if url:

                results.append(
                    (
                        width,
                        url,
                    )
                )

    results.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return [
        url
        for width, url
        in results
    ]


def extract_html_image_urls(
    html_text,
    page_url,
):

    if not html_text:
        return []

    candidates = []

    patterns = [

        r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']',

        r'<source[^>]+(?:src|data-src)=["\']([^"\']+)["\']',
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            html_text,
            flags=re.IGNORECASE,
        )

        for value in matches:

            value = normalize_image_url(
                value,
                page_url,
            )

            if value:

                candidates.append(
                    value
                )

    candidates.extend(
        extract_srcset_images(
            html_text,
            page_url,
        )
    )

    unique = []

    for item in candidates:

        if item not in unique:

            unique.append(
                item
            )

    return unique


def get_article_page_image_sync(
    article_url,
    rss_image=None,
):

    if not article_url:

        return rss_image

    try:

        request = make_request(
            article_url,
            accept=(
                "text/html,"
                "application/xhtml+xml,"
                "*/*;q=0.8"
            ),
        )

        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:

            raw = response.read(
                3 * 1024 * 1024
            )

            page_url = response.geturl()

        page = raw.decode(
            "utf-8",
            errors="ignore",
        )

        og_image = extract_meta_image(
            page,
            page_url,
        )

        if og_image:

            return og_image

        srcset_images = (
            extract_srcset_images(
                page,
                page_url,
            )
        )

        if srcset_images:

            return srcset_images[0]

        html_images = (
            extract_html_image_urls(
                page,
                page_url,
            )
        )

        if html_images:

            return html_images[0]

    except Exception as e:

        print(
            "ARTICLE IMAGE ERROR:",
            type(e).__name__,
            e,
        )

    return rss_image


async def get_best_image_url(
    article_url,
    rss_image,
):

    return await asyncio.to_thread(
        get_article_page_image_sync,
        article_url,
        rss_image,
    )


# =========================================================
# IMAGE DOWNLOAD
# =========================================================

def download_image_sync(
    image_url
):

    if not image_url:
        return None

    try:

        request = make_request(
            image_url,
            accept=(
                "image/avif,"
                "image/webp,"
                "image/apng,"
                "image/svg+xml,"
                "image/*,"
                "*/*;q=0.8"
            ),
        )

        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:

            content_type = safe_text(
                response.headers.get(
                    "Content-Type",
                    "",
                )
            ).lower()

            data = response.read(
                MAX_IMAGE_BYTES + 1
            )

            if len(data) > MAX_IMAGE_BYTES:

                return None

            if not data:

                return None

            if (
                content_type
                and
                not content_type.startswith(
                    "image/"
                )
            ):

                if (
                    b"<html"
                    in data[:5000].lower()
                    or
                    b"<!doctype"
                    in data[:5000].lower()
                ):

                    return None

            return data

    except Exception as e:

        print(
            "IMAGE DOWNLOAD ERROR:",
            type(e).__name__,
            e,
        )

        return None


async def download_image(
    image_url
):

    return await asyncio.to_thread(
        download_image_sync,
        image_url,
    )


# =========================================================
# RSS PARSER
# =========================================================

def parse_feed(
    feed_info
):

    try:

        feed = feedparser.parse(
            feed_info["url"]
        )

        articles = []

        for entry in feed.entries[:30]:

            title = safe_text(
                entry.get(
                    "title",
                    "",
                )
            )

            link = safe_text(
                entry.get(
                    "link",
                    "",
                )
            )

            summary = clean_html_text(
                entry.get(
                    "summary",
                    "",
                )
            )

            published = safe_text(
                entry.get(
                    "published",
                    "",
                )
            )

            rss_image = (
                extract_image_from_entry(
                    entry
                )
            )

            if (
                not title
                or
                not link
            ):

                continue

            articles.append(
                {
                    "source": feed_info[
                        "name"
                    ],

                    "source_kind": feed_info[
                        "kind"
                    ],

                    "title_original": title,

                    "link": link,

                    "summary_original": summary,

                    "published": published,

                    "rss_image": rss_image,

                    "image_url": None,
                }
            )

        return articles

    except Exception as e:

        print(
            "RSS ERROR:",
            feed_info["name"],
            type(e).__name__,
            e,
        )

        return []


# =========================================================
# COLLECT NORMAL NEWS
# =========================================================

async def collect_raw_articles():

    all_articles = []

    for feed_info in RSS_FEEDS:

        articles = await asyncio.to_thread(
            parse_feed,
            feed_info,
        )

        all_articles.extend(
            articles
        )

    unique = []

    for article in all_articles:

        if is_duplicate_article(
            article.get(
                "link",
                "",
            ),
            article.get(
                "title_original",
                "",
            ),
        ):

            continue

        if is_duplicate_article(
            article.get(
                "link",
                "",
            ),
            article.get(
                "title_original",
                "",
            ),
            existing_articles=unique,
        ):

            continue

        unique.append(
            article
        )

        if len(unique) >= MAX_ARTICLES:

            break

    return unique


# =========================================================
# COLLECT TRANSFER NEWS
# =========================================================

async def collect_transfer_articles():

    all_articles = []

    for feed_info in TRANSFER_FEEDS:

        articles = await asyncio.to_thread(
            parse_feed,
            feed_info,
        )

        all_articles.extend(
            articles
        )

    unique = []

    for article in all_articles:

        if is_duplicate_article(
            article.get(
                "link",
                "",
            ),
            article.get(
                "title_original",
                "",
            ),
        ):

            continue

        if is_duplicate_article(
            article.get(
                "link",
                "",
            ),
            article.get(
                "title_original",
                "",
            ),
            existing_articles=unique,
        ):

            continue

        unique.append(
            article
        )

        if (
            len(unique)
            >= MAX_TRANSFER_ARTICLES
        ):

            break

    return unique


# =========================================================
# OPENAI
# =========================================================

def get_openai_client():

    global openai_client

    if openai_client is None:

        if not OPENAI_API_KEY:

            print(
                "OPENAI ERROR: API key missing."
            )

            return None

        openai_client = AsyncOpenAI(
            api_key=OPENAI_API_KEY
        )

    return openai_client


# =========================================================
# NORMAL NEWS AI
# =========================================================

async def translate_and_classify(
    articles
):

    client = get_openai_client()

    if not client:

        return articles

    results = []

    for article in articles:

        original_title = article.get(
            "title_original",
            "",
        )

        original_summary = article.get(
            "summary_original",
            "",
        )

        prompt = f"""
You are the editor of a Persian football news Telegram channel.

Rewrite this football news in natural Persian.

Rules:
- Do not invent facts.
- Keep facts accurate.
- Do not turn rumors into confirmed facts.
- Make the headline short.
- Body should be 2 to 4 short paragraphs.
- Do not include source URLs.
- Do not add personal opinions.

Importance:

URGENT = genuinely breaking or major football news.

IMPORTANT = important football news deserving extra attention.

NORMAL = ordinary football news, interviews, routine training,
minor comments, minor squad updates, or interesting but non-major stories.

Be strict.

Most normal news must be NORMAL.

Return EXACTLY:

TITLE:
<short Persian title>

TEXT:
<2 to 4 short Persian paragraphs>

LEVEL:
<URGENT or IMPORTANT or NORMAL>

SOURCE:
{article.get("source", "")}

ORIGINAL TITLE:
{original_title}

ORIGINAL SUMMARY:
{original_summary}
"""

        try:

            response = await client.responses.create(
                model=AI_MODEL,
                input=prompt,
            )

            output = safe_text(
                getattr(
                    response,
                    "output_text",
                    "",
                )
            )

            title_match = re.search(
                r"TITLE:\s*(.*?)(?:\n|$)",
                output,
                flags=re.IGNORECASE,
            )

            text_match = re.search(
                r"TEXT:\s*(.*?)(?:\nLEVEL:|$)",
                output,
                flags=re.IGNORECASE | re.DOTALL,
            )

            level_match = re.search(
                r"LEVEL:\s*(URGENT|IMPORTANT|NORMAL)",
                output,
                flags=re.IGNORECASE,
            )

            article["title"] = (
                safe_text(
                    title_match.group(1)
                )
                if title_match
                else original_title
            )

            article["text"] = (
                safe_text(
                    text_match.group(1)
                )
                if text_match
                else (
                    original_summary
                    or
                    "اطلاعات بیشتری درباره این خبر منتشر شده است."
                )
            )

            level = "NORMAL"

            if level_match:

                level = (
                    level_match.group(1)
                    .upper()
                )

            if level not in {
                "URGENT",
                "IMPORTANT",
                "NORMAL",
            }:

                level = "NORMAL"

            article["level"] = level

            article["important"] = (
                level
                in {
                    "URGENT",
                    "IMPORTANT",
                }
            )

            article["news_type"] = "FOOTBALL"

            results.append(
                article
            )

        except Exception as e:

            print(
                "NORMAL AI ERROR:",
                type(e).__name__,
                e,
            )

            article["title"] = (
                original_title
            )

            article["text"] = (
                original_summary
                or
                "برای این خبر اطلاعات بیشتری منتشر شده است."
            )

            article["level"] = "NORMAL"
            article["important"] = False
            article["news_type"] = "FOOTBALL"

            results.append(
                article
            )

    return results


# =========================================================
# TRANSFER AI
# =========================================================

async def classify_transfers(
    articles
):

    client = get_openai_client()

    if not client:

        return articles

    results = []

    for article in articles:

        original_title = article.get(
            "title_original",
            "",
        )

        original_summary = article.get(
            "summary_original",
            "",
        )

        prompt = f"""
You are a highly careful football transfer editor.

Analyze this football transfer report.

Your most important rule:
NEVER upgrade a rumor into an official transfer.

Use the wording and evidence in the source.

TRANSFER STATUS:

OFFICIAL
Only if the source clearly reports an official announcement,
completed signing, completed loan, club confirmation,
player confirmation, or equivalent official confirmation.

AGREEMENT
Use when a deal/agreement is clearly reported but the transfer
is not yet officially completed.

NEGOTIATION
Use when clubs are negotiating, discussing, making an offer,
holding talks, or pursuing a player.

RUMOR
Use when it is only reported as a possibility, interest,
speculation, paper talk, or rumor.

DENIED
Use when the relevant club, player, agent, or reliable source
clearly denies the transfer claim.

TRANSFER TYPE:

PERMANENT
LOAN
FREE
EXTENSION
RETURN
UNKNOWN

Extract these if the source provides them:

PLAYER
FROM_CLUB
TO_CLUB
FEE
CONTRACT
TRANSFER_TYPE
TRANSFER_STATUS

Important:
- Do not invent a player name.
- Do not invent clubs.
- Do not invent a fee.
- Do not invent contract length.
- If something is unknown, write UNKNOWN.
- Preserve uncertainty.
- A newspaper rumor remains RUMOR.
- "Interested in" is NOT an agreement.
- "Bid submitted" is NOT an official transfer.
- "Agreement reached" is AGREEMENT, not OFFICIAL.
- "Here we go" or similar wording alone is not official unless
the source itself clearly establishes the transfer as completed.
- If the source says a club is monitoring a player, classify as RUMOR.
- If the source says talks are ongoing, classify as NEGOTIATION.

IMPORTANCE:

URGENT
Only a major breaking transfer development.

IMPORTANT
Major transfer development worth highlighting.

NORMAL
Routine transfer rumor or minor transfer development.

Write natural Persian suitable for Telegram.

Do not include the source URL.

Return EXACTLY:

TITLE:
<short Persian Persian headline>

TEXT:
<2 to 4 short Persian paragraphs>

PLAYER:
<name or UNKNOWN>

FROM_CLUB:
<club or UNKNOWN>

TO_CLUB:
<club or UNKNOWN>

FEE:
<fee or UNKNOWN>

CONTRACT:
<contract or UNKNOWN>

TRANSFER_TYPE:
<PERMANENT or LOAN or FREE or EXTENSION or RETURN or UNKNOWN>

TRANSFER_STATUS:
<OFFICIAL or AGREEMENT or NEGOTIATION or RUMOR or DENIED>

LEVEL:
<URGENT or IMPORTANT or NORMAL>

SOURCE:
{article.get("source", "")}

ORIGINAL TITLE:
{original_title}

ORIGINAL SUMMARY:
{original_summary}
"""

        try:

            response = await client.responses.create(
                model=AI_MODEL,
                input=prompt,
            )

            output = safe_text(
                getattr(
                    response,
                    "output_text",
                    "",
                )
            )

            def extract(
                label,
                default="UNKNOWN",
            ):

                match = re.search(
                    rf"{label}:\s*(.*?)(?:\n|$)",
                    output,
                    flags=re.IGNORECASE,
                )

                if not match:
                    return default

                value = safe_text(
                    match.group(1)
                )

                return (
                    value
                    if value
                    else default
                )

            title = extract(
                "TITLE",
                original_title,
            )

            text_match = re.search(
                r"TEXT:\s*(.*?)(?:\nPLAYER:|$)",
                output,
                flags=re.IGNORECASE | re.DOTALL,
            )

            text = (
                safe_text(
                    text_match.group(1)
                )
                if text_match
                else
                original_summary
            )

            player = extract(
                "PLAYER"
            )

            from_club = extract(
                "FROM_CLUB"
            )

            to_club = extract(
                "TO_CLUB"
            )

            fee = extract(
                "FEE"
            )

            contract = extract(
                "CONTRACT"
            )

            transfer_type = extract(
                "TRANSFER_TYPE"
            ).upper()

            transfer_status = extract(
                "TRANSFER_STATUS"
            ).upper()

            level = extract(
                "LEVEL",
                "NORMAL",
            ).upper()

            allowed_types = {
                "PERMANENT",
                "LOAN",
                "FREE",
                "EXTENSION",
                "RETURN",
                "UNKNOWN",
            }

            allowed_statuses = {
                "OFFICIAL",
                "AGREEMENT",
                "NEGOTIATION",
                "RUMOR",
                "DENIED",
            }

            allowed_levels = {
                "URGENT",
                "IMPORTANT",
                "NORMAL",
            }

            if transfer_type not in allowed_types:

                transfer_type = "UNKNOWN"

            if transfer_status not in allowed_statuses:

                transfer_status = "RUMOR"

            if level not in allowed_levels:

                level = "NORMAL"

            # -------------------------------------------------
            # SAFETY DOWNGRADE
            # -------------------------------------------------
            #
            # اگر اطلاعات اصلی ناقص باشد، اجازه نمی‌دهیم
            # خبر به‌اشتباه "رسمی" منتشر شود.
            #

            if (
                player == "UNKNOWN"
                and
                transfer_status == "OFFICIAL"
            ):

                transfer_status = "RUMOR"
                level = "NORMAL"

            article["title"] = title

            article["text"] = text

            article["player"] = player

            article["from_club"] = from_club

            article["to_club"] = to_club

            article["fee"] = fee

            article["contract"] = contract

            article["transfer_type"] = (
                transfer_type
            )

            article["transfer_status"] = (
                transfer_status
            )

            article["level"] = level

            article["important"] = (
                level
                in {
                    "URGENT",
                    "IMPORTANT",
                }
            )

            article["news_type"] = (
                "TRANSFER"
            )

            results.append(
                article
            )

            print(
                "TRANSFER AI:",
                title,
                "|",
                transfer_status,
                "|",
                level,
            )

        except Exception as e:

            print(
                "TRANSFER AI ERROR:",
                type(e).__name__,
                e,
            )

            article["title"] = (
                original_title
            )

            article["text"] = (
                original_summary
                or
                "گزارش نقل‌وانتقالاتی جدیدی منتشر شده است."
            )

            article["player"] = "UNKNOWN"
            article["from_club"] = "UNKNOWN"
            article["to_club"] = "UNKNOWN"
            article["fee"] = "UNKNOWN"
            article["contract"] = "UNKNOWN"
            article["transfer_type"] = "UNKNOWN"

            article["transfer_status"] = (
                "RUMOR"
            )

            article["level"] = "NORMAL"
            article["important"] = False
            article["news_type"] = "TRANSFER"

            results.append(
                article
            )

    return results


# =========================================================
# IMAGE PREPARATION
# =========================================================

async def prepare_article_images(
    articles
):

    for article in articles:

        article_url = safe_text(
            article.get(
                "link",
                "",
            )
        )

        rss_image = safe_text(
            article.get(
                "rss_image",
                "",
            )
        )

        best_image = await get_best_image_url(
            article_url,
            rss_image,
        )

        article["image_url"] = (
            best_image
        )

    return articles


# =========================================================
# NORMAL NEWS PIPELINE
# =========================================================

async def get_news_pipeline():

    articles = await collect_raw_articles()

    if not articles:
        return []

    articles = await translate_and_classify(
        articles
    )

    articles = await prepare_article_images(
        articles
    )

    global recent_articles

    recent_articles = (
        articles
        + recent_articles
    )[:MAX_RECENT_ARTICLES]

    return articles


# =========================================================
# TRANSFER PIPELINE
# =========================================================

async def get_transfer_pipeline():

    articles = await collect_transfer_articles()

    if not articles:
        return []

    articles = await classify_transfers(
        articles
    )

    # -----------------------------------------------------
    # TRANSFER DUPLICATE CHECK
    # -----------------------------------------------------

    filtered = []

    for article in articles:

        if is_duplicate_transfer(
            article,
            existing_articles=filtered,
        ):

            print(
                "TRANSFER DUPLICATE BLOCKED:",
                article.get(
                    "title",
                    "",
                ),
            )

            continue

        filtered.append(
            article
        )

    articles = await prepare_article_images(
        filtered
    )

    global recent_transfers

    recent_transfers = (
        articles
        + recent_transfers
    )[:MAX_TRANSFER_HISTORY]

    return articles


# =========================================================
# FULL PIPELINE
# =========================================================

async def get_full_news_pipeline():

    normal_news = await get_news_pipeline()

    transfer_news = await get_transfer_pipeline()

    combined = []

    combined.extend(
        transfer_news
    )

    combined.extend(
        normal_news
    )

    return combined


# =========================================================
# STATUS DISPLAY
# =========================================================

def transfer_status_label(
    status
):

    status = safe_text(
        status
    ).upper()

    labels = {

        "OFFICIAL":
            "✅ انتقال رسمی",

        "AGREEMENT":
            "📝 توافق",

        "NEGOTIATION":
            "🤝 مذاکرات",

        "RUMOR":
            "🟡 شایعه",

        "DENIED":
            "❌ تکذیب",
    }

    return labels.get(
        status,
        "🟡 نقل‌وانتقالات",
    )


def transfer_type_label(
    transfer_type
):

    transfer_type = safe_text(
        transfer_type
    ).upper()

    labels = {

        "PERMANENT":
            "انتقال دائمی",

        "LOAN":
            "قرضی",

        "FREE":
            "آزاد",

        "EXTENSION":
            "تمدید قرارداد",

        "RETURN":
            "بازگشت",

    }

    return labels.get(
        transfer_type,
        "",
    )


# =========================================================
# BUILD POST
# =========================================================

def build_post_text(
    article
):

    title = safe_text(
        article.get(
            "title",
            "",
        )
    )

    text = safe_text(
        article.get(
            "text",
            "",
        )
    )

    source = safe_text(
        article.get(
            "source",
            "",
        )
    )

    news_type = safe_text(
        article.get(
            "news_type",
            "FOOTBALL",
        )
    )

    level = article.get(
        "level",
        "NORMAL",
    )

    if news_type == "TRANSFER":

        status = article.get(
            "transfer_status",
            "RUMOR",
        )

        prefix = transfer_status_label(
            status
        )

        lines = [
            prefix,
            "",
            f"🔥 {title}",
            "",
            text,
        ]

        player = safe_text(
            article.get(
                "player",
                "",
            )
        )

        from_club = safe_text(
            article.get(
                "from_club",
                "",
            )
        )

        to_club = safe_text(
            article.get(
                "to_club",
                "",
            )
        )

        fee = safe_text(
            article.get(
                "fee",
                "",
            )
        )

        transfer_type = (
            transfer_type_label(
                article.get(
                    "transfer_type",
                    "",
                )
            )
        )

        if (
            player
            and player != "UNKNOWN"
        ):

            lines.extend(
                [
                    "",
                    f"👤 بازیکن: {player}",
                ]
            )

        if (
            from_club
            and from_club != "UNKNOWN"
        ):

            lines.append(
                f"🏠 مبدأ: {from_club}"
            )

        if (
            to_club
            and to_club != "UNKNOWN"
        ):

            lines.append(
                f"🏟 مقصد: {to_club}"
            )

        if (
            fee
            and fee != "UNKNOWN"
        ):

            lines.append(
                f"💰 مبلغ: {fee}"
            )

        if transfer_type:

            lines.append(
                f"🔄 نوع: {transfer_type}"
            )

        lines.extend(
            [
                "",
                f"📰 منبع: {source}",
            ]
        )

        return "\n".join(
            lines
        )

    # -----------------------------------------------------
    # NORMAL NEWS
    # -----------------------------------------------------

    if level == "URGENT":

        prefix = "🚨 خبر فوری"

    elif level == "IMPORTANT":

        prefix = "🔥 خبر مهم"

    else:

        prefix = "⚽ خبر فوتبال"

    return (
        f"{prefix}\n\n"
        f"🔥 {title}\n\n"
        f"{text}\n\n"
        f"📰 منبع: {source}"
    )


# =========================================================
# POST ARTICLE
# =========================================================

async def post_article(
    bot,
    article,
    target,
):

    try:

        text = build_post_text(
            article
        )

        image_url = safe_text(
            article.get(
                "image_url",
                "",
            )
        )

        if image_url:

            image_data = await download_image(
                image_url
            )

            if image_data:

                try:

                    photo = InputFile(
                        BytesIO(
                            image_data
                        ),
                        filename="vexa.jpg",
                    )

                    caption = text[:1024]

                    await bot.send_photo(
                        chat_id=target,
                        photo=photo,
                        caption=caption,
                    )

                    if len(text) > 1024:

                        await bot.send_message(
                            chat_id=target,
                            text=text[1024:],
                            disable_web_page_preview=True,
                        )

                    return True

                except Exception as e:

                    print(
                        "PHOTO SEND ERROR:",
                        type(e).__name__,
                        e,
                    )

        await bot.send_message(
            chat_id=target,
            text=text,
            disable_web_page_preview=True,
        )

        return True

    except Exception as e:

        print(
            "POST ERROR:",
            type(e).__name__,
            e,
        )

        return False


# =========================================================
# PUBLISH NEWS
# =========================================================

async def publish_news(
    articles,
    bot,
    target=CHANNEL_USERNAME,
):

    posted = 0

    for article in articles:

        link = canonicalize_url(
            article.get(
                "link",
                "",
            )
        )

        title = (
            article.get(
                "title_original",
                "",
            )
            or
            article.get(
                "title",
                "",
            )
        )

        title_key = normalize_title(
            title
        )

        content_key = make_content_key(
            title
        )

        if (
            (
                link
                and link in sent_links
            )
            or
            (
                title_key
                and title_key in sent_title_keys
            )
            or
            (
                content_key
                and content_key in sent_content_keys
            )
        ):

            print(
                "DUPLICATE BLOCKED:",
                title,
            )

            continue

        success = await post_article(
            bot,
            article,
            target,
        )

        if success:

            mark_article_sent(
                article
            )

            if (
                article.get(
                    "news_type"
                )
                == "TRANSFER"
            ):

                mark_transfer_sent(
                    article
                )

            posted += 1

        await asyncio.sleep(
            1.2
        )

    return posted


# =========================================================
# KEYBOARD
# =========================================================

def main_keyboard():

    keyboard = [
        [
            "📰 اخبار جدید",
            "🔥 نقل‌وانتقالات",
        ],
        [
            "🚨 اخبار مهم",
            "🔄 بروزرسانی",
        ],
        [
            "ℹ️ راهنما",
            "❌ بستن منو",
        ],
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
    )


# =========================================================
# START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if user:

        users[str(user.id)] = {
            "id": user.id,
            "username": user.username,
            "first_name": user.first_name,
        }

        save_state()

    await update.message.reply_text(
        "سلام داداش 👋🔥\n\n"
        "من Vexa هستم؛ ربات اخبار فوتبال و نقل‌وانتقالات.\n\n"
        "از منوی پایین می‌تونی اخبار جدید، "
        "اخبار مهم و نقل‌وانتقالات رو ببینی.",
        reply_markup=main_keyboard(),
    )


# =========================================================
# HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "ℹ️ راهنمای Vexa\n\n"
        "/start — شروع ربات\n"
        "/news — اخبار جدید\n"
        "/important — اخبار مهم\n"
        "/transfers — نقل‌وانتقالات\n"
        "/testpost — تست ارسال\n"
        "/help — راهنما",
        reply_markup=main_keyboard(),
    )


# =========================================================
# CLOSE MENU
# =========================================================

async def close_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "منو بسته شد 👌",
        reply_markup=ReplyKeyboardRemove(),
    )


# =========================================================
# NEWS COMMAND
# =========================================================

async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "⏳ دارم اخبار جدید رو بررسی می‌کنم..."
    )

    try:

        articles = await get_news_pipeline()

        if not articles:

            await update.message.reply_text(
                "فعلاً خبر جدیدی پیدا نکردم 😅",
                reply_markup=main_keyboard(),
            )

            return

        for article in articles:

            await post_article(
                context.bot,
                article,
                update.effective_chat.id,
            )

            await asyncio.sleep(
                0.8
            )

    except Exception as e:

        print(
            "NEWS ERROR:",
            type(e).__name__,
            e,
        )

        await update.message.reply_text(
            "یه خطا موقع دریافت اخبار پیش اومد 😕",
            reply_markup=main_keyboard(),
        )


# =========================================================
# TRANSFERS COMMAND
# =========================================================

async def transfers_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🔄 دارم بازار نقل‌وانتقالات رو بررسی می‌کنم..."
    )

    try:

        articles = await get_transfer_pipeline()

        if not articles:

            await update.message.reply_text(
                "فعلاً خبر نقل‌وانتقالاتی جدیدی پیدا نکردم 😅",
                reply_markup=main_keyboard(),
            )

            return

        for article in articles:

            await post_article(
                context.bot,
                article,
                update.effective_chat.id,
            )

            await asyncio.sleep(
                0.8
            )

    except Exception as e:

        print(
            "TRANSFERS ERROR:",
            type(e).__name__,
            e,
        )

        await update.message.reply_text(
            "یه خطا موقع بررسی نقل‌وانتقالات پیش اومد 😕",
            reply_markup=main_keyboard(),
        )


# =========================================================
# IMPORTANT COMMAND
# =========================================================

async def important_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🚨 دارم اخبار مهم رو بررسی می‌کنم..."
    )

    try:

        articles = await get_full_news_pipeline()

        important_articles = [
            article
            for article in articles
            if article.get(
                "level",
                "NORMAL",
            )
            in {
                "URGENT",
                "IMPORTANT",
            }
        ]

        if not important_articles:

            await update.message.reply_text(
                "فعلاً خبر مهمی پیدا نکردم 👌",
                reply_markup=main_keyboard(),
            )

            return

        for article in important_articles:

            await post_article(
                context.bot,
                article,
                update.effective_chat.id,
            )

            await asyncio.sleep(
                0.8
            )

    except Exception as e:

        print(
            "IMPORTANT ERROR:",
            type(e).__name__,
            e,
        )

        await update.message.reply_text(
            "یه خطا موقع دریافت اخبار مهم پیش اومد 😕",
            reply_markup=main_keyboard(),
        )


# =========================================================
# TEST POST
# =========================================================

async def testpost_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    test_article = {
        "title": "تست Vexa",
        "text": (
            "این یک پیام آزمایشی برای بررسی "
            "سیستم جدید Vexa است."
        ),
        "source": "Vexa",
        "level": "NORMAL",
        "important": False,
        "news_type": "FOOTBALL",
        "image_url": "",
    }

    try:

        success = await post_article(
            context.bot,
            test_article,
            CHANNEL_USERNAME,
        )

        if success:

            await update.message.reply_text(
                "✅ پیام تست با موفقیت ارسال شد.",
                reply_markup=main_keyboard(),
            )

        else:

            await update.message.reply_text(
                "❌ ارسال تست ناموفق بود.",
                reply_markup=main_keyboard(),
            )

    except Exception as e:

        print(
            "TESTPOST ERROR:",
            type(e).__name__,
            e,
        )

        await update.message.reply_text(
            "❌ خطا در تست ارسال.",
            reply_markup=main_keyboard(),
        )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = safe_text(
        update.message.text
    )

    if text == "📰 اخبار جدید":

        await news_command(
            update,
            context,
        )

    elif text == "🔥 نقل‌وانتقالات":

        await transfers_command(
            update,
            context,
        )

    elif text == "🚨 اخبار مهم":

        await important_command(
            update,
            context,
        )

    elif text == "🔄 بروزرسانی":

        await news_command(
            update,
            context,
        )

    elif text == "ℹ️ راهنما":

        await help_command(
            update,
            context,
        )

    elif text == "❌ بستن منو":

        await close_menu(
            update,
            context,
        )


# =========================================================
# AUTOMATIC NEWS
# =========================================================

async def automatic_news_job(
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "\n===================================="
    )

    print(
        "VEXA AUTO JOB STARTED"
    )

    try:

        # ---------------------------------------------
        # TRANSFERS FIRST
        # ---------------------------------------------

        transfer_articles = (
            await get_transfer_pipeline()
        )

        if transfer_articles:

            print(
                "TRANSFER ARTICLES:",
                len(transfer_articles),
            )

            await publish_news(
                transfer_articles,
                context.bot,
                CHANNEL_USERNAME,
            )

        # ---------------------------------------------
        # NORMAL FOOTBALL NEWS
        # ---------------------------------------------

        normal_articles = (
            await get_news_pipeline()
        )

        if normal_articles:

            print(
                "NORMAL ARTICLES:",
                len(normal_articles),
            )

            await publish_news(
                normal_articles,
                context.bot,
                CHANNEL_USERNAME,
            )

        print(
            "VEXA AUTO JOB FINISHED"
        )

    except Exception as e:

        print(
            "AUTO JOB ERROR:",
            type(e).__name__,
            e,
        )

    print(
        "====================================\n"
    )


# =========================================================
# DAILY DIGEST
# =========================================================

async def daily_digest_job(
    context: ContextTypes.DEFAULT_TYPE,
):

    global last_digest_date

    today = (
        now_local()
        .date()
        .isoformat()
    )

    if last_digest_date == today:

        return

    try:

        articles = await get_full_news_pipeline()

        if not articles:

            return

        selected = []

        # اول خبرهای فوری
        for article in articles:

            if (
                article.get(
                    "level",
                    "NORMAL",
                )
                == "URGENT"
            ):

                selected.append(
                    article
                )

        # بعد خبرهای مهم
        for article in articles:

            if len(selected) >= 6:

                break

            if (
                article.get(
                    "level",
                    "NORMAL",
                )
                == "IMPORTANT"
            ):

                if article not in selected:

                    selected.append(
                        article
                    )

        # اگر خبر مهم کافی نبود
        if not selected:

            selected = articles[:5]

        selected = selected[:6]

        lines = [
            "🌙 خلاصه اخبار فوتبال امروز",
            "",
        ]

        for index, article in enumerate(
            selected,
            start=1,
        ):

            news_type = article.get(
                "news_type",
                "FOOTBALL",
            )

            if news_type == "TRANSFER":

                icon = "🔄"

                status = (
                    transfer_status_label(
                        article.get(
                            "transfer_status",
                            "RUMOR",
                        )
                    )
                )

                lines.append(
                    f"{icon} {status}: "
                    f"{article.get('title', '')}"
                )

            else:

                level = article.get(
                    "level",
                    "NORMAL",
                )

                icon = (
                    "🚨"
                    if level == "URGENT"
                    else
                    "🔥"
                    if level == "IMPORTANT"
                    else
                    "⚽"
                )

                lines.append(
                    f"{icon} "
                    f"{article.get('title', '')}"
                )

        await context.bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text="\n".join(lines),
            disable_web_page_preview=True,
        )

        last_digest_date = today

        save_state()

    except Exception as e:

        print(
            "DIGEST ERROR:",
            type(e).__name__,
            e,
        )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application,
):

    await application.bot.set_my_commands(
        [
            (
                "start",
                "شروع ربات",
            ),
            (
                "news",
                "اخبار جدید",
            ),
            (
                "important",
                "اخبار مهم",
            ),
            (
                "transfers",
                "نقل‌وانتقالات",
            ),
            (
                "testpost",
                "تست ارسال",
            ),
            (
                "help",
                "راهنما",
            ),
        ]
    )

    print(
        "BOT COMMANDS CONFIGURED"
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        print(
            "ERROR: BOT_TOKEN is missing."
        )

        return

    print(
        "===================================="
    )

    print(
        "VEXA BOT STARTING..."
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
        "TRANSFER SYSTEM: ENABLED"
    )

    print(
        "TRANSFER STATUSES:"
    )

    print(
        "OFFICIAL / AGREEMENT / "
        "NEGOTIATION / RUMOR / DENIED"
    )

    print(
        "===================================="
    )

    load_state()

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
            start_command,
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
            "testpost",
            testpost_command,
        )
    )

    # Buttons
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            button_handler,
        )
    )

    # Jobs
    if application.job_queue:

        application.job_queue.run_repeating(
            automatic_news_job,
            interval=NEWS_INTERVAL,
            first=FIRST_NEWS_DELAY,
            name="vexa_auto_news",
        )

        application.job_queue.run_daily(
            daily_digest_job,
            time=time(
                DAILY_DIGEST_HOUR,
                DAILY_DIGEST_MINUTE,
                tzinfo=LOCAL_TZ,
            ),
            name="vexa_daily_digest",
        )

        print(
            "JOB QUEUE: ENABLED"
        )

    else:

        print(
            "WARNING: JOB QUEUE UNAVAILABLE"
        )

    print(
        "VEXA IS RUNNING..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
