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
    Defaults,
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

DIGEST_HOUR = 21
DIGEST_MINUTE = 0

TIMEZONE = ZoneInfo("Europe/Budapest")

STATE_FILE = "vexa_state.json"

MAX_ARTICLES_PER_RUN = 8
MAX_TRANSFER_ARTICLES_PER_RUN = 6


# =========================================================
# RSS SOURCES
# =========================================================

FOOTBALL_FEEDS = [
    {
        "name": "BBC Sport Football",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "kind": "football",
    },
    {
        "name": "The Guardian Football",
        "url": "https://www.theguardian.com/football/rss",
        "kind": "football",
    },
]


TRANSFER_FEEDS = [
    {
        "name": "Sky Sports Transfer Centre",
        "url": "https://www.skysports.com/rss/12040",
        "kind": "transfer",
    },
    {
        "name": "The Guardian Transfer Window",
        "url": "https://www.theguardian.com/football/transfer-window/rss",
        "kind": "transfer",
    },
    {
        "name": "The Guardian Rumour Mill",
        "url": "https://www.theguardian.com/football/series/rumourmill/rss",
        "kind": "transfer",
    },
]


# =========================================================
# STATE
# =========================================================

sent_links = set()
sent_title_keys = set()
sent_content_keys = set()
sent_transfer_keys = set()

daily_posted = 0
last_digest_date = None


# =========================================================
# AI
# =========================================================

client = (
    AsyncOpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_API_KEY
    else None
)


# =========================================================
# LOAD / SAVE STATE
# =========================================================

def load_state():
    global sent_links
    global sent_title_keys
    global sent_content_keys
    global sent_transfer_keys
    global last_digest_date

    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        sent_links = set(
            data.get("sent_links", [])
        )

        sent_title_keys = set(
            data.get("sent_title_keys", [])
        )

        sent_content_keys = set(
            data.get("sent_content_keys", [])
        )

        sent_transfer_keys = set(
            data.get("sent_transfer_keys", [])
        )

        last_digest_date = data.get(
            "last_digest_date"
        )

        print(
            f"STATE LOADED | "
            f"links={len(sent_links)} "
            f"titles={len(sent_title_keys)} "
            f"transfers={len(sent_transfer_keys)}"
        )

    except Exception as e:
        print(
            "STATE LOAD ERROR:",
            e,
        )


def save_state():
    try:
        data = {
            "sent_links": list(sent_links)[-3000:],
            "sent_title_keys": list(sent_title_keys)[-3000:],
            "sent_content_keys": list(sent_content_keys)[-3000:],
            "sent_transfer_keys": list(sent_transfer_keys)[-3000:],
            "last_digest_date": last_digest_date,
        }

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
            e,
        )


# =========================================================
# TEXT HELPERS
# =========================================================

def clean_text(text):
    if not text:
        return ""

    text = html.unescape(text)

    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def canonicalize_url(url):
    if not url:
        return ""

    try:
        parsed = urllib.parse.urlparse(
            url
        )

        query = urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )

        blocked = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "gclid",
            "fbclid",
        }

        query = [
            (k, v)
            for k, v in query
            if k.lower() not in blocked
        ]

        clean = parsed._replace(
            query=urllib.parse.urlencode(
                query
            ),
            fragment="",
        )

        return urllib.parse.urlunparse(
            clean
        ).rstrip("/")

    except Exception:
        return url.strip()


def normalize_title(title):
    title = clean_text(
        title
    ).lower()

    title = re.sub(
        r"[^a-zA-Z0-9\u0600-\u06FF\s]",
        " ",
        title,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    )

    return title.strip()


def make_content_key(title):
    normalized = normalize_title(
        title
    )

    if not normalized:
        return ""

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def title_similarity(a, b):
    a = normalize_title(a)
    b = normalize_title(b)

    if not a or not b:
        return 0

    return difflib.SequenceMatcher(
        None,
        a,
        b,
    ).ratio()


# =========================================================
# SPORT FILTER
# =========================================================

NON_FOOTBALL_TERMS = [
    "formula 1",
    "formula one",
    "f1",
    "grand prix",
    "motogp",
    "moto gp",
    "motorsport",
    "nascar",
    "indycar",
    "pit stop",
    "pole position",
    "qualifying",
    "qualifier",
    "lap time",
    "laps",
    "chassis",
    "paddock",
    "tyre strategy",
    "race weekend",

    "nba",
    "nfl",
    "nhl",
    "mlb",
    "cricket",
    "golf",
    "tennis",
    "rugby",
    "boxing",
    "ufc",
    "mma",
    "volleyball",
    "handball",
    "cycling",
    "athletics",
    "swimming",
]


FOOTBALL_TERMS = [
    "football",
    "soccer",
    "premier league",
    "champions league",
    "europa league",
    "conference league",
    "la liga",
    "laliga",
    "serie a",
    "bundesliga",
    "ligue 1",
    "fa cup",
    "carabao cup",
    "uefa",
    "fifa",
    "club",
    "manager",
    "coach",
    "striker",
    "midfielder",
    "defender",
    "goalkeeper",
    "footballer",
    "soccer player",
]


TRANSFER_TERMS = [
    "transfer",
    "transfers",
    "signing",
    "signings",
    "signs",
    "signed",
    "joins",
    "joined",
    "join",
    "move",
    "moved",
    "deal",
    "agreement",
    "agreed",
    "contract",
    "extension",
    "loan",
    "loaned",
    "borrow",
    "bid",
    "bids",
    "offer",
    "offers",
    "interest",
    "interested",
    "negotiations",
    "negotiation",
    "talks",
    "target",
    "targets",
    "pursuit",
    "swap",
    "free agent",
    "released",
    "departure",
    "leaves",
    "left",
    "return",
    "returns",
]


def contains_non_football(text):
    text = text.lower()

    return any(
        term in text
        for term in NON_FOOTBALL_TERMS
    )


def contains_football_signal(text):
    text = text.lower()

    return any(
        term in text
        for term in FOOTBALL_TERMS
    )


def contains_transfer_signal(text):
    text = text.lower()

    return any(
        term in text
        for term in TRANSFER_TERMS
    )


def is_football_article(article):
    title = article.get(
        "title_original",
        "",
    )

    summary = article.get(
        "summary_original",
        "",
    )

    text = (
        f"{title} {summary}"
    ).lower()

    if contains_non_football(text):
        return False

    if not contains_football_signal(text):
        source = article.get(
            "source",
            "",
        ).lower()

        football_sources = [
            "bbc sport football",
            "the guardian football",
            "sky sports transfer centre",
            "the guardian transfer window",
            "the guardian rumour mill",
        ]

        if not any(
            source_name in source
            for source_name in football_sources
        ):
            return False

    return True


def is_football_transfer(article):
    title = article.get(
        "title_original",
        "",
    )

    summary = article.get(
        "summary_original",
        "",
    )

    source = article.get(
        "source",
        "",
    )

    text = (
        f"{title} {summary}"
    ).lower()

    if contains_non_football(text):
        print(
            "TRANSFER SPORT BLOCK:",
            title,
        )
        return False

    if not contains_football_signal(text):
        if "transfer" not in source.lower():
            print(
                "TRANSFER FOOTBALL SIGNAL BLOCK:",
                title,
            )
            return False

    if not contains_transfer_signal(text):
        print(
            "TRANSFER SIGNAL BLOCK:",
            title,
        )
        return False

    return True


# =========================================================
# LANGUAGE DETECTION
# =========================================================

def language_stats(text):
    if not text:
        return {
            "persian": 0,
            "latin": 0,
            "digits": 0,
            "letters": 0,
        }

    persian = len(
        re.findall(
            r"[\u0600-\u06FF]",
            text,
        )
    )

    latin = len(
        re.findall(
            r"[A-Za-z]",
            text,
        )
    )

    digits = len(
        re.findall(
            r"\d",
            text,
        )
    )

    letters = persian + latin

    return {
        "persian": persian,
        "latin": latin,
        "digits": digits,
        "letters": letters,
    }


def is_persian_text(text):
    """
    بررسی می‌کند که متن واقعاً فارسی باشد.
    نام بازیکنان، باشگاه‌ها و کلمات خاص لاتین
    مجاز هستند؛ ولی متن انگلیسی کامل رد می‌شود.
    """

    stats = language_stats(text)

    persian = stats["persian"]
    latin = stats["latin"]

    if persian < 8:
        return False

    if latin == 0:
        return True

    # اگر متن طولانی است، بخش فارسی باید غالب باشد.
    if persian >= latin:
        return True

    # برای متن‌هایی که نام لاتین دارند،
    # کمی لاتین مجاز است.
    if persian >= 25 and latin <= persian * 0.45:
        return True

    return False


# =========================================================
# PARSE AI FIELDS
# =========================================================

def extract_ai_field(
    text,
    field_name,
    next_fields=None,
):
    if not text:
        return ""

    if next_fields is None:
        next_fields = []

    escaped_next = "|".join(
        re.escape(field)
        for field in next_fields
    )

    if escaped_next:
        pattern = (
            rf"{re.escape(field_name)}\s*:\s*"
            rf"(.*?)"
            rf"(?=\n(?:{escaped_next})\s*:|$)"
        )
    else:
        pattern = (
            rf"{re.escape(field_name)}\s*:\s*(.*)"
        )

    match = re.search(
        pattern,
        text,
        re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return ""

    return clean_text(
        match.group(1)
    )


# =========================================================
# IMAGE FROM RSS
# =========================================================

def get_entry_image(entry):
    try:
        media_content = entry.get(
            "media_content"
        )

        if media_content:
            for item in media_content:
                url = item.get("url")

                if url:
                    return url

        media_thumbnail = entry.get(
            "media_thumbnail"
        )

        if media_thumbnail:
            for item in media_thumbnail:
                url = item.get("url")

                if url:
                    return url

        for link in entry.get(
            "links",
            [],
        ):
            link_type = link.get(
                "type",
                "",
            )

            if "image" in link_type:
                url = link.get(
                    "href"
                )

                if url:
                    return url

    except Exception:
        pass

    return ""


# =========================================================
# IMAGE DOWNLOAD
# =========================================================

async def download_image(url):
    if not url:
        return None

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "Chrome/130 Safari/537.36"
            )
        }

        request = urllib.request.Request(
            url,
            headers=headers,
        )

        def fetch():
            with urllib.request.urlopen(
                request,
                timeout=15,
            ) as response:
                return response.read()

        data = await asyncio.to_thread(
            fetch
        )

        if not data:
            return None

        return BytesIO(data)

    except Exception as e:
        print(
            "IMAGE DOWNLOAD ERROR:",
            e,
        )

        return None


# =========================================================
# RSS PARSER
# =========================================================

def parse_feed(feed_info):
    articles = []

    try:
        feed = feedparser.parse(
            feed_info["url"]
        )

        for entry in feed.entries[:30]:

            title = clean_text(
                entry.get(
                    "title",
                    "",
                )
            )

            summary = clean_text(
                entry.get(
                    "summary",
                    "",
                )
                or entry.get(
                    "description",
                    "",
                )
            )

            link = canonicalize_url(
                entry.get(
                    "link",
                    "",
                )
            )

            if not title or not link:
                continue

            image = get_entry_image(
                entry
            )

            articles.append(
                {
                    "title_original": title,
                    "summary_original": summary,
                    "link": link,
                    "source": feed_info["name"],
                    "kind": feed_info["kind"],
                    "image": image,
                }
            )

    except Exception as e:
        print(
            f"FEED ERROR | "
            f"{feed_info['name']} | {e}"
        )

    return articles


# =========================================================
# DUPLICATES
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
                or article.get(
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


def transfer_event_key(article):
    player = normalize_title(
        article.get(
            "player",
            "",
        )
    )

    from_club = normalize_title(
        article.get(
            "from_club",
            "",
        )
    )

    to_club = normalize_title(
        article.get(
            "to_club",
            "",
        )
    )

    status = article.get(
        "transfer_status",
        "",
    )

    transfer_type = article.get(
        "transfer_type",
        "",
    )

    raw = (
        f"{player}|"
        f"{from_club}|"
        f"{to_club}|"
        f"{status}|"
        f"{transfer_type}"
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


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
        or article.get(
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
        sent_title_keys.add(title_key)

    if content_key:
        sent_content_keys.add(content_key)

    save_state()


def mark_transfer_sent(article):
    key = transfer_event_key(
        article
    )

    if key:
        sent_transfer_keys.add(key)

    save_state()


# =========================================================
# UNIVERSAL PERSIAN REWRITE
# =========================================================

async def force_persian_news(article):
    """
    این تابع برای تمام اخبار استفاده می‌شود،
    نه فقط نقل‌وانتقالات.
    """

    if not client:
        return None

    title = clean_text(
        article.get(
            "title",
            "",
        )
    )

    summary = clean_text(
        article.get(
            "summary",
            "",
        )
    )

    combined = (
        f"{title}\n{summary}"
    )

    if is_persian_text(combined):
        return article

    print(
        "PERSIAN REWRITE NEEDED:",
        title,
    )

    prompt = f"""
این خبر درباره فوتبال است.

متن زیر ممکن است انگلیسی باشد.
آن را به فارسی روان، طبیعی و حرفه‌ای برای انتشار در یک کانال خبری فوتبال بازنویسی کن.

عنوان اصلی:
{title}

متن اصلی:
{summary}

قوانین بسیار مهم:
- ترجمه و بازنویسی کامل به فارسی.
- هیچ جمله انگلیسی در TITLE یا SUMMARY باقی نماند.
- نام بازیکنان و باشگاه‌ها را می‌توان به شکل رایج فارسی نوشت.
- اطلاعات جدید یا ساختگی اضافه نکن.
- معنی خبر را تغییر نده.
- متن خبری طبیعی و کوتاه باشد.

فقط این دو خط را بده:

TITLE: تیتر فارسی
SUMMARY: خلاصه فارسی 2 تا 4 جمله‌ای
"""

    try:
        response = await client.responses.create(
            model=AI_MODEL,
            input=prompt,
        )

        text = response.output_text.strip()

        new_title = extract_ai_field(
            text,
            "TITLE",
            ["SUMMARY"],
        )

        new_summary = extract_ai_field(
            text,
            "SUMMARY",
            [],
        )

        if not new_title or not new_summary:
            print(
                "PERSIAN REWRITE PARSE ERROR:",
                title,
            )
            return None

        combined_new = (
            f"{new_title}\n{new_summary}"
        )

        if not is_persian_text(
            combined_new
        ):
            print(
                "PERSIAN REWRITE FAILED:",
                new_title,
            )
            return None

        return {
            **article,
            "title": new_title,
            "summary": new_summary,
        }

    except Exception as e:
        print(
            "PERSIAN REWRITE ERROR:",
            e,
        )

        return None


# =========================================================
# AI NORMAL NEWS
# =========================================================

async def classify_normal_news(article):
    if not client:
        return None

    title = article[
        "title_original"
    ]

    summary = article[
        "summary_original"
    ]

    prompt = f"""
تو یک سردبیر حرفه‌ای اخبار فوتبال هستی.

عنوان اصلی:
{title}

متن خبر:
{summary}

خبر را فقط برای فوتبال بررسی و به فارسی حرفه‌ای تبدیل کن.

خروجی دقیقاً:

SPORT: FOOTBALL
IMPORTANCE: URGENT یا IMPORTANT یا NORMAL
TITLE: تیتر فارسی کوتاه
SUMMARY: خلاصه فارسی 2 تا 4 جمله‌ای

قوانین:
- فقط فوتبال.
- اگر خبر درباره F1، Formula 1، MotoGP یا هر ورزش دیگری است، SPORT: OTHER بنویس.
- TITLE و SUMMARY حتماً فارسی باشند.
- متن انگلیسی را کپی نکن.
- اطلاعات جدید و ساختگی اضافه نکن.
- خبر عادی NORMAL باشد.
- فقط خبر واقعاً فوری URGENT باشد.
- خبر مهم ولی غیرفوری IMPORTANT باشد.
"""

    try:
        response = await client.responses.create(
            model=AI_MODEL,
            input=prompt,
        )

        text = response.output_text.strip()

        sport_match = re.search(
            r"SPORT:\s*([A-Z_]+)",
            text,
            re.IGNORECASE,
        )

        if not sport_match:
            return None

        if (
            sport_match.group(1).upper()
            != "FOOTBALL"
        ):
            print(
                "NORMAL SPORT BLOCK:",
                title,
            )
            return None

        importance_match = re.search(
            r"IMPORTANCE:\s*"
            r"(URGENT|IMPORTANT|NORMAL)",
            text,
            re.IGNORECASE,
        )

        importance = (
            importance_match.group(1).upper()
            if importance_match
            else "NORMAL"
        )

        new_title = extract_ai_field(
            text,
            "TITLE",
            ["SUMMARY"],
        )

        new_summary = extract_ai_field(
            text,
            "SUMMARY",
            [],
        )

        if not new_title or not new_summary:
            print(
                "NORMAL AI PARSE ERROR:",
                title,
            )
            return None

        result = {
            **article,
            "title": new_title,
            "summary": new_summary,
            "importance": importance,
            "news_type": "NORMAL",
        }

        # مرحله اجباری فارسی‌سازی
        result = await force_persian_news(
            result
        )

        if not result:
            return None

        combined = (
            result.get("title", "")
            + " "
            + result.get("summary", "")
        )

        if not is_persian_text(
            combined
        ):
            print(
                "NORMAL FINAL LANGUAGE BLOCK:",
                title,
            )
            return None

        # فیلتر نهایی ورزش
        original_combined = (
            article.get(
                "title_original",
                "",
            )
            + " "
            + article.get(
                "summary_original",
                "",
            )
        )

        if contains_non_football(
            original_combined
        ):
            print(
                "NORMAL FINAL SPORT BLOCK:",
                title,
            )
            return None

        return result

    except Exception as e:
        print(
            "NORMAL AI ERROR:",
            e,
        )

        return None


# =========================================================
# AI TRANSFER NEWS
# =========================================================

async def classify_transfer_news(article):
    if not client:
        return None

    if not is_football_transfer(
        article
    ):
        return None

    title = article[
        "title_original"
    ]

    summary = article[
        "summary_original"
    ]

    prompt = f"""
تو سردبیر تخصصی نقل‌وانتقالات فوتبال هستی.

عنوان اصلی:
{title}

متن خبر:
{summary}

خروجی دقیقاً:

SPORT: FOOTBALL
IS_TRANSFER: YES یا NO
STATUS: OFFICIAL یا AGREEMENT یا NEGOTIATION یا RUMOR یا DENIED
TYPE: PERMANENT یا LOAN یا FREE یا EXTENSION یا RETURN یا UNKNOWN
PLAYER: نام بازیکن
FROM: باشگاه قبلی
TO: باشگاه جدید
FEE: مبلغ یا UNKNOWN
CONTRACT: مدت قرارداد یا UNKNOWN
TITLE: تیتر فارسی
SUMMARY: خلاصه فارسی 2 تا 4 جمله‌ای

قوانین:
- فقط فوتبال.
- F1، Formula 1، MotoGP، Motorsport و سایر ورزش‌ها ممنوع.
- اگر انتقال فوتبال نیست IS_TRANSFER: NO.
- TITLE و SUMMARY حتماً فارسی باشند.
- متن انگلیسی را کپی نکن.
- اطلاعات ساختگی اضافه نکن.
- OFFICIAL فقط برای انتقال/تمدید رسمی.
- AGREEMENT برای توافق گزارش‌شده.
- NEGOTIATION برای مذاکره.
- RUMOR برای شایعه.
- DENIED برای تکذیب.
- اطلاعات نامعلوم را UNKNOWN بنویس.
"""

    try:
        response = await client.responses.create(
            model=AI_MODEL,
            input=prompt,
        )

        text = response.output_text.strip()

        sport_match = re.search(
            r"SPORT:\s*([A-Z_]+)",
            text,
            re.IGNORECASE,
        )

        if not sport_match:
            return None

        if (
            sport_match.group(1).upper()
            != "FOOTBALL"
        ):
            print(
                "TRANSFER AI SPORT BLOCK:",
                title,
            )
            return None

        transfer_match = re.search(
            r"IS_TRANSFER:\s*(YES|NO)",
            text,
            re.IGNORECASE,
        )

        if not transfer_match:
            return None

        if (
            transfer_match.group(1).upper()
            != "YES"
        ):
            print(
                "TRANSFER AI NOT TRANSFER:",
                title,
            )
            return None

        status = extract_ai_field(
            text,
            "STATUS",
            [
                "TYPE",
                "PLAYER",
                "FROM",
                "TO",
                "FEE",
                "CONTRACT",
                "TITLE",
                "SUMMARY",
            ],
        ).upper()

        transfer_type = extract_ai_field(
            text,
            "TYPE",
            [
                "PLAYER",
                "FROM",
                "TO",
                "FEE",
                "CONTRACT",
                "TITLE",
                "SUMMARY",
            ],
        ).upper()

        player = extract_ai_field(
            text,
            "PLAYER",
            [
                "FROM",
                "TO",
                "FEE",
                "CONTRACT",
                "TITLE",
                "SUMMARY",
            ],
        )

        from_club = extract_ai_field(
            text,
            "FROM",
            [
                "TO",
                "FEE",
                "CONTRACT",
                "TITLE",
                "SUMMARY",
            ],
        )

        to_club = extract_ai_field(
            text,
            "TO",
            [
                "FEE",
                "CONTRACT",
                "TITLE",
                "SUMMARY",
            ],
        )

        fee = extract_ai_field(
            text,
            "FEE",
            [
                "CONTRACT",
                "TITLE",
                "SUMMARY",
            ],
        )

        contract = extract_ai_field(
            text,
            "CONTRACT",
            [
                "TITLE",
                "SUMMARY",
            ],
        )

        new_title = extract_ai_field(
            text,
            "TITLE",
            ["SUMMARY"],
        )

        new_summary = extract_ai_field(
            text,
            "SUMMARY",
            [],
        )

        if not new_title or not new_summary:
            print(
                "TRANSFER AI PARSE ERROR:",
                title,
            )
            return None

        valid_statuses = {
            "OFFICIAL",
            "AGREEMENT",
            "NEGOTIATION",
            "RUMOR",
            "DENIED",
        }

        if status not in valid_statuses:
            status = "RUMOR"

        valid_types = {
            "PERMANENT",
            "LOAN",
            "FREE",
            "EXTENSION",
            "RETURN",
            "UNKNOWN",
        }

        if transfer_type not in valid_types:
            transfer_type = "UNKNOWN"

        result = {
            **article,
            "title": new_title,
            "summary": new_summary,
            "news_type": "TRANSFER",
            "transfer_status": status,
            "transfer_type": transfer_type,
            "player": player or "UNKNOWN",
            "from_club": from_club or "UNKNOWN",
            "to_club": to_club or "UNKNOWN",
            "fee": fee or "UNKNOWN",
            "contract": contract or "UNKNOWN",
        }

        # فارسی‌سازی مشترک
        result = await force_persian_news(
            result
        )

        if not result:
            return None

        combined = (
            result.get("title", "")
            + " "
            + result.get("summary", "")
        )

        if not is_persian_text(
            combined
        ):
            print(
                "TRANSFER FINAL LANGUAGE BLOCK:",
                result.get("title"),
            )
            return None

        original_combined = (
            article.get(
                "title_original",
                "",
            )
            + " "
            + article.get(
                "summary_original",
                "",
            )
        )

        if contains_non_football(
            original_combined
        ):
            print(
                "TRANSFER FINAL SPORT BLOCK:",
                result.get("title"),
            )
            return None

        return result

    except Exception as e:
        print(
            "TRANSFER AI ERROR:",
            e,
        )

        return None


# =========================================================
# COLLECT NORMAL NEWS
# =========================================================

async def get_normal_news():
    collected = []

    for feed_info in FOOTBALL_FEEDS:

        articles = await asyncio.to_thread(
            parse_feed,
            feed_info,
        )

        for article in articles:

            if not is_football_article(
                article
            ):
                continue

            if is_duplicate_article(
                article["link"],
                article["title_original"],
                article["source"],
                collected,
            ):
                continue

            collected.append(article)

    return collected[
        :MAX_ARTICLES_PER_RUN
    ]


# =========================================================
# COLLECT TRANSFERS
# =========================================================

async def get_transfer_news():
    collected = []

    for feed_info in TRANSFER_FEEDS:

        articles = await asyncio.to_thread(
            parse_feed,
            feed_info,
        )

        for article in articles:

            if not is_football_transfer(
                article
            ):
                continue

            if is_duplicate_article(
                article["link"],
                article["title_original"],
                article["source"],
                collected,
            ):
                continue

            collected.append(article)

    return collected[
        :MAX_TRANSFER_ARTICLES_PER_RUN
    ]


# =========================================================
# PIPELINES
# =========================================================

async def get_news_pipeline():
    raw_articles = await get_normal_news()

    final_articles = []

    for article in raw_articles:

        processed = await classify_normal_news(
            article
        )

        if processed:
            final_articles.append(
                processed
            )

    return final_articles


async def get_transfer_pipeline():
    raw_articles = await get_transfer_news()

    final_articles = []

    for article in raw_articles:

        processed = await classify_transfer_news(
            article
        )

        if not processed:
            continue

        event_key = transfer_event_key(
            processed
        )

        if event_key in sent_transfer_keys:
            print(
                "TRANSFER EVENT DUPLICATE:",
                processed.get("title"),
            )
            continue

        final_articles.append(
            processed
        )

    return final_articles


# =========================================================
# TRANSFER LABELS
# =========================================================

def transfer_status_label(status):
    labels = {
        "OFFICIAL": "✅ انتقال رسمی",
        "AGREEMENT": "📝 توافق",
        "NEGOTIATION": "🤝 مذاکرات",
        "RUMOR": "🟡 شایعه",
        "DENIED": "❌ تکذیب",
    }

    return labels.get(
        status,
        "🔄 نقل‌وانتقال",
    )


def transfer_type_label(
    transfer_type
):
    labels = {
        "PERMANENT": "انتقال دائمی",
        "LOAN": "قرضی",
        "FREE": "بازیکن آزاد",
        "EXTENSION": "تمدید قرارداد",
        "RETURN": "بازگشت",
        "UNKNOWN": "",
    }

    return labels.get(
        transfer_type,
        "",
    )


# =========================================================
# BUILD POST
# =========================================================

def build_post_text(article):
    news_type = article.get(
        "news_type",
        "NORMAL",
    )

    title = article.get(
        "title",
        article.get(
            "title_original",
            "",
        ),
    )

    summary = article.get(
        "summary",
        article.get(
            "summary_original",
            "",
        ),
    )

    source = article.get(
        "source",
        "",
    )

    if news_type == "TRANSFER":

        status = article.get(
            "transfer_status",
            "RUMOR",
        )

        transfer_type = article.get(
            "transfer_type",
            "UNKNOWN",
        )

        player = article.get(
            "player",
            "",
        )

        from_club = article.get(
            "from_club",
            "",
        )

        to_club = article.get(
            "to_club",
            "",
        )

        fee = article.get(
            "fee",
            "UNKNOWN",
        )

        contract = article.get(
            "contract",
            "UNKNOWN",
        )

        lines = [
            transfer_status_label(
                status
            ),
            "",
            f"🔥 {title}",
            "",
            summary,
            "",
        ]

        if (
            player
            and player != "UNKNOWN"
        ):
            lines.append(
                f"👤 بازیکن: {player}"
            )

        if (
            from_club
            and from_club != "UNKNOWN"
        ):
            lines.append(
                f"🔵 از: {from_club}"
            )

        if (
            to_club
            and to_club != "UNKNOWN"
        ):
            lines.append(
                f"🟢 به: {to_club}"
            )

        type_label = transfer_type_label(
            transfer_type
        )

        if type_label:
            lines.append(
                f"📌 نوع: {type_label}"
            )

        if fee and fee != "UNKNOWN":
            lines.append(
                f"💰 مبلغ: {fee}"
            )

        if (
            contract
            and contract != "UNKNOWN"
        ):
            lines.append(
                f"📄 قرارداد: {contract}"
            )

        lines.extend(
            [
                "",
                f"📰 منبع: {source}",
            ]
        )

        return "\n".join(lines)

    # -----------------------------------------------------
    # NORMAL
    # -----------------------------------------------------

    level = article.get(
        "importance",
        "NORMAL",
    )

    if level == "URGENT":
        prefix = "🚨 خبر فوری"
    elif level == "IMPORTANT":
        prefix = "🔥 خبر مهم"
    else:
        prefix = "⚽ خبر فوتبال"

    return (
        f"{prefix}\n\n"
        f"🔥 {title}\n\n"
        f"{summary}\n\n"
        f"📰 منبع: {source}"
    )


# =========================================================
# IMAGE
# =========================================================

async def prepare_image(article):
    image_url = article.get(
        "image",
        "",
    )

    if not image_url:
        return None

    return await download_image(
        image_url
    )


# =========================================================
# POST
# =========================================================

async def post_article(
    bot,
    article,
    target=CHANNEL_USERNAME,
):
    try:
        text = build_post_text(
            article
        )

        image = await prepare_image(
            article
        )

        if image:

            await bot.send_photo(
                chat_id=target,
                photo=InputFile(
                    image,
                    filename="news.jpg",
                ),
                caption=text[:1024],
            )

        else:

            await bot.send_message(
                chat_id=target,
                text=text[:4096],
            )

        print(
            "POSTED:",
            article.get(
                "title",
                article.get(
                    "title_original",
                    "",
                ),
            ),
        )

        return True

    except Exception as e:
        print(
            "POST ERROR:",
            e,
        )

        return False


# =========================================================
# PUBLISH
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
            or article.get(
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
            (link and link in sent_links)
            or (
                title_key
                and title_key in sent_title_keys
            )
            or (
                content_key
                and content_key in sent_content_keys
            )
        ):
            print(
                "DUPLICATE BLOCKED:",
                title,
            )
            continue

        # ---------------------------------------------
        # FINAL LANGUAGE BLOCK
        # ---------------------------------------------

        final_title = article.get(
            "title",
            "",
        )

        final_summary = article.get(
            "summary",
            "",
        )

        if not is_persian_text(
            f"{final_title} {final_summary}"
        ):
            print(
                "PUBLISH LANGUAGE BLOCK:",
                final_title,
            )
            continue

        # ---------------------------------------------
        # FINAL SPORT BLOCK
        # ---------------------------------------------

        original_text = (
            article.get(
                "title_original",
                "",
            )
            + " "
            + article.get(
                "summary_original",
                "",
            )
        )

        if contains_non_football(
            original_text
        ):
            print(
                "PUBLISH SPORT BLOCK:",
                final_title,
            )
            continue

        # ---------------------------------------------
        # TRANSFER CHECK
        # ---------------------------------------------

        if article.get(
            "news_type"
        ) == "TRANSFER":

            event_key = transfer_event_key(
                article
            )

            if event_key in sent_transfer_keys:
                print(
                    "TRANSFER EVENT BLOCK:",
                    final_title,
                )
                continue

        # ---------------------------------------------
        # POST
        # ---------------------------------------------

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
            "🔥 اخبار مهم",
        ],
        [
            "🔄 نقل‌وانتقالات",
            "ℹ️ راهنما",
        ],
        [
            "❌ بستن منو",
        ],
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
    )


# =========================================================
# COMMANDS
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "سلام داداش 👋🔥\n\n"
        "من Vexa هستم؛ دستیار اخبار فوتبال.\n\n"
        "از منوی پایین می‌تونی اخبار جدید، "
        "اخبار مهم و نقل‌وانتقالات رو ببینی.",
        reply_markup=main_keyboard(),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "📚 راهنمای Vexa\n\n"
        "/start — شروع\n"
        "/help — راهنما\n"
        "/news — بررسی اخبار جدید\n"
        "/important — اخبار مهم\n"
        "/transfers — نقل‌وانتقالات\n"
        "/testpost — تست ارسال\n",
        reply_markup=main_keyboard(),
    )


async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔎 دارم اخبار جدید فوتبال رو بررسی می‌کنم..."
    )

    try:
        articles = await get_news_pipeline()

        if not articles:
            await update.message.reply_text(
                "فعلاً خبر جدید و قابل‌انتشاری پیدا نکردم 😅"
            )
            return

        count = await publish_news(
            articles,
            context.bot,
        )

        await update.message.reply_text(
            f"✅ بررسی تمام شد.\n"
            f"📤 {count} خبر ارسال شد."
        )

    except Exception as e:
        print(
            "NEWS COMMAND ERROR:",
            e,
        )

        await update.message.reply_text(
            "❌ هنگام بررسی اخبار خطایی رخ داد."
        )


async def transfers_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔄 دارم نقل‌وانتقالات فوتبال رو بررسی می‌کنم..."
    )

    try:
        articles = await get_transfer_pipeline()

        if not articles:
            await update.message.reply_text(
                "فعلاً انتقال جدید و قابل‌انتشاری پیدا نکردم."
            )
            return

        count = await publish_news(
            articles,
            context.bot,
        )

        await update.message.reply_text(
            f"✅ بررسی نقل‌وانتقالات تمام شد.\n"
            f"📤 {count} خبر ارسال شد."
        )

    except Exception as e:
        print(
            "TRANSFER COMMAND ERROR:",
            e,
        )

        await update.message.reply_text(
            "❌ هنگام بررسی نقل‌وانتقالات خطایی رخ داد."
        )


async def important_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔥 دارم اخبار مهم فوتبال رو بررسی می‌کنم..."
    )

    try:
        articles = await get_news_pipeline()

        important = [
            article
            for article in articles
            if article.get(
                "importance"
            )
            in {
                "URGENT",
                "IMPORTANT",
            }
        ]

        if not important:
            await update.message.reply_text(
                "فعلاً خبر مهمی پیدا نشد."
            )
            return

        count = await publish_news(
            important,
            context.bot,
        )

        await update.message.reply_text(
            f"🔥 {count} خبر مهم ارسال شد."
        )

    except Exception as e:
        print(
            "IMPORTANT ERROR:",
            e,
        )

        await update.message.reply_text(
            "❌ خطا در بررسی اخبار مهم."
        )


async def testpost_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        test_article = {
            "title": (
                "Vexa آماده دریافت "
                "اخبار فوتبال است"
            ),
            "summary": (
                "این یک پیام آزمایشی برای "
                "بررسی عملکرد ارسال ربات است."
            ),
            "source": "Vexa",
            "news_type": "NORMAL",
            "importance": "NORMAL",
            "image": "",
        }

        await post_article(
            context.bot,
            test_article,
        )

        await update.message.reply_text(
            "✅ پیام تست ارسال شد."
        )

    except Exception as e:
        print(
            "TEST POST ERROR:",
            e,
        )

        await update.message.reply_text(
            "❌ ارسال تست ناموفق بود."
        )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = update.message.text

    if text == "📰 اخبار جدید":

        await news_command(
            update,
            context,
        )

    elif text == "🔥 اخبار مهم":

        await important_command(
            update,
            context,
        )

    elif text == "🔄 نقل‌وانتقالات":

        await transfers_command(
            update,
            context,
        )

    elif text == "ℹ️ راهنما":

        await help_command(
            update,
            context,
        )

    elif text == "❌ بستن منو":

        await update.message.reply_text(
            "منو بسته شد 👌",
            reply_markup=ReplyKeyboardRemove(),
        )


# =========================================================
# AUTOMATIC NEWS
# =========================================================

async def auto_news_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    global daily_posted

    print(
        "AUTO NEWS CHECK STARTED"
    )

    try:

        # ---------------------------------------------
        # NORMAL FOOTBALL NEWS
        # ---------------------------------------------

        news = await get_news_pipeline()

        if news:

            count = await publish_news(
                news,
                context.bot,
            )

            daily_posted += count

            print(
                f"NORMAL NEWS POSTED: {count}"
            )

        # ---------------------------------------------
        # TRANSFER NEWS
        # ---------------------------------------------

        transfers = (
            await get_transfer_pipeline()
        )

        if transfers:

            count = await publish_news(
                transfers,
                context.bot,
            )

            daily_posted += count

            print(
                f"TRANSFER NEWS POSTED: {count}"
            )

        print(
            "AUTO NEWS CHECK FINISHED"
        )

    except Exception as e:
        print(
            "AUTO NEWS ERROR:",
            e,
        )


# =========================================================
# DAILY DIGEST
# =========================================================

async def daily_digest_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    global daily_posted
    global last_digest_date

    now = datetime.now(
        TIMEZONE
    )

    today = now.strftime(
        "%Y-%m-%d"
    )

    if last_digest_date == today:
        return

    try:

        text = (
            "📊 خلاصه فعالیت امروز Vexa\n\n"
            f"📰 تعداد اخبار ارسال‌شده: "
            f"{daily_posted}\n\n"
            "⚽ فوتبال | 🔄 نقل‌وانتقالات\n\n"
            "Vexa همچنان اخبار را به‌صورت خودکار "
            "بررسی می‌کند. 🤖🔥"
        )

        await context.bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=text,
        )

        last_digest_date = today
        daily_posted = 0

        save_state()

        print(
            "DAILY DIGEST SENT"
        )

    except Exception as e:
        print(
            "DIGEST ERROR:",
            e,
        )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application,
):
    print(
        "VEXA BOT STARTED"
    )

    print(
        f"CHANNEL: {CHANNEL_USERNAME}"
    )

    print(
        f"AI MODEL: {AI_MODEL}"
    )

    print(
        f"TIMEZONE: {TIMEZONE}"
    )

    # ---------------------------------------------
    # FIRST CHECK
    # ---------------------------------------------

    application.job_queue.run_once(
        auto_news_job,
        when=FIRST_NEWS_DELAY,
        name="first_news_check",
    )

    # ---------------------------------------------
    # EVERY 10 MINUTES
    # ---------------------------------------------

    application.job_queue.run_repeating(
        auto_news_job,
        interval=NEWS_INTERVAL,
        first=FIRST_NEWS_DELAY + 5,
        name="automatic_news",
    )

    # ---------------------------------------------
    # DAILY DIGEST 21:00
    # ---------------------------------------------

    application.job_queue.run_daily(
        daily_digest_job,
        time=time(
            hour=DIGEST_HOUR,
            minute=DIGEST_MINUTE,
            second=0,
        ),
        name="daily_digest",
    )

    print(
        "SCHEDULER READY"
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context: ContextTypes.DEFAULT_TYPE,
):
    print(
        "BOT ERROR:",
        context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY environment variable is missing."
        )

    load_state()

    defaults = Defaults(
        tzinfo=TIMEZONE
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .post_init(post_init)
        .build()
    )

    # ---------------------------------------------
    # COMMANDS
    # ---------------------------------------------

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

    # ---------------------------------------------
    # KEYBOARD
    # ---------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            button_handler,
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "STARTING POLLING..."
    )

    application.run_polling(
        drop_pending_updates=True
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
