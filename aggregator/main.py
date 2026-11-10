
import os
import re
import json
import html
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import requests
import feedparser


BASE_DIR = os.path.dirname(os.path.dirname(__file__))

SOURCES_FILE = os.path.join(
    BASE_DIR, "aggregator", "sources.json"
)

CATEGORIES_FILE = os.path.join(
    BASE_DIR, "aggregator", "categories.json"
)

OUTPUT_FILE = os.path.join(
    BASE_DIR, "data", "news.json"
)

MAX_ARTICLES = 500
MAX_AGE_DAYS = 90
TIMEOUT = 25

HEADERS = {
    "User-Agent": "KabarUmmahNewsBot/1.0 (+https://github.com/sscorpfam/kabar-ummah)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as error:
        logging.error("Gagal membaca %s: %s", path, error)
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temp_path, path)


def clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def valid_http_url(url):
    if not url:
        return False

    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.netloc)
    except Exception:
        return False


def normalize_url(url):
    if not valid_http_url(url):
        return ""

    parts = urlsplit(url.strip())

    # Fragment tidak diperlukan untuk identitas artikel.
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        parts.query,
        ""
    ))


def generate_id(url):
    return hashlib.sha256(
        url.encode("utf-8")
    ).hexdigest()[:16]


def get_published_date(entry):
    parsed = entry.get("published_parsed")

    if not parsed:
        parsed = entry.get("updated_parsed")

    if parsed:
        from time import mktime
        from calendar import timegm

        return datetime.fromtimestamp(
            timegm(parsed),
            timezone.utc
        ).isoformat()

    for field in ("published", "updated", "pubDate"):
        value = entry.get(field)

        if not value:
            continue

        try:
            date = parsedate_to_datetime(value)

            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)

            return date.astimezone(
                timezone.utc
            ).isoformat()

        except Exception:
            continue

    return datetime.now(timezone.utc).isoformat()


def extract_image(entry):
    candidates = []

    # Media RSS.
    for field in ("media_content", "media_thumbnail"):
        values = entry.get(field, [])

        if isinstance(values, dict):
            values = [values]

        for item in values:
            if isinstance(item, dict):
                candidates.append(item.get("url", ""))

    # Enclosure RSS.
    for item in entry.get("enclosures", []):
        if isinstance(item, dict):
            candidates.append(item.get("href", ""))

    # Link dengan rel=enclosure.
    for item in entry.get("links", []):
        if isinstance(item, dict):
            if item.get("rel") == "enclosure":
                candidates.append(item.get("href", ""))

    # Gambar di HTML content.
    html_fields = []

    for item in entry.get("content", []):
        if isinstance(item, dict):
            html_fields.append(item.get("value", ""))

    html_fields.append(entry.get("summary", ""))

    for content in html_fields:
        match = re.search(
            r'<img[^>]+src=["\']([^"\']+)["\']',
            content or "",
            re.IGNORECASE
        )

        if match:
            candidates.append(html.unescape(match.group(1)))

    for candidate in candidates:
        if valid_http_url(candidate):
            return candidate

    return ""



def categorize(title, description, categories):

    text = clean_text(
        title + " " + description
    ).lower()

    scores = {}
    matched_keywords = {}

    for category, keywords in categories.items():

        score = 0
        matches = []

        for keyword in keywords:

            keyword = keyword.lower().strip()

            if not keyword:
                continue

            # Regex untuk mencocokkan kata/frasa lengkap.
            # Tidak boleh cocok di tengah kata lain.
            pattern = (
                r"(?<![a-z0-9])"
                + re.escape(keyword)
                + r"(?![a-z0-9])"
            )

            if re.search(pattern, text, re.IGNORECASE):

                matches.append(keyword)

                # Frasa yang terdiri dari beberapa kata
                # mendapat bobot lebih tinggi.
                if len(keyword.split()) >= 2:
                    score += 3
                else:
                    score += 2

        if matches:
            scores[category] = score
            matched_keywords[category] = matches

    if not scores:
        return None, []

    # Pilih kategori dengan skor tertinggi.
    best_category = max(
        scores,
        key=scores.get
    )

    # Tidak ada kecocokan berarti berita ditolak.
    if scores[best_category] < 2:
        return None, []

    # Urutkan kategori tambahan berdasarkan skor.
    matched_categories = sorted(
        matched_keywords.keys(),
        key=lambda category: scores[category],
        reverse=True
    )

    return best_category, matched_categories



def fetch_source(source, categories):
    result = {
        "name": source.get("name", "Unknown"),
        "status": "error",
        "raw": 0,
        "accepted": 0,
        "filtered": 0,
        "error": None
    }

    try:
        response = requests.get(
            source["url"],
            headers=HEADERS,
            timeout=TIMEOUT
        )

        response.raise_for_status()

        feed = feedparser.parse(response.content)

        entries = feed.entries

        result["raw"] = len(entries)

        if not entries and feed.bozo:
            raise ValueError(
                "RSS tidak dapat dibaca: "
                + str(feed.bozo_exception)
            )

        result["status"] = "success"

        articles = []

        for entry in entries:

            title = clean_text(
                entry.get("title", "")
            )

            description = clean_text(
                entry.get("summary", "")
            )

            if not description:
                description = clean_text(
                    entry.get("description", "")
                )

            url = normalize_url(
                entry.get("link", "")
            )

            if not title or not url:
                result["filtered"] += 1
                continue

            category, tags = categorize(
                title,
                description,
                categories
            )

            if not category:
                result["filtered"] += 1
                continue

            article = {
                "id": generate_id(url),
                "title": title,
                "description": description[:800],
                "image": extract_image(entry),
                "category": category,
                "tags": tags,
                "source": source["name"],
                "language": source.get("language", "id"),
                "country": source.get("country", ""),
                "publishedAt": get_published_date(entry),
                "originalUrl": url
            }

            articles.append(article)

        result["accepted"] = len(articles)

        logging.info(
            "%s | RSS: %d | Lolos: %d | Disaring: %d",
            result["name"],
            result["raw"],
            result["accepted"],
            result["filtered"]
        )

        return articles, result

    except Exception as error:

        result["error"] = str(error)

        logging.error(
            "%s gagal: %s",
            result["name"],
            error
        )

        return [], result


def load_previous_articles():
    previous = load_json(
        OUTPUT_FILE,
        default={}
    )

    if not isinstance(previous, dict):
        return []

    articles = previous.get("articles", [])

    return articles if isinstance(articles, list) else []


def merge_articles(new_articles, old_articles):
    merged = {}

    # Berita lama dimasukkan terlebih dahulu.
    for article in old_articles:
        url = normalize_url(
            article.get("originalUrl", "")
        )

        if url:
            merged[url] = article

    # Berita terbaru menggantikan data lama
    # jika URL-nya sama.
    for article in new_articles:
        url = normalize_url(
            article.get("originalUrl", "")
        )

        if url:
            merged[url] = article

    cutoff = datetime.now(timezone.utc) - timedelta(
        days=MAX_AGE_DAYS
    )

    valid_articles = []

    for article in merged.values():

        try:
            date = datetime.fromisoformat(
                article["publishedAt"].replace("Z", "+00:00")
            )

            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)

            if date < cutoff:
                continue

        except Exception:
            pass

        valid_articles.append(article)

    valid_articles.sort(
        key=lambda item: item.get("publishedAt", ""),
        reverse=True
    )

    return valid_articles[:MAX_ARTICLES]


def main():

    sources = load_json(
        SOURCES_FILE,
        default=[]
    )

    categories = load_json(
        CATEGORIES_FILE,
        default={}
    )

    if not sources:
        raise RuntimeError("Daftar RSS kosong.")

    if not categories:
        raise RuntimeError("Kategori berita kosong.")

    previous_articles = load_previous_articles()

    all_articles = []
    source_stats = []

    for source in sources:

        articles, stats = fetch_source(
            source,
            categories
        )

        all_articles.extend(articles)
        source_stats.append(stats)

    successful = sum(
        1 for item in source_stats
        if item["status"] == "success"
    )

    failed = len(source_stats) - successful

    final_articles = merge_articles(
        all_articles,
        previous_articles
    )

    if successful == 0:
        status = "error"
    elif failed > 0:
        status = "partial"
    else:
        status = "success"

    result = {
        "status": status,
        "lastUpdated": datetime.now(
            timezone.utc
        ).isoformat(),
        "total": len(final_articles),
        "newArticles": len(all_articles),
        "sourcesChecked": len(sources),
        "sourcesSucceeded": successful,
        "sourcesFailed": failed,
        "sourceStats": source_stats,
        "articles": final_articles
    }

    save_json(
        OUTPUT_FILE,
        result
    )

    logging.info("--------------------------------")
    logging.info("Status: %s", status)
    logging.info("Sumber berhasil: %d/%d", successful, len(sources))
    logging.info("Berita baru lolos filter: %d", len(all_articles))
    logging.info("Total berita tersimpan: %d", len(final_articles))
    logging.info("--------------------------------")

    # Jika semua sumber gagal, workflow dibuat gagal
    # agar masalah terlihat di GitHub Actions.
    if successful == 0:
        raise RuntimeError(
            "Semua sumber RSS gagal diakses."
        )


if __name__ == "__main__":
    main()
