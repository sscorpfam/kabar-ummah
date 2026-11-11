
import os
import re
import json
import html
import hashlib
import logging

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, urljoin
from html.parser import HTMLParser
from difflib import SequenceMatcher

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
IMAGE_TIMEOUT = 10

HEADERS = {
    "User-Agent": (
        "KabarUmmahNewsBot/1.0 "
        "(+https://github.com/sscorpfam/kabar-ummah)"
    ),
    "Accept": (
        "application/rss+xml, application/xml, "
        "text/xml, */*"
    )
}


logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)


# =========================================================
# HTML IMAGE PARSER
# =========================================================

class ImageHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):

        attrs = dict(attrs)

        tag = tag.lower()

        # Metadata gambar.
        if tag == "meta":

            key = (
                attrs.get("property", "")
                or attrs.get("name", "")
                or attrs.get("itemprop", "")
            ).lower()

            if key in (
                "og:image",
                "og:image:url",
                "twitter:image",
                "twitter:image:src",
                "image"
            ):

                self.images.append(
                    attrs.get("content", "")
                )

        # Tag gambar.
        elif tag == "img":

            for key in (
                "src",
                "data-src",
                "data-original",
                "data-lazy-src",
                "data-original-src"
            ):

                value = attrs.get(key)

                if value:
                    self.images.append(value)

            # Responsive image.
            srcset = (
                attrs.get("srcset", "")
                or attrs.get("data-srcset", "")
            )

            if srcset:

                first = srcset.split(",")[0].strip()

                if first:
                    self.images.append(
                        first.split()[0]
                    )

        # Gambar dari tag picture/source.
        elif tag == "source":

            srcset = (
                attrs.get("srcset", "")
                or attrs.get("data-srcset", "")
            )

            if srcset:

                first = srcset.split(",")[0].strip()

                if first:
                    self.images.append(
                        first.split()[0]
                    )

        # Link image_src.
        elif tag == "link":

            rel = attrs.get("rel", "").lower()

            if "image_src" in rel:

                self.images.append(
                    attrs.get("href", "")
                )


# =========================================================
# JSON FUNCTIONS
# =========================================================

def load_json(path, default=None):

    try:

        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    except Exception as error:

        logging.error(
            "Gagal membaca %s: %s",
            path,
            error
        )

        return default


def save_json(path, data):

    os.makedirs(
        os.path.dirname(path),
        exist_ok=True
    )

    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as file:

        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temp_path, path)


# =========================================================
# TEXT FUNCTIONS
# =========================================================

def clean_text(value):

    value = html.unescape(str(value or ""))

    value = re.sub(
        r"<[^>]+>",
        " ",
        value
    )

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


# =========================================================
# URL FUNCTIONS
# =========================================================

def valid_http_url(url):

    if not url:
        return False

    try:

        parts = urlsplit(url)

        return (
            parts.scheme in ("http", "https")
            and bool(parts.netloc)
        )

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


# =========================================================
# PUBLISHED DATE
# =========================================================

def get_published_date(entry):

    parsed = entry.get("published_parsed")

    if not parsed:
        parsed = entry.get("updated_parsed")

    if parsed:

        from calendar import timegm

        return datetime.fromtimestamp(
            timegm(parsed),
            timezone.utc
        ).isoformat()

    for field in (
        "published",
        "updated",
        "pubDate"
    ):

        value = entry.get(field)

        if not value:
            continue

        try:

            date = parsedate_to_datetime(value)

            if date.tzinfo is None:

                date = date.replace(
                    tzinfo=timezone.utc
                )

            return date.astimezone(
                timezone.utc
            ).isoformat()

        except Exception:
            continue

    return datetime.now(
        timezone.utc
    ).isoformat()


# =========================================================
# IMAGE URL VALIDATION
# =========================================================

def find_valid_image(candidates, base_url=""):

    for candidate in candidates:

        if not isinstance(candidate, str):
            continue

        candidate = html.unescape(
            candidate.strip()
        )

        if not candidate:
            continue

        # Abaikan placeholder kosong.
        if candidate.lower().startswith(
            ("data:", "javascript:")
        ):
            continue

        # URL protocol-relative.
        if candidate.startswith("//"):

            candidate = "https:" + candidate

        # URL relatif.
        elif base_url:

            candidate = urljoin(
                base_url,
                candidate
            )

        if valid_http_url(candidate):

            return candidate

    return ""


# =========================================================
# IMAGE EXTRACTION
# =========================================================

def extract_image(entry, article_url=""):

    candidates = []

    # -----------------------------------------------------
    # 1. MEDIA RSS
    # -----------------------------------------------------

    def collect_media(value):

        if isinstance(value, dict):

            image_url = value.get("url", "")

            if image_url:
                candidates.append(image_url)

            # Mendukung struktur media bertingkat.
            for nested in value.values():

                if isinstance(
                    nested,
                    (dict, list)
                ):

                    collect_media(nested)

        elif isinstance(value, list):

            for item in value:
                collect_media(item)

    for field in (
        "media_content",
        "media_thumbnail",
        "media_group"
    ):

        collect_media(
            entry.get(field, [])
        )

    # -----------------------------------------------------
    # 2. ENCLOSURE RSS
    # -----------------------------------------------------

    for item in entry.get("enclosures", []):

        if isinstance(item, dict):

            candidates.append(
                item.get("href", "")
                or item.get("url", "")
            )

    # -----------------------------------------------------
    # 3. LINK ENCLOSURE
    # -----------------------------------------------------

    for item in entry.get("links", []):

        if isinstance(item, dict):

            if item.get("rel") == "enclosure":

                candidates.append(
                    item.get("href", "")
                )

    # -----------------------------------------------------
    # 4. HTML CONTENT RSS
    # -----------------------------------------------------

    html_fields = []

    for item in entry.get("content", []):

        if isinstance(item, dict):

            html_fields.append(
                item.get("value", "")
            )

    for field in (
        "summary",
        "description"
    ):

        value = entry.get(field, "")

        if value:
            html_fields.append(value)

    for content in html_fields:

        if not content:
            continue

        parser = ImageHTMLParser()

        try:

            parser.feed(str(content))

            candidates.extend(
                parser.images
            )

        except Exception as error:

            logging.debug(
                "Gagal membaca HTML RSS: %s",
                error
            )

    # -----------------------------------------------------
    # 5. AMBIL GAMBAR DARI RSS
    # -----------------------------------------------------

    image_url = find_valid_image(
        candidates,
        article_url
    )

    if image_url:

        return image_url

    # -----------------------------------------------------
    # 6. FALLBACK KE HALAMAN ARTIKEL
    # -----------------------------------------------------

    if (
        not article_url
        or not valid_http_url(article_url)
    ):

        return ""

    try:

        response = requests.get(
            article_url,
            headers={
                **HEADERS,
                "Accept": (
                    "text/html,application/xhtml+xml,*/*"
                )
            },
            timeout=IMAGE_TIMEOUT,
            allow_redirects=True
        )

        response.raise_for_status()

        parser = ImageHTMLParser()

        parser.feed(response.text)

        image_url = find_valid_image(
            parser.images,
            response.url
        )

        if image_url:

            logging.info(
                "Gambar ditemukan dari halaman artikel: %s",
                article_url
            )

            return image_url

    except Exception as error:

        logging.warning(
            "Gagal mengambil gambar artikel %s: %s",
            article_url,
            error
        )

    return ""


# =========================================================
# NEWS CATEGORIZATION
# =========================================================

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

            if re.search(
                pattern,
                text,
                re.IGNORECASE
            ):

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


# =========================================================
# FETCH NEWS SOURCE
# =========================================================

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

        feed = feedparser.parse(
            response.content
        )

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

            # Ambil gambar RSS atau halaman artikel.
            image_url = extract_image(
                entry,
                url
            )

            article = {
                "id": generate_id(url),
                "title": title,
                "description": description[:800],
                "image": image_url,
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


# =========================================================
# LOAD PREVIOUS ARTICLES
# =========================================================

def load_previous_articles():

    previous = load_json(
        OUTPUT_FILE,
        default={}
    )

    if not isinstance(previous, dict):

        return []

    articles = previous.get(
        "articles",
        []
    )

    return (
        articles
        if isinstance(articles, list)
        else []
    )


# =========================================================
# NEWS DUPLICATE DETECTION
# =========================================================

# Rentang waktu maksimal untuk mendeteksi berita
# yang membahas peristiwa sama.
DUPLICATE_TIME_WINDOW_HOURS = 72


def normalize_title(title):
    """
    Membersihkan judul agar perbedaan tanda baca,
    huruf besar/kecil, dan spasi tidak memengaruhi
    pencocokan.
    """

    title = clean_text(title).lower()

    title = re.sub(
        r"[^a-z0-9\s]",
        " ",
        title
    )

    title = re.sub(
        r"\s+",
        " ",
        title
    ).strip()

    return title


def get_title_tokens(title):
    """
    Mengambil kata-kata penting dari judul.
    """

    stopwords = {
        "yang", "dan", "di", "ke", "dari", "untuk",
        "pada", "dengan", "ini", "itu", "dalam",
        "the", "a", "an", "of", "to", "in", "on",
        "at", "for", "with", "and", "is", "are",
        "as", "by", "from"
    }

    normalized = normalize_title(title)

    return {
        word
        for word in normalized.split()
        if len(word) > 2 and word not in stopwords
    }


def get_article_datetime(article):
    """
    Membaca tanggal publikasi berita.
    """

    value = article.get("publishedAt", "")

    if not value:
        return None

    try:

        date = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)

        return date.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def is_same_news(article_a, article_b):
    """
    Memeriksa apakah dua artikel kemungkinan
    membahas berita atau peristiwa yang sama.
    """

    title_a = normalize_title(
        article_a.get("title", "")
    )

    title_b = normalize_title(
        article_b.get("title", "")
    )

    if not title_a or not title_b:
        return False

    # Judul identik setelah normalisasi.
    if title_a == title_b:
        return True

    # Batasi perbandingan berita berdasarkan waktu.
    date_a = get_article_datetime(article_a)
    date_b = get_article_datetime(article_b)

    if date_a and date_b:

        difference = abs(
            (date_a - date_b).total_seconds()
        )

        if difference > DUPLICATE_TIME_WINDOW_HOURS * 3600:
            return False

    tokens_a = get_title_tokens(title_a)
    tokens_b = get_title_tokens(title_b)

    if not tokens_a or not tokens_b:
        return False

    common_tokens = tokens_a.intersection(tokens_b)

    # Minimal harus memiliki beberapa kata penting sama.
    if len(common_tokens) < 3:
        return False

    # Mengukur kesamaan urutan judul.
    sequence_score = SequenceMatcher(
        None,
        title_a,
        title_b
    ).ratio()

    # Mengukur kesamaan kumpulan kata.
    union_tokens = tokens_a.union(tokens_b)

    jaccard_score = (
        len(common_tokens) / len(union_tokens)
        if union_tokens else 0
    )

    # Salah satu kriteria kemiripan harus terpenuhi.
    return (
        sequence_score >= 0.86
        or (
            jaccard_score >= 0.72
            and len(common_tokens) >= 4
        )
    )


def article_quality(article):
    """
    Menentukan artikel mana yang lebih layak
    dipertahankan ketika ditemukan duplikasi.
    """

    image_score = 1 if article.get("image") else 0

    description_score = len(
        article.get("description", "")
    )

    title_score = len(
        article.get("title", "")
    )

    return (
        image_score,
        description_score,
        title_score
    )



# =========================================================
# MERGE OLD AND NEW ARTICLES
# =========================================================

def merge_articles(new_articles, old_articles):

    # -----------------------------------------------------
    # TAHAP 1: DEDUPLIKASI BERDASARKAN URL
    # -----------------------------------------------------

    url_map = {}

    # Masukkan berita lama.
    for article in old_articles:

        url = normalize_url(
            article.get("originalUrl", "")
        )

        if url:
            url_map[url] = article

    # Masukkan berita terbaru.
    # Jika URL sama, data terbaru menggantikan data lama.
    for article in new_articles:

        url = normalize_url(
            article.get("originalUrl", "")
        )

        if not url:
            continue

        old_article = url_map.get(url, {})

        # Pertahankan gambar lama jika gambar terbaru kosong.
        if not article.get("image"):

            old_image = old_article.get("image", "")

            if old_image:
                article["image"] = old_image

        url_map[url] = article

    candidates = list(url_map.values())

    # -----------------------------------------------------
    # TAHAP 2: FILTER BERITA KADALUWARSA
    # -----------------------------------------------------

    cutoff = datetime.now(
        timezone.utc
    ) - timedelta(
        days=MAX_AGE_DAYS
    )

    valid_articles = []

    for article in candidates:

        date = get_article_datetime(article)

        if date and date < cutoff:
            continue

        valid_articles.append(article)

    # -----------------------------------------------------
    # TAHAP 3: URUTKAN BERITA TERBARU
    # -----------------------------------------------------

    valid_articles.sort(
        key=lambda item: item.get(
            "publishedAt", ""
        ),
        reverse=True
    )

    # -----------------------------------------------------
    # TAHAP 4: DEDUPLIKASI BERDASARKAN JUDUL
    # -----------------------------------------------------

    unique_articles = []

    for article in valid_articles:

        duplicate_index = None

        for index, existing in enumerate(unique_articles):

            if is_same_news(article, existing):

                duplicate_index = index
                break

        # Berita belum pernah ditemukan.
        if duplicate_index is None:

            unique_articles.append(article)
            continue

        # Berita sudah ditemukan.
        existing = unique_articles[duplicate_index]

        logging.info(
            "Duplikasi ditemukan: %s | %s",
            article.get("source", ""),
            article.get("title", "")
        )

        # Pertahankan artikel yang memiliki data lebih baik.
        if article_quality(article) > article_quality(existing):

            unique_articles[duplicate_index] = article

    # -----------------------------------------------------
    # TAHAP 5: BATASI JUMLAH BERITA
    # -----------------------------------------------------

    unique_articles.sort(
        key=lambda item: item.get(
            "publishedAt", ""
        ),
        reverse=True
    )

    return unique_articles[:MAX_ARTICLES]



# =========================================================
# MAIN
# =========================================================

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

        raise RuntimeError(
            "Daftar RSS kosong."
        )

    if not categories:

        raise RuntimeError(
            "Kategori berita kosong."
        )

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
        1
        for item in source_stats
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

    # Statistik gambar.
    images_found = sum(
        1
        for article in final_articles
        if article.get("image")
    )

    images_missing = (
        len(final_articles) - images_found
    )

    logging.info("--------------------------------")

    logging.info(
        "Status: %s",
        status
    )

    logging.info(
        "Sumber berhasil: %d/%d",
        successful,
        len(sources)
    )

    logging.info(
        "Berita baru lolos filter: %d",
        len(all_articles)
    )

    logging.info(
        "Total berita tersimpan: %d",
        len(final_articles)
    )

    logging.info(
        "Berita dengan gambar: %d",
        images_found
    )

    logging.info(
        "Berita tanpa gambar: %d",
        images_missing
    )

    logging.info("--------------------------------")

    # Jika semua sumber gagal, workflow dibuat gagal
    # agar masalah terlihat di GitHub Actions.
    if successful == 0:

        raise RuntimeError(
            "Semua sumber RSS gagal diakses."
        )


if __name__ == "__main__":

    main()
