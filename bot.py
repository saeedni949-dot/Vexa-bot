import asyncio
import json
import os
import re
import time as time_module
from datetime import time
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit
from html import escape

import feedparser
from openai import AsyncOpenAI

from telegram import (
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

CHANNEL_USERNAME = "@fcnewsss"

AI_MODEL = "gpt-6-luna"

NEWS_INTERVAL = 600
FIRST_NEWS_DELAY = 30

DIGEST_HOUR = 21
DIGEST_MINUTE = 0

TIMEZONE = "Europe/Budapest"

MAX_ARTICLES_PER_RUN = 6
MAX_TRANSFER_ARTICLES_PER_RUN = 4

MAX_AI_BATCH = 8

AI_COOLDOWN_DEFAULT = 1800

STATE_FILE = "vexa_state.json"


# =========================================================
# FEEDS
# =========================================================

FOOTBALL_FEEDS = [
    (
        "BBC Sport",
        "https://feeds.bbci.co.uk/sport/football/rss.xml",
    ),
    (
        "The Guardian",
        "https://www.theguardian.com/football/rss",
    ),
]

TRANSFER_FEEDS = [
    (
        "Sky Sports",
        "https://www.skysports.com/rss/12040",
    ),
    (
        "The Guardian",
        "https://www.theguardian.com/football/transfer-window/rss",
    ),
    (
        "The Guardian",
        "https://www.theguardian.com/football/series/rumour+mill/rss",
    ),
]


# =========================================================
# STATE
# =========================================================

sent_links = set()
sent_title_keys = set()
sent_content_keys = set()

ai_cooldown_until = 0


# =========================================================
# OPENAI
# =========================================================

client = None

if OPENAI_API_KEY:
    client = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )


# =========================================================
# LOG
# =========================================================

def log(message):
    print(
        f"[VEXA] {message}",
        flush=True,
    )


# =========================================================
# STATE
# =========================================================

def load_state():
    global sent_links
    global sent_title_keys
    global sent_content_keys

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

        log(
            "STATE LOADED: "
            f"{len(sent_links)} links / "
            f"{len(sent_title_keys)} titles"
        )

    except Exception as e:
        log(
            f"STATE LOAD ERROR: {e}"
        )


def save_state():
    try:
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
        log(
            f"STATE SAVE ERROR: {e}"
        )


# =========================================================
# TEXT NORMALIZATION
# =========================================================

def normalize_text(text):
    if not text:
        return ""

    text = str(text)

    text = text.replace(
        "\u200c",
        " ",
    )

    text = text.replace(
        "\u200f",
        " ",
    )

    text = text.replace(
        "\u200e",
        " ",
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def normalize_title(title):
    text = normalize_text(
        title
    ).lower()

    text = re.sub(
        r"https?://\S+",
        "",
        text,
    )

    text = re.sub(
        r"[^\w\s\u0600-\u06ff]",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def make_content_key(title):
    key = normalize_title(
        title
    )

    words = key.split()

    return " ".join(
        words[:18]
    )


def canonicalize_url(url):
    if not url:
        return ""

    try:
        parts = urlsplit(
            url.strip()
        )

        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/"),
                "",
                "",
            )
        )

    except Exception:
        return url.strip()


def title_similarity(a, b):
    a = normalize_title(a)
    b = normalize_title(b)

    if not a or not b:
        return 0

    sa = set(a.split())
    sb = set(b.split())

    if not sa or not sb:
        return 0

    return len(sa & sb) / max(
        len(sa),
        len(sb),
    )


# =========================================================
# PERSIAN LANGUAGE CHECK
# =========================================================

COMMON_ENGLISH_WORDS = {
    "the",
    "and",
    "this",
    "that",
    "with",
    "from",
    "football",
    "transfer",
    "club",
    "player",
    "manager",
    "coach",
    "official",
    "deal",
    "agreement",
    "contract",
    "loan",
    "signing",
    "joins",
    "joined",
    "rumour",
    "rumor",
    "report",
    "reports",
    "according",
    "sources",
    "news",
}


def language_stats(text):
    if not text:
        return {
            "persian": 0,
            "latin": 0,
            "digits": 0,
            "total": 0,
        }

    persian = len(
        re.findall(
            r"[\u0600-\u06ff]",
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

    total = len(
        re.findall(
            r"[\u0600-\u06ffA-Za-z0-9]",
            text,
        )
    )

    return {
        "persian": persian,
        "latin": latin,
        "digits": digits,
        "total": total,
    }


def is_persian_text(text):
    stats = language_stats(
        text
    )

    if stats["persian"] < 8:
        return False

    if stats["total"] == 0:
        return False

    ratio = (
        stats["persian"]
        / stats["total"]
    )

    return ratio >= 0.35


def contains_too_much_english(text):
    if not text:
        return False

    words = re.findall(
        r"\b[A-Za-z]{2,}\b",
        text.lower(),
    )

    if not words:
        return False

    common = sum(
        1
        for word in words
        if word in COMMON_ENGLISH_WORDS
    )

    return (
        len(words) >= 5
        and common >= 3
    )


# =========================================================
# FOOTBALL FILTERS
# =========================================================

NON_FOOTBALL_TERMS = [
    "formula 1",
    "formula one",
    "f1",
    "motogp",
    "motorsport",
    "nascar",
    "indycar",
    "pit stop",
    "pole position",
    "qualifying",
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
    text = (
        text or ""
    ).lower()

    return any(
        term in text
        for term in NON_FOOTBALL_TERMS
    )


def contains_football_signal(text):
    text = (
        text or ""
    ).lower()

    return any(
        term in text
        for term in FOOTBALL_TERMS
    )


def contains_transfer_signal(text):
    text = (
        text or ""
    ).lower()

    return any(
        term in text
        for term in TRANSFER_TERMS
    )


def is_football_article(article):
    text = " ".join(
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
                "source",
                "",
            ),
        ]
    ).lower()

    if contains_non_football(
        text
    ):
        return False

    return contains_football_signal(
        text
    )


def is_football_transfer(article):
    text = " ".join(
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
                "source",
                "",
            ),
        ]
    ).lower()

    if contains_non_football(
        text
    ):
        return False

    return (
        contains_football_signal(
            text
        )
        and contains_transfer_signal(
            text
        )
    )


# =========================================================
# RSS
# =========================================================

def clean_html(text):
    if not text:
        return ""

    text = re.sub(
        r"<script.*?</script>",
        " ",
        text,
        flags=re.I | re.S,
    )

    text = re.sub(
        r"<style.*?</style>",
        " ",
        text,
        flags=re.I | re.S,
    )

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


def extract_rss_image(entry):
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

        enclosures = entry.get(
            "enclosures"
        )

        if enclosures:
            for item in enclosures:
                url = (
                    item.get("href")
                    or item.get("url")
                )

                if url:
                    return url

    except Exception:
        pass

    return ""


def parse_feed(
    source,
    url,
    news_type,
):
    articles = []

    try:
        feed = feedparser.parse(
            url
        )

        for entry in feed.entries[:12]:

            title = normalize_text(
                entry.get(
                    "title",
                    "",
                )
            )

            link = canonicalize_url(
                entry.get(
                    "link",
                    "",
                )
            )

            summary = clean_html(
                entry.get(
                    "summary",
                    "",
                )
                or entry.get(
                    "description",
                    "",
                )
            )

            if not title or not link:
                continue

            image = extract_rss_image(
                entry
            )

            articles.append(
                {
                    "title": title,
                    "title_original": title,
                    "summary": summary,
                    "link": link,
                    "source": source,
                    "image": image,
                    "news_type": news_type,
                }
            )

    except Exception as e:
        log(
            f"RSS ERROR [{source}]: {e}"
        )

    return articles


# =========================================================
# DUPLICATE SYSTEM
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


def mark_article_sent(
    article
):
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

    if link:
        sent_links.add(
            link
        )

    title_key = normalize_title(
        title
    )

    if title_key:
        sent_title_keys.add(
            title_key
        )

    content_key = make_content_key(
        title
    )

    if content_key:
        sent_content_keys.add(
            content_key
        )

    save_state()


# =========================================================
# COLLECT ARTICLES
# =========================================================

def collect_articles():

    normal = []
    transfers = []

    # -------------------------
    # Normal football
    # -------------------------

    for source, url in FOOTBALL_FEEDS:

        items = parse_feed(
            source,
            url,
            "NORMAL",
        )

        for article in items:

            if not is_football_article(
                article
            ):
                continue

            if is_duplicate_article(
                article["link"],
                article["title"],
                source,
                normal,
            ):
                continue

            normal.append(
                article
            )

            if (
                len(normal)
                >= MAX_ARTICLES_PER_RUN
            ):
                break

        if (
            len(normal)
            >= MAX_ARTICLES_PER_RUN
        ):
            break

    # -------------------------
    # Transfers
    # -------------------------

    for source, url in TRANSFER_FEEDS:

        items = parse_feed(
            source,
            url,
            "TRANSFER",
        )

        for article in items:

            if not is_football_transfer(
                article
            ):
                continue

            if is_duplicate_article(
                article["link"],
                article["title"],
                source,
                transfers,
            ):
                continue

            transfers.append(
                article
            )

            if (
                len(transfers)
                >= MAX_TRANSFER_ARTICLES_PER_RUN
            ):
                break

        if (
            len(transfers)
            >= MAX_TRANSFER_ARTICLES_PER_RUN
        ):
            break

    return normal, transfers


# =========================================================
# AI RATE LIMIT
# =========================================================

def ai_available():
    return (
        time_module.time()
        >= ai_cooldown_until
    )


def activate_ai_cooldown(
    error_text
):
    global ai_cooldown_until

    seconds = (
        AI_COOLDOWN_DEFAULT
    )

    match = re.search(
        r"try again in\s+(\d+)m",
        error_text,
        flags=re.I,
    )

    if match:
        seconds = (
            int(
                match.group(1)
            )
            * 60
        ) + 30

    ai_cooldown_until = (
        time_module.time()
        + seconds
    )

    log(
        f"AI COOLDOWN: {seconds} seconds"
    )


# =========================================================
# AI PROMPT
# =========================================================

def build_ai_prompt(
    articles
):
    payload = []

    for index, article in enumerate(
        articles
    ):
        payload.append(
            {
                "id": index,
                "type": article.get(
                    "news_type",
                    "NORMAL",
                ),
                "source": article.get(
                    "source",
                    "",
                ),
                "title": article.get(
                    "title_original",
                    article.get(
                        "title",
                        "",
                    ),
                ),
                "summary": article.get(
                    "summary",
                    "",
                ),
            }
        )

    return f"""
تو سردبیر حرفه‌ای یک کانال خبری فوتبال فارسی هستی.

فقط اخبار فوتبال را پردازش کن.

اخبار Formula 1، F1، MotoGP،
بسکتبال، تنیس، NFL، NBA،
NHL، MLB و سایر ورزش‌ها را رد کن.

برای هر خبر:

TITLE:
یک تیتر کوتاه، طبیعی و کاملاً فارسی بنویس.

SUMMARY:
خلاصه‌ای کوتاه و کاملاً فارسی در یک یا دو جمله بنویس.

SPORT:
فقط FOOTBALL یا REJECT

IMPORTANCE:
URGENT یا IMPORTANT یا NORMAL

URGENT:
فقط خبرهای واقعاً فوری و بسیار مهم.

IMPORTANT:
خبر مهم فوتبال.

NORMAL:
خبر عادی فوتبال.

برای نقل‌وانتقالات:

IS_TRANSFER:
YES یا NO

STATUS:
OFFICIAL
AGREEMENT
NEGOTIATION
RUMOR
DENIED

TYPE:
PERMANENT
LOAN
FREE
EXTENSION
RETURN
UNKNOWN

PLAYER
FROM
TO
FEE
CONTRACT

تعریف وضعیت‌ها:

OFFICIAL:
باشگاه یا منبع رسمی انتقال را اعلام کرده.

AGREEMENT:
توافق انجام شده اما هنوز رسمی نشده.

NEGOTIATION:
مذاکرات در جریان است.

RUMOR:
شایعه یا گزارش غیرقطعی.

DENIED:
خبر یا شایعه تکذیب شده.

TITLE و SUMMARY حتماً فارسی باشند.

نام بازیکنان و باشگاه‌ها را به شکل رایج فارسی بنویس.

خروجی فقط JSON معتبر باشد.
هیچ متن دیگری خارج JSON ننویس.

ساختار:

{{
  "items": [
    {{
      "id": 0,
      "sport": "FOOTBALL",
      "importance": "NORMAL",
      "is_transfer": "NO",
      "status": "RUMOR",
      "type": "UNKNOWN",
      "player": "",
      "from": "",
      "to": "",
      "fee": "",
      "contract": "",
      "title": "",
      "summary": ""
    }}
  ]
}}

اخبار:

{json.dumps(
    payload,
    ensure_ascii=False
)}
"""


def extract_json(text):
    if not text:
        return None

    text = text.strip()

    try:
        return json.loads(
            text
        )
    except Exception:
        pass

    match = re.search(
        r"\{.*\}",
        text,
        flags=re.S,
    )

    if not match:
        return None

    try:
        return json.loads(
            match.group(0)
        )
    except Exception:
        return None


# =========================================================
# AI BATCH
# =========================================================

async def classify_batch(
    articles
):
    if not articles:
        return []

    if not client:
        log(
            "OPENAI KEY NOT FOUND"
        )
        return []

    if not ai_available():

        remaining = int(
            max(
                0,
                ai_cooldown_until
                - time_module.time(),
            )
        )

        log(
            f"AI COOLDOWN ACTIVE: "
            f"{remaining}s"
        )

        return []

    batch = articles[
        :MAX_AI_BATCH
    ]

    prompt = build_ai_prompt(
        batch
    )

    try:

        response = (
            await client.responses.create(
                model=AI_MODEL,
                input=prompt,
            )
        )

        text = getattr(
            response,
            "output_text",
            "",
        )

        data = extract_json(
            text
        )

        if not data:
            log(
                "AI JSON ERROR"
            )
            return []

        items = data.get(
            "items",
            [],
        )

        if not isinstance(
            items,
            list,
        ):
            return []

        results = []

        for item in items:

            try:
                index = int(
                    item.get(
                        "id"
                    )
                )
            except Exception:
                continue

            if (
                index < 0
                or index >= len(batch)
            ):
                continue

            article = dict(
                batch[index]
            )

            article["sport"] = str(
                item.get(
                    "sport",
                    "REJECT",
                )
            ).upper().strip()

            article["importance"] = str(
                item.get(
                    "importance",
                    "NORMAL",
                )
            ).upper().strip()

            article["is_transfer"] = str(
                item.get(
                    "is_transfer",
                    "NO",
                )
            ).upper().strip()

            article["status"] = str(
                item.get(
                    "status",
                    "RUMOR",
                )
            ).upper().strip()

            article["type"] = str(
                item.get(
                    "type",
                    "UNKNOWN",
                )
            ).upper().strip()

            article["player"] = normalize_text(
                item.get(
                    "player",
                    "",
                )
            )

            article["from"] = normalize_text(
                item.get(
                    "from",
                    "",
                )
            )

            article["to"] = normalize_text(
                item.get(
                    "to",
                    "",
                )
            )

            article["fee"] = normalize_text(
                item.get(
                    "fee",
                    "",
                )
            )

            article["contract"] = normalize_text(
                item.get(
                    "contract",
                    "",
                )
            )

            article["title"] = normalize_text(
                item.get(
                    "title",
                    "",
                )
            )

            article["summary"] = normalize_text(
                item.get(
                    "summary",
                    "",
                )
            )

            results.append(
                article
            )

        return results

    except Exception as e:

        error_text = str(e)

        log(
            f"AI ERROR: {error_text}"
        )

        if (
            "429" in error_text
            or "rate_limit" in error_text.lower()
            or "RPD" in error_text
        ):
            activate_ai_cooldown(
                error_text
            )

        return []


# =========================================================
# VALIDATION
# =========================================================

def validate_ai_article(
    article
):
    if not article:
        return False

    if (
        article.get("sport")
        != "FOOTBALL"
    ):
        return False

    title = normalize_text(
        article.get(
            "title",
            "",
        )
    )

    summary = normalize_text(
        article.get(
            "summary",
            "",
        )
    )

    if not title or not summary:
        return False

    combined = (
        title
        + " "
        + summary
    )

    if contains_non_football(
        combined
    ):
        return False

    if not is_persian_text(
        combined
    ):
        log(
            f"PERSIAN BLOCKED: {title}"
        )
        return False

    if contains_too_much_english(
        combined
    ):
        log(
            f"ENGLISH BLOCKED: {title}"
        )
        return False

    return True


# =========================================================
# POST LABELS
# =========================================================

def importance_prefix(
    importance
):
    if importance == "URGENT":
        return "🚨 خبر فوری"

    if importance == "IMPORTANT":
        return "🔥 خبر مهم"

    return "⚽ خبر فوتبال"


def transfer_status_label(
    status
):
    labels = {
        "OFFICIAL": "✅ انتقال رسمی",
        "AGREEMENT": "📝 توافق",
        "NEGOTIATION": "🤝 مذاکرات",
        "RUMOR": "🟡 شایعه",
        "DENIED": "❌ تکذیب",
    }

    return labels.get(
        status,
        "⚽ نقل‌وانتقالات",
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
    }

    return labels.get(
        transfer_type,
        "",
    )


# =========================================================
# BUILD POST
# =========================================================

def build_post(
    article
):
    title = escape(
        normalize_text(
            article.get(
                "title",
                "",
            )
        )
    )

    summary = escape(
        normalize_text(
            article.get(
                "summary",
                "",
            )
        )
    )

    source = escape(
        normalize_text(
            article.get(
                "source",
                "",
            )
        )
    )

    if (
        article.get(
            "is_transfer"
        )
        == "YES"
    ):

        status = transfer_status_label(
            article.get(
                "status",
                "RUMOR",
            )
        )

        transfer_type = (
            transfer_type_label(
                article.get(
                    "type",
                    "UNKNOWN",
                )
            )
        )

        player = escape(
            normalize_text(
                article.get(
                    "player",
                    "",
                )
            )
        )

        from_club = escape(
            normalize_text(
                article.get(
                    "from",
                    "",
                )
            )
        )

        to_club = escape(
            normalize_text(
                article.get(
                    "to",
                    "",
                )
            )
        )

        fee = escape(
            normalize_text(
                article.get(
                    "fee",
                    "",
                )
            )
        )

        contract = escape(
            normalize_text(
                article.get(
                    "contract",
                    "",
                )
            )
        )

        lines = [
            f"<b>{status}</b>",
            "",
            f"<b>{title}</b>",
            "",
            summary,
        ]

        if player:
            lines.append(
                f"\n👤 <b>بازیکن:</b> {player}"
            )

        if from_club:
            lines.append(
                f"🏟 <b>از:</b> {from_club}"
            )

        if to_club:
            lines.append(
                f"➡️ <b>به:</b> {to_club}"
            )

        if fee:
            lines.append(
                f"💰 <b>مبلغ:</b> {fee}"
            )

        if contract:
            lines.append(
                f"📄 <b>قرارداد:</b> {contract}"
            )

        if transfer_type:
            lines.append(
                f"🔄 <b>نوع:</b> {escape(transfer_type)}"
            )

        lines.extend(
            [
                "",
                f"📰 منبع: {source}",
                "",
                "⚡ Vexa Football",
            ]
        )

        return "\n".join(
            lines
        )

    prefix = importance_prefix(
        article.get(
            "importance",
            "NORMAL",
        )
    )

    return (
        f"<b>{prefix}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"{summary}\n\n"
        f"📰 منبع: {source}\n\n"
        f"⚡ Vexa Football"
    )


# =========================================================
# TELEGRAM POST
# =========================================================

async def post_article(
    bot,
    article,
    target=CHANNEL_USERNAME,
):
    if not validate_ai_article(
        article
    ):
        return False

    text = build_post(
        article
    )

    if not is_persian_text(
        text
    ):
        log(
            "FINAL PERSIAN CHECK BLOCKED"
        )
        return False

    try:

        image = article.get(
            "image",
            "",
        )

        if image:
            try:

                await bot.send_photo(
                    chat_id=target,
                    photo=image,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                )

                return True

            except Exception as e:
                log(
                    f"IMAGE FAILED: {e}"
                )

        await bot.send_message(
            chat_id=target,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

        return True

    except Exception as e:
        log(
            f"TELEGRAM POST ERROR: {e}"
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
            link
            and link in sent_links
        ):
            log(
                f"DUPLICATE BLOCKED: {title}"
            )
            continue

        if (
            title_key
            and title_key in sent_title_keys
        ):
            log(
                f"DUPLICATE TITLE: {title}"
            )
            continue

        if (
            content_key
            and content_key in sent_content_keys
        ):
            log(
                f"DUPLICATE CONTENT: {title}"
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

            posted += 1

            log(
                f"POSTED: {title}"
            )

        await asyncio.sleep(
            1.2
        )

    return posted


# =========================================================
# NEWS PIPELINE
# =========================================================

async def run_news_pipeline(
    bot,
    force=False,
):
    log(
        "NEWS CHECK START"
    )

    normal, transfers = (
        collect_articles()
    )

    log(
        "COLLECTED: "
        f"normal={len(normal)} "
        f"transfers={len(transfers)}"
    )

    candidates = (
        normal[:MAX_ARTICLES_PER_RUN]
        + transfers[
            :MAX_TRANSFER_ARTICLES_PER_RUN
        ]
    )

    if not candidates:
        log(
            "NO NEW ARTICLES"
        )
        return 0

    candidates = candidates[
        :MAX_AI_BATCH
    ]

    processed = await classify_batch(
        candidates
    )

    if not processed:
        log(
            "NO AI RESULTS"
        )
        return 0

    valid = []

    for article in processed:

        if not validate_ai_article(
            article
        ):
            continue

        combined = (
            article.get(
                "title",
                "",
            )
            + " "
            + article.get(
                "summary",
                "",
            )
        )

        if contains_non_football(
            combined
        ):
            continue

        valid.append(
            article
        )

    if not valid:
        log(
            "NO VALID FOOTBALL ARTICLES"
        )
        return 0

    posted = await publish_news(
        valid,
        bot,
    )

    log(
        f"NEWS CHECK END: posted={posted}"
    )

    return posted


# =========================================================
# KEYBOARD
# =========================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [
            "📰 اخبار جدید",
            "🔥 اخبار مهم",
        ],
        [
            "🔄 نقل‌وانتقالات",
            "🧪 تست پست",
        ],
        [
            "❌ بستن منو",
        ],
    ],
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
        "سلام داداش 👋\n\n"
        "من Vexa هستم؛ ربات اخبار فوتبال و نقل‌وانتقالات ⚽🔥\n\n"
        "از منوی پایین می‌تونی اخبار رو بررسی کنی.",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "⚡ دستورات Vexa\n\n"
        "/start — شروع\n"
        "/help — راهنما\n"
        "/news — بررسی اخبار جدید\n"
        "/important — اخبار مهم\n"
        "/transfers — نقل‌وانتقالات\n"
        "/testpost — تست انتشار\n",
        reply_markup=MAIN_KEYBOARD,
    )


async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔎 دارم اخبار جدید فوتبال رو بررسی می‌کنم..."
    )

    try:

        posted = await run_news_pipeline(
            context.bot,
            force=True,
        )

        await update.message.reply_text(
            f"✅ بررسی انجام شد.\n"
            f"📤 تعداد پست‌های منتشرشده: {posted}",
            reply_markup=MAIN_KEYBOARD,
        )

    except Exception as e:

        log(
            f"/news ERROR: {e}"
        )

        await update.message.reply_text(
            "❌ هنگام بررسی اخبار مشکلی پیش آمد.",
            reply_markup=MAIN_KEYBOARD,
        )


async def important_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔥 دارم اخبار جدید رو بررسی می‌کنم..."
    )

    try:

        posted = await run_news_pipeline(
            context.bot,
            force=True,
        )

        await update.message.reply_text(
            f"✅ بررسی انجام شد.\n"
            f"📤 {posted} خبر جدید منتشر شد.",
            reply_markup=MAIN_KEYBOARD,
        )

    except Exception as e:

        log(
            f"/important ERROR: {e}"
        )

        await update.message.reply_text(
            "❌ مشکلی پیش آمد.",
            reply_markup=MAIN_KEYBOARD,
        )


async def transfers_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔄 دارم نقل‌وانتقالات جدید رو بررسی می‌کنم..."
    )

    try:

        posted = await run_news_pipeline(
            context.bot,
            force=True,
        )

        await update.message.reply_text(
            f"✅ بررسی انجام شد.\n"
            f"📤 {posted} خبر جدید منتشر شد.",
            reply_markup=MAIN_KEYBOARD,
        )

    except Exception as e:

        log(
            f"/transfers ERROR: {e}"
        )

        await update.message.reply_text(
            "❌ مشکلی پیش آمد.",
            reply_markup=MAIN_KEYBOARD,
        )


async def testpost_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    article = {
        "title_original": "تست انتشار Vexa",
        "title": "تست انتشار Vexa",
        "summary": (
            "این یک پیام آزمایشی برای بررسی "
            "عملکرد انتشار ربات Vexa است."
        ),
        "source": "Vexa",
        "link": "",
        "image": "",
        "news_type": "NORMAL",
        "sport": "FOOTBALL",
        "importance": "NORMAL",
        "is_transfer": "NO",
    }

    try:

        success = await post_article(
            context.bot,
            article,
            CHANNEL_USERNAME,
        )

        if success:

            await update.message.reply_text(
                "✅ تست پست با موفقیت ارسال شد.",
                reply_markup=MAIN_KEYBOARD,
            )

        else:

            await update.message.reply_text(
                "❌ تست پست ارسال نشد.",
                reply_markup=MAIN_KEYBOARD,
            )

    except Exception as e:

        log(
            f"/testpost ERROR: {e}"
        )

        await update.message.reply_text(
            "❌ خطا در تست پست.",
            reply_markup=MAIN_KEYBOARD,
        )


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    text = (
        update.message.text
        or ""
    ).strip()

    if text == "❌ بستن منو":

        await update.message.reply_text(
            "منو بسته شد 👌",
            reply_markup=ReplyKeyboardRemove(),
        )

        return

    if text == "📰 اخبار جدید":
        await news_command(
            update,
            context,
        )
        return

    if text == "🔥 اخبار مهم":
        await important_command(
            update,
            context,
        )
        return

    if text == "🔄 نقل‌وانتقالات":
        await transfers_command(
            update,
            context,
        )
        return

    if text == "🧪 تست پست":
        await testpost_command(
            update,
            context,
        )
        return


# =========================================================
# AUTOMATIC NEWS JOB
# =========================================================

async def automatic_news_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    try:

        await run_news_pipeline(
            context.bot
        )

    except Exception as e:

        log(
            f"AUTO JOB ERROR: {e}"
        )


# =========================================================
# DAILY DIGEST
# =========================================================

async def daily_digest_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    try:

        await context.bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=(
                "🌙 <b>خلاصه روزانه Vexa</b>\n\n"
                "امروز هم Vexa اخبار فوتبال و "
                "نقل‌وانتقالات جدید را دنبال کرد. ⚽🔥\n\n"
                "⚡ Vexa Football"
            ),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:

        log(
            f"DIGEST ERROR: {e}"
        )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application,
):
    log(
        "POST INIT"
    )

    application.job_queue.run_once(
        automatic_news_job,
        when=FIRST_NEWS_DELAY,
        name="first_news_check",
    )

    application.job_queue.run_repeating(
        automatic_news_job,
        interval=NEWS_INTERVAL,
        first=NEWS_INTERVAL,
        name="automatic_news",
    )

    application.job_queue.run_daily(
        daily_digest_job,
        time=time(
            hour=DIGEST_HOUR,
            minute=DIGEST_MINUTE,
            second=0,
        ),
        name="daily_digest",
    )

    log(
        "SCHEDULER READY"
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

    # =====================================================
    # مهم:
    # Telegram/PTB به tzinfo واقعی نیاز دارد،
    # نه رشته.
    # =====================================================

    timezone = ZoneInfo(
        TIMEZONE
    )

    defaults = Defaults(
        tzinfo=timezone
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .post_init(post_init)
        .build()
    )

    # =====================================================
    # COMMAND HANDLERS
    # =====================================================

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

    # =====================================================
    # TEXT HANDLER
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler,
        )
    )

    log(
        "VEXA STARTING..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
