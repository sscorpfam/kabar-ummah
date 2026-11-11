import os
import re
import json
import html
import hashlib
import logging

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import (
    urlsplit,
    urlunsplit,
    urljoin,
    parse_qsl,
    urlencode
)
from html.parser import HTMLParser
from difflib import SequenceMatcher

import requests
import feedparser


# =========================================================
# CONFIGURATION
# =========================================================

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

# Rentang waktu untuk membandingkan berita lintas media.
DUPLICATE_TIME_WINDOW_HOURS = 72

# Batas minimum kesamaan teks.
TITLE_SIMILARITY_THRESHOLD = 0.82
DESCRIPTION_SIMILARITY_THRESHOLD = 0.48

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

    directory = os.path.dirname(path)

    os.makedirs(
        directory,
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


def normalize_text(value):

    value = clean_text(value).lower()

    value = re.sub(
        r"[^\w\s]",
        " ",
        value,
        flags=re.UNICODE
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
            parts.scheme.lower() in ("http", "https")
            and bool(parts.netloc)
        )

    except Exception:
        return False


def normalize_url(url):

    """
    Membersihkan URL dari fragment dan parameter tracking.

    Parameter yang berpotensi menjadi bagian URL artikel
    tetap dipertahankan agar tidak salah menggabungkan
    dua artikel yang berbeda.
    """

    if not valid_http_url(url):
        return ""

    parts = urlsplit(url.strip())

    tracking_names = {
        "fbclid",
        "gclid",
        "dclid",
        "mc_cid",
        "mc_eid",
        "ref_src",
        "traffic_source",
        "utm"
    }

    query_items = []

    for key, value in parse_qsl(
        parts.query,
        keep_blank_values=True
    ):

        key_lower = key.lower()

        if key_lower.startswith("utm_"):
            continue

        if key_lower in tracking_names:
            continue

        query_items.append((key, value))

    clean_query = urlencode(
        sorted(query_items),
        doseq=True
    )

    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        clean_query,
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


def get_article_datetime(article):

    value = article.get("publishedAt", "")

    if not value:
        return None

    try:

        date = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if date.tzinfo is None:

            date = date.replace(
                tzinfo=timezone.utc
            )

        return date.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


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

        if candidate.lower().startswith(
            ("data:", "javascript:")
        ):
            continue

        if candidate.startswith("//"):

            candidate = "https:" + candidate

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
    # 5. GAMBAR DARI RSS
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

    if not valid_http_url(article_url):
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

    text = normalize_text(
        title + " " + description
    )

    scores = {}
    matched_keywords = {}

    for category, keywords in categories.items():

        score = 0
        matches = []

        for keyword in keywords:

            keyword = normalize_text(keyword)

            if not keyword:
                continue

            # Unicode-aware word boundary.
            pattern = (
                r"(?<!\w)"
                + re.escape(keyword)
                + r"(?!\w)"
            )

            if re.search(pattern, text):

                matches.append(keyword)

                if len(keyword.split()) >= 2:
                    score += 3
                else:
                    score += 2

        if matches:

            scores[category] = score
            matched_keywords[category] = matches

    if not scores:
        return None, []

    best_category = max(
        scores,
        key=scores.get
    )

    if scores[best_category] < 2:
        return None, []

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

            image_url = extract_image(
                entry,
                url
            )

            article = {
                "id": generate_id(url),
                "title": title,
                "description": description[:2000],
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
# DUPLICATE DETECTION
# =========================================================

DEDUP_STOPWORDS = {

    # Indonesia
    "yang", "dan", "di", "ke", "dari", "untuk",
    "pada", "dengan", "ini", "itu", "dalam",
    "oleh", "sebagai", "karena", "setelah",
    "sebelum", "hingga", "saat", "akan",
    "telah", "kembali", "agar", "atau",
    "juga", "masih", "lebih", "para",
    "sejumlah", "sebuah", "orang", "tersebut",
    "menjadi", "adalah", "bukan",

    # English
    "the", "and", "for", "with", "from",
    "that", "this", "after", "before",
    "over", "into", "amid", "says", "said",
    "have", "has", "was", "were", "are",
    "its", "their", "they", "will", "who",
    "how", "why", "what", "when", "where",
    "his", "her", "about", "not", "but",
    "all", "new", "one", "two"
}


# Normalisasi kata kerja bahasa Inggris.
IRREGULAR_WORDS = {

    "killed": "kill",
    "kills": "kill",
    "killing": "kill",

    "attacked": "attack",
    "attacks": "attack",
    "attacking": "attack",

    "wounded": "wound",
    "wounds": "wound",
    "wounding": "wound",

    "injured": "injure",
    "injures": "injure",
    "injuring": "injure",

    "flooded": "flood",
    "floods": "flood",
    "flooding": "flood",

    "died": "die",
    "dies": "die",
    "dying": "die",

    "burned": "burn",
    "burns": "burn",
    "burning": "burn",

    "collapsed": "collapse",
    "collapses": "collapse",
    "collapsing": "collapse",

    "arrested": "arrest",
    "arrests": "arrest",
    "arresting": "arrest",

    "detained": "detain",
    "detains": "detain",
    "detaining": "detain",

    "bombed": "bomb",
    "bombs": "bomb",
    "bombing": "bomb",

    "struck": "strike",
    "strikes": "strike",
    "striking": "strike",

    "rescued": "rescue",
    "rescues": "rescue",
    "rescuing": "rescue"
}


EVENT_ACTIONS = {
    "kill",
    "attack",
    "bomb",
    "strike",
    "wound",
    "injure",
    "arrest",
    "detain",
    "flood",
    "fire",
    "burn",
    "collapse",
    "die",
    "rescue",
    "release",
    "capture",
    "evacuate",
    "explode",
    "destroy"
}


def normalize_dedup_token(token):

    return IRREGULAR_WORDS.get(
        token,
        token
    )


def news_tokens(value):

    tokens = normalize_text(value).split()

    result = set()

    for token in tokens:

        if len(token) < 3:
            continue

        if token in DEDUP_STOPWORDS:
            continue

        result.add(
            normalize_dedup_token(token)
        )

    return result


def token_jaccard(tokens_a, tokens_b):

    if not tokens_a or not tokens_b:
        return 0.0

    union = tokens_a | tokens_b

    if not union:
        return 0.0

    return len(tokens_a & tokens_b) / len(union)


def token_overlap(tokens_a, tokens_b):

    if not tokens_a or not tokens_b:
        return 0.0

    smaller = min(
        len(tokens_a),
        len(tokens_b)
    )

    if smaller == 0:
        return 0.0

    return len(tokens_a & tokens_b) / smaller


def is_within_duplicate_window(article_a, article_b):

    date_a = get_article_datetime(article_a)
    date_b = get_article_datetime(article_b)

    # Jika tanggal tidak tersedia, jangan menggunakan
    # fuzzy matching yang berisiko salah menghapus berita.
    if not date_a or not date_b:
        return False

    difference = abs(
        (date_a - date_b).total_seconds()
    )

    return (
        difference
        <= DUPLICATE_TIME_WINDOW_HOURS * 3600
    )


def is_same_news(article_a, article_b):
    """
    Memeriksa apakah dua artikel kemungkinan membahas
    peristiwa yang sama.

    Tidak menghapus artikel hanya karena menyebut
    wilayah, negara, konflik, atau tokoh yang sama.
    """

    title_a = normalize_text(
        article_a.get("title", "")
    )

    title_b = normalize_text(
        article_b.get("title", "")
    )

    if not title_a or not title_b:
        return False

    # Fuzzy matching wajib berada dalam jendela waktu.
    if not is_within_duplicate_window(
        article_a,
        article_b
    ):
        return False

    title_tokens_a = news_tokens(
        article_a.get("title", "")
    )

    title_tokens_b = news_tokens(
        article_b.get("title", "")
    )

    if not title_tokens_a or not title_tokens_b:
        return False

    common_title = (
        title_tokens_a & title_tokens_b
    )

    title_similarity = token_jaccard(
        title_tokens_a,
        title_tokens_b
    )

    title_overlap = token_overlap(
        title_tokens_a,
        title_tokens_b
    )

    sequence_similarity = SequenceMatcher(
        None,
        title_a,
        title_b
    ).ratio()

    # -----------------------------------------------------
    # 1. JUDUL SANGAT MIRIP
    # -----------------------------------------------------

    if (
        len(common_title) >= 3
        and (
            sequence_similarity >= TITLE_SIMILARITY_THRESHOLD
            or title_similarity >= 0.68
        )
    ):
        return True

    # -----------------------------------------------------
    # 2. POLA PERISTIWA YANG SAMA
    # -----------------------------------------------------

    shared_actions = (
        common_title & EVENT_ACTIONS
    )

    if (
        len(common_title) >= 5
        and title_overlap >= 0.65
        and shared_actions
    ):
        return True

    # -----------------------------------------------------
    # 3. PERBANDINGAN DESKRIPSI
    # -----------------------------------------------------

    description_a = news_tokens(
        article_a.get("description", "")
    )

    description_b = news_tokens(
        article_b.get("description", "")
    )

    common_description = (
        description_a & description_b
    )

    description_similarity = token_jaccard(
        description_a,
        description_b
    )

    description_overlap = token_overlap(
        description_a,
        description_b
    )

    # Isi hampir sama meskipun judul berbeda.
    if (
        len(common_description) >= 8
        and description_similarity
        >= DESCRIPTION_SIMILARITY_THRESHOLD
    ):
        return True

    # Isi memiliki kemiripan tinggi dan judul memiliki
    # beberapa kata penting yang sama.
    if (
        len(common_description) >= 8
        and description_overlap >= 0.72
        and len(common_title) >= 2
        and title_similarity >= 0.25
    ):
        return True

    # -----------------------------------------------------
    # 4. GABUNGAN JUDUL DAN DESKRIPSI
    # -----------------------------------------------------

    combined_a = (
        title_tokens_a | description_a
    )

    combined_b = (
        title_tokens_b | description_b
    )

    combined_common = (
        combined_a & combined_b
    )

    combined_similarity = token_jaccard(
        combined_a,
        combined_b
    )

    if (
        len(combined_common) >= 10
        and combined_similarity >= 0.50
        and len(common_title) >= 3
        and title_overlap >= 0.50
    ):
        return True

    return False


# =========================================================
# ARTICLE QUALITY
# =========================================================

def article_quality(article):

    description = clean_text(
        article.get("description", "")
    )

    title = clean_text(
        article.get("title", "")
    )

    # Prioritas utama: deskripsi paling lengkap.
    # Prioritas kedua: artikel memiliki gambar.
    # Prioritas ketiga: judul tersedia dengan baik.

    return (
        len(description),
        1 if article.get("image") else 0,
        len(title)
    )


def merge_duplicate_metadata(preferred, other):

    result = dict(preferred)

    # -----------------------------------------------------
    # DESKRIPSI TERLENGKAP
    # -----------------------------------------------------

    preferred_description = clean_text(
        result.get("description", "")
    )

    other_description = clean_text(
        other.get("description", "")
    )

    if len(other_description) > len(preferred_description):

        result["description"] = other_description[:2000]

    # -----------------------------------------------------
    # GAMBAR
    # -----------------------------------------------------

    if (
        not result.get("image")
        and other.get("image")
    ):

        result["image"] = other["image"]

    # -----------------------------------------------------
    # TAG
    # -----------------------------------------------------

    tags = []

    for tag in (
        (result.get("tags") or [])
        + (other.get("tags") or [])
    ):

        if tag not in tags:
            tags.append(tag)

    result["tags"] = tags

    # -----------------------------------------------------
    # DAFTAR MEDIA
    # -----------------------------------------------------

    sources = []

    for source in (
        (result.get("sources") or [])
        + [result.get("source", "")]
        + (other.get("sources") or [])
        + [other.get("source", "")]
    ):

        if source and source not in sources:
            sources.append(source)

    if sources:
        result["sources"] = sources

    return result


# =========================================================
# MERGE OLD AND NEW ARTICLES
# =========================================================

def merge_articles(new_articles, old_articles):

    logging.info("Memulai pembersihan berita lama dan baru.")

    # -----------------------------------------------------
    # TAHAP 1: NORMALISASI URL DAN ID
    # -----------------------------------------------------

    url_map = {}

    for article in old_articles:

        if not isinstance(article, dict):
            continue

        url = normalize_url(
            article.get("originalUrl", "")
        )

        if not url:
            continue

        article = dict(article)

        article["originalUrl"] = url
        article["id"] = generate_id(url)

        existing = url_map.get(url)

        if existing:

            if article_quality(article) > article_quality(existing):

                article = merge_duplicate_metadata(
                    article,
                    existing
                )

            else:

                article = merge_duplicate_metadata(
                    existing,
                    article
                )

        url_map[url] = article

    # -----------------------------------------------------
    # TAHAP 2: MASUKKAN BERITA RSS TERBARU
    # -----------------------------------------------------

    for article in new_articles:

        if not isinstance(article, dict):
            continue

        url = normalize_url(
            article.get("originalUrl", "")
        )

        if not url:
            continue

        article = dict(article)

        article["originalUrl"] = url
        article["id"] = generate_id(url)

        existing = url_map.get(url)

        if existing:

            if article_quality(article) > article_quality(existing):

                article = merge_duplicate_metadata(
                    article,
                    existing
                )

            else:

                article = merge_duplicate_metadata(
                    existing,
                    article
                )

        url_map[url] = article

    candidates = list(
        url_map.values()
    )

    logging.info(
        "Artikel setelah pembersihan URL: %d",
        len(candidates)
    )

    # -----------------------------------------------------
    # TAHAP 3: HAPUS BERITA KEDALUWARSA
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
    # TAHAP 4: URUTKAN BERDASARKAN PUBLIKASI
    # -----------------------------------------------------

    valid_articles.sort(
        key=lambda item: item.get(
            "publishedAt", ""
        ),
        reverse=True
    )

    # -----------------------------------------------------
    # TAHAP 5: DEDUPLIKASI BERDASARKAN ID
    # -----------------------------------------------------

    id_map = {}

    for article in valid_articles:

        article_id = article.get("id", "")

        if not article_id:
            article_id = generate_id(
                article.get("originalUrl", "")
            )

        article["id"] = article_id

        existing = id_map.get(article_id)

        if existing:

            if article_quality(article) > article_quality(existing):

                article = merge_duplicate_metadata(
                    article,
                    existing
                )

            else:

                article = merge_duplicate_metadata(
                    existing,
                    article
                )

        id_map[article_id] = article

    valid_articles = list(
        id_map.values()
    )

    # -----------------------------------------------------
    # TAHAP 6: DEDUPLIKASI PERISTIWA LINTAS MEDIA
    # -----------------------------------------------------

    unique_articles = []

    duplicate_count = 0

    for article in valid_articles:

        duplicate_index = None

        for index, existing in enumerate(unique_articles):

            if is_same_news(
                article,
                existing
            ):

                duplicate_index = index
                break

        # Berita baru, belum ditemukan duplikat.
        if duplicate_index is None:

            unique_articles.append(article)
            continue

        # Berita merupakan duplikat.
        duplicate_count += 1

        existing = unique_articles[duplicate_index]

        logging.info(
            "Duplikasi ditemukan: %s | %s",
            article.get("source", ""),
            article.get("title", "")
        )

        # Pilih artikel dengan kualitas terbaik.
        if article_quality(article) > article_quality(existing):

            preferred = article
            other = existing

        else:

            preferred = existing
            other = article

        unique_articles[duplicate_index] = (
            merge_duplicate_metadata(
                preferred,
                other
            )
        )

    # -----------------------------------------------------
    # TAHAP 7: PEMBERSIHAN ID TERAKHIR
    # -----------------------------------------------------

    final_map = {}

    for article in unique_articles:

        article_id = article.get("id", "")

        if not article_id:
            continue

        if article_id not in final_map:

            final_map[article_id] = article

    final_articles = list(
        final_map.values()
    )

    # -----------------------------------------------------
    # TAHAP 8: URUTKAN DAN BATASI
    # -----------------------------------------------------

    final_articles.sort(
        key=lambda item: item.get(
            "publishedAt", ""
        ),
        reverse=True
    )

    final_articles = final_articles[:MAX_ARTICLES]

    logging.info(
        "========================================"
    )

    logging.info(
        "Artikel sebelum deduplikasi: %d",
        len(valid_articles)
    )

    logging.info(
        "Duplikasi peristiwa digabung: %d",
        duplicate_count
    )

    logging.info(
        "Total berita unik: %d",
        len(final_articles)
    )

    logging.info(
        "========================================"
    )

    return final_articles


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

    # Membaca data berita lama.
    # Data lama juga akan dibersihkan kembali.
    previous_articles = load_previous_articles()

    logging.info(
        "Berita lama ditemukan: %d",
        len(previous_articles)
    )

    all_articles = []

    source_stats = []

    # -----------------------------------------------------
    # FETCH SELURUH RSS
    # -----------------------------------------------------

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

    failed = (
        len(source_stats) - successful
    )

    # -----------------------------------------------------
    # DEDUPLIKASI BERITA LAMA DAN BARU
    # -----------------------------------------------------

    final_articles = merge_articles(
        all_articles,
        previous_articles
    )

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    if successful == 0:

        status = "error"

    elif failed > 0:

        status = "partial"

    else:

        status = "success"

    # -----------------------------------------------------
    # OUTPUT JSON
    # -----------------------------------------------------

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

    # Menulis ulang news.json.
    # Seluruh data lama yang duplikat akan diganti
    # oleh hasil pembersihan di atas.
    save_json(
        OUTPUT_FILE,
        result
    )

    # -----------------------------------------------------
    # STATISTIK GAMBAR
    # -----------------------------------------------------

    images_found = sum(
        1
        for article in final_articles
        if article.get("image")
    )

    images_missing = (
        len(final_articles) - images_found
    )

    # -----------------------------------------------------
    # LOGGING
    # -----------------------------------------------------

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
        "Berita lama sebelum pembersihan: %d",
        len(previous_articles)
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

    # -----------------------------------------------------
    # GAGAL JIKA SEMUA RSS GAGAL
    # -----------------------------------------------------

    if successful == 0:

        raise RuntimeError(
            "Semua sumber RSS gagal diakses."
        )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    main()
