import json
import os
import re
import hashlib
import requests
import feedparser

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlparse


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


def load_json(path):
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def clean_html(text):

    text = re.sub(r"<[^>]+>", " ", text or "")
    text = unescape(text)

    return re.sub(r"\s+", " ", text).strip()


def get_image(entry):

    media = entry.get("media_content", [])

    if media:
        return media[0].get("url", "")

    thumbnails = entry.get("media_thumbnail", [])

    if thumbnails:
        return thumbnails[0].get("url", "")

    enclosures = entry.get("enclosures", [])

    for enclosure in enclosures:
        if enclosure.get("type", "").startswith("image/"):
            return enclosure.get("href", "")

    return ""


def get_date(entry):

    try:
        date = entry.get("published") or entry.get("updated")

        if date:
            parsed = parsedate_to_datetime(date)

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(
                timezone.utc
            ).isoformat()

    except Exception:
        pass

    return datetime.now(timezone.utc).isoformat()


def categorize(title, description, categories):

    content = (
        title + " " + description
    ).lower()

    for category, keywords in categories.items():

        for keyword in keywords:

            if keyword.lower() in content:
                return category

    return None


def generate_id(url):

    return hashlib.sha256(
        url.encode("utf-8")
    ).hexdigest()[:16]


def fetch_feed(source):

    try:

        response = requests.get(
            source["url"],
            timeout=20,
            headers={
                "User-Agent": "UmmahNewsAggregator/1.0"
            }
        )

        response.raise_for_status()

        feed = feedparser.parse(response.content)

        articles = []

        for entry in feed.entries:

            title = clean_html(
                entry.get("title", "")
            )

            description = clean_html(
                entry.get("summary", "")
            )

            url = entry.get("link", "")

            if not title or not url:
                continue

            category = categorize(
                title,
                description,
                CATEGORIES
            )

            if not category:
                continue

            image = get_image(entry)

            article = {

                "id": generate_id(url),

                "title": title,

                "description": description[:600],

                "image": image,

                "category": category,

                "source": source["name"],

                "language": source["language"],

                "country": source["country"],

                "publishedAt": get_date(entry),

                "originalUrl": url

            }

            articles.append(article)

        print(
            source["name"],
            ":", len(articles), "berita"
        )

        return articles

    except Exception as error:

        print(
            "Gagal mengambil",
            source["name"],
            error
        )

        return []


def main():

    global CATEGORIES

    sources = load_json(SOURCES_FILE)

    CATEGORIES = load_json(CATEGORIES_FILE)

    all_articles = []

    for source in sources:

        articles = fetch_feed(source)

        all_articles.extend(articles)

    # Menghapus duplikat berdasarkan URL

    unique = {}

    for article in all_articles:

        unique[article["originalUrl"]] = article

    articles = list(unique.values())

    # Urutkan berdasarkan tanggal terbaru

    articles.sort(
        key=lambda x: x["publishedAt"],
        reverse=True
    )

    # Simpan maksimal 500 berita

    articles = articles[:500]

    result = {

        "status": "success",

        "lastUpdated": datetime.now(
            timezone.utc
        ).isoformat(),

        "total": len(articles),

        "articles": articles

    }

    os.makedirs(
        os.path.dirname(OUTPUT_FILE),
        exist_ok=True
    )

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            result,
            file,
            ensure_ascii=False,
            indent=2
        )

    print(
        "Berhasil menyimpan",
        len(articles),
        "berita"
    )


if __name__ == "__main__":

    main()
