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
from deep_translator import GoogleTranslator


BASE_DIR = os.path.dirname(os.path.dirname(__file__))

SOURCES_FILE = os.path.join(BASE_DIR, "aggregator", "sources.json")
CATEGORIES_FILE = os.path.join(BASE_DIR, "aggregator", "categories.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "data", "news.json")

MAX_ARTICLES = 500
MAX_AGE_DAYS = 90
TIMEOUT = 25

HEADERS = {
    "User-Agent": "KabarUmmahNewsBot/1.0 (+https://github.com/sscorpfam/kabar-ummah)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*"
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


# =========================================================
# PENERJEMAH TEKS (TRANSLATOR)
# =========================================================

def translate_text(text, target_lang="en"):
    """Menerjemahkan teks secara otomatis menggunakan deep-translator."""
    if not text:
        return ""
    try:
        # Batasi panjang teks jika terlalu panjang untuk menghindari error limit translator
        trimmed_text = text[:1000]
        translated = GoogleTranslator(source='auto', target=target_lang).translate(trimmed_text)
        return translated if translated else text
    except Exception as error:
        logging.error("Gagal menerjemahkan teks: %s", error)
        return text


# =========================================================
# FILTER BERITA ISLAMI
# =========================================================

DIRECT_ISLAMIC_KEYWORDS = (
    "islam", "islami", "islamic", "muslim", "muslimah", "umat islam",
    "umat muslim", "kaum muslimin", "al quran", "alquran", "quran",
    "qur'an", "mushaf", "tafsir", "hadis", "hadits", "hadith", "sunnah",
    "shalat", "salat", "sholat", "zakat", "infak", "infaq", "sedekah",
    "puasa", "shaum", "ramadan", "ramadhan", "qurban", "kurban",
    "tahajud", "tarawih", "witir", "azan", "adzan", "iqamah",
    "doa nabi", "doa rasul", "dzikir", "zikir", "istighfar", "wudhu",
    "wudu", "nabi", "rasulullah", "muhammad saw", "sirah nabawi",
    "sejarah islam", "peradaban islam", "khilafah", "ulama", "ustadz",
    "ustaz", "kyai", "kiai", "dai", "da'i", "mufti", "habib", "syekh",
    "syaikh", "ustadzah", "ustazah", "dakwah", "kajian islam",
    "kajian islami", "ceramah agama", "fatwa", "fikih", "fiqih", "akidah",
    "aqidah", "syariah", "syariat", "hukum islam", "pendidikan islam",
    "ekonomi syariah", "keuangan syariah", "perbankan syariah", "wakaf",
    "badan amil zakat", "idul fitri", "idul adha", "maulid nabi",
    "isra mikraj", "isra mi'raj", "muharram", "ka'bah", "baitullah",
    "hijrah", "hijriah", "hijriyah", "moderasi beragama",
    "ukhuwah islamiyah", "syiar islam", "dunia islam", "islamofobia",
    "anti-muslim", "diskriminasi muslim", "palestina", "gaza",
    "masjid al-aqsa", "al aqsa"
)

CONTEXT_TITLE_KEYWORDS = (
    "haji", "hajj", "umrah", "umroh", "jamaah", "jemaah", "masjid",
    "mosque", "pesantren", "santri", "madrasah", "musholla", "musala",
    "makkah", "mekkah", "madinah", "muhammadiyah", "nahdlatul ulama",
    "persis", "mui", "kemenag", "kemenhaj", "menhaj", "baznas", "majelis taklim"
)

ISLAMIC_CONTEXT_KEYWORDS = (
    "ibadah", "keimanan", "iman", "takwa", "ketaqwaan", "syariat",
    "syariah", "ajaran islam", "hukum islam", "hukum syariat", "fatwa",
    "ulama", "ustaz", "ustadz", "kiai", "kyai", "dakwah", "pengajian",
    "khutbah", "khutbah jumat", "ceramah", "kajian", "jamaah masjid",
    "salat berjamaah", "shalat berjamaah", "alquran", "al-quran", "tafsir",
    "hadis", "hadits", "rasulullah", "nabi muhammad", "zakat", "wakaf",
    "sedekah", "puasa", "ramadan", "ramadhan", "manasik", "ihram",
    "tawaf", "sa'i", "sai", "miqat", "haji reguler", "petugas haji",
    "pelayanan haji", "layanan haji", "penyelenggaraan haji", "kuota haji",
    "embarkasi haji", "petugas haji daerah", "seleksi petugas haji",
    "menteri haji", "kementerian haji", "jamaah haji", "jemaah haji",
    "jamaah umrah", "jemaah umrah", "perhajian",
    "tanah suci", "masjidil haram", "masjid nabawi", "masjid al-aqsa",
    "pendidikan pesantren", "pondok pesantren", "madrasah islam",
    "organisasi islam", "ormas islam", "umat islam", "umat muslim",
    "masyarakat muslim", "kehidupan muslim", "hak muslim", "pengungsi muslim",
    "diskriminasi muslim", "kemanusiaan palestina", "rakyat palestina"
)

PROMOTIONAL_KEYWORDS = (
    "kurir terbaik", "promo", "diskon", "brand ambassador", "endorsement",
    "program loyalitas", "peluncuran produk", "produk terbaru", "kampanye merek",
    "kolaborasi perusahaan", "kerja sama komersial", "apresiasi pelanggan",
    "hadiah pelanggan", "paket wisata", "paket perjalanan", "sponsor utama"
)


def _contains_keyword(text, keywords):
    text = clean_text(text).lower()
    for keyword in keywords:
        pattern = (
            r"(?<![a-z0-9])"
            + re.escape(keyword.lower())
            + r"(?![a-z0-9])"
        )
        if re.search(pattern, text):
            return True
    return False


def is_islamic_news(title, description=""):
    title_text = clean_text(title or "").lower()
    description_text = clean_text(description or "").lower()
    combined_text = f"{title_text} {description_text}"

    if _contains_keyword(title_text, DIRECT_ISLAMIC_KEYWORDS):
        return True

    has_context_title = _contains_keyword(title_text, CONTEXT_TITLE_KEYWORDS)
    context_matches = sum(
        1 for keyword in ISLAMIC_CONTEXT_KEYWORDS
        if _contains_keyword(combined_text, (keyword,))
    )

    if has_context_title and context_matches >= 2:
        if _contains_keyword(combined_text, PROMOTIONAL_KEYWORDS):
            substantive = (
                "manasik", "ihram", "tawaf", "sa'i", "miqat", "petugas haji",
                "pelayanan haji", "penyelenggaraan haji", "kuota haji",
                "embarkasi haji", "kebijakan haji", "aturan haji",
                "jamaah haji", "jemaah haji", "ibadah umrah", "ibadah haji"
            )
            if not _contains_keyword(combined_text, substantive):
                return False
        return True

    organization_terms = (
        "muhammadiyah", "nahdlatul ulama", "persis", "mui",
        "kemenag", "kemenhaj", "baznas", "pesantren", "madrasah"
    )
    if _contains_keyword(combined_text, organization_terms) and context_matches >= 2:
        return True

    return False


# =========================================================
# JSON
# =========================================================

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
        json.dump(data, file, ensure_ascii=False, indent=2)

    os.replace(temp_path, path)


# =========================================================
# TEKS DAN URL
# =========================================================

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
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        parts.query,
        ""
    ))


def generate_id(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# =========================================================
# TANGGAL PUBLIKASI
# =========================================================

def get_published_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")

    if parsed:
        from calendar import timegm
        return datetime.fromtimestamp(timegm(parsed), timezone.utc).isoformat()

    for field in ("published", "updated", "pubDate"):
        value = entry.get(field)
        if not value:
            continue

        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return date.astimezone(timezone.utc).isoformat()
        except Exception:
            continue

    return datetime.now(timezone.utc).isoformat()


# =========================================================
# EKSTRAKSI GAMBAR
# =========================================================

def extract_image(entry):
    candidates = []

    for field in ("media_content", "media_thumbnail"):
        values = entry.get(field, [])
        if isinstance(values, dict):
            values = [values]

        for item in values:
            if isinstance(item, dict):
                candidates.append(item.get("url", ""))

    for item in entry.get("enclosures", []):
        if isinstance(item, dict):
            candidates.append(item.get("href", ""))

    for item in entry.get("links", []):
        if isinstance(item, dict) and item.get("rel") == "enclosure":
            candidates.append(item.get("href", ""))

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


# =========================================================
# KATEGORISASI
# =========================================================

def categorize(title, description, categories):
    text = clean_text(title + " " + description).lower()

    if not is_islamic_news(title, description):
        return None, []

    scores = {}
    matched_keywords = {}

    for category, keywords in categories.items():
        score = 0
        matches = []

        for keyword in keywords:
            keyword = keyword.lower().strip()
            if not keyword:
                continue

            pattern = (
                r"(?<![a-z0-9])"
                + re.escape(keyword)
                + r"(?![a-z0-9])"
            )

            if re.search(pattern, text):
                matches.append(keyword)
                score += 3 if len(keyword.split()) >= 2 else 2

        if matches:
            scores[category] = score
            matched_keywords[category] = matches

    if not scores:
        return None, []

    best_category = max(scores, key=scores.get)

    if scores[best_category] < 2:
        return None, []

    matched_categories = sorted(
        matched_keywords.keys(),
        key=lambda category: scores[category],
        reverse=True
    )

    return best_category, matched_categories


# =========================================================
# FETCH RSS
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

        feed = feedparser.parse(response.content)
        entries = feed.entries
        result["raw"] = len(entries)

        if not entries and feed.bozo:
            raise ValueError(
                "RSS tidak dapat dibaca: " + str(feed.bozo_exception)
            )

        result["status"] = "success"
        articles = []

        for entry in entries:
            title = clean_text(entry.get("title", ""))
            description = clean_text(entry.get("summary", ""))

            if not description:
                description = clean_text(entry.get("description", ""))

            url = normalize_url(entry.get("link", ""))

            if not title or not url:
                result["filtered"] += 1
                continue

            if not is_islamic_news(title, description):
                result["filtered"] += 1
                continue

            category, tags = categorize(title, description, categories)

            if not category:
                result["filtered"] += 1
                continue

            # --- PROSES TERJEMAHAN OTOMATIS KE BAHASA INGGRIS ---
            cleaned_desc = description[:800]
            title_en = translate_text(title, target_lang="en")
            description_en = translate_text(cleaned_desc, target_lang="en")

            article = {
                "id": generate_id(url),
                "title": title,
                "titleEn": title_en,
                "description": cleaned_desc,
                "descriptionEn": description_en,
                "image": extract_image(entry),
                "category": category,
                "tags": tags,
                "source": source["name"],
                "sourceCountry": source.get("country", ""),
                "region": "",
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
        logging.error("%s gagal: %s", result["name"], error)
        return [], result


# =========================================================
# BERITA LAMA
# =========================================================

def load_previous_articles():
    previous = load_json(OUTPUT_FILE, default={})

    if not isinstance(previous, dict):
        return []

    articles = previous.get("articles", [])
    return articles if isinstance(articles, list) else []


# =========================================================
# MERGE DAN FILTER
# =========================================================

def merge_articles(new_articles, old_articles):
    merged = {}

    for article in old_articles:
        if not isinstance(article, dict):
            continue

        if not is_islamic_news(
            article.get("title", ""),
            article.get("description", "")
        ):
            continue

        url = normalize_url(article.get("originalUrl", ""))
        if url:
            merged[url] = article

    for article in new_articles:
        if not isinstance(article, dict):
            continue

        if not is_islamic_news(
            article.get("title", ""),
            article.get("description", "")
        ):
            continue

        url = normalize_url(article.get("originalUrl", ""))
        if url:
            merged[url] = article

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
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


# =========================================================
# MAIN
# =========================================================

def main():
    sources = load_json(SOURCES_FILE, default=[])
    categories = load_json(CATEGORIES_FILE, default={})

    if not sources:
        raise RuntimeError("Daftar RSS kosong.")

    if not categories:
        raise RuntimeError("Kategori berita kosong.")

    previous_articles = load_previous_articles()
    all_articles = []
    source_stats = []

    for source in sources:
        articles, stats = fetch_source(source, categories)
        all_articles.extend(articles)
        source_stats.append(stats)

    successful = sum(
        1 for item in source_stats if item["status"] == "success"
    )
    failed = len(source_stats) - successful

    final_articles = merge_articles(all_articles, previous_articles)

    if successful == 0:
        status = "error"
    elif failed > 0:
        status = "partial"
    else:
        status = "success"

    result = {
        "status": status,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "total": len(final_articles),
        "newArticles": len(all_articles),
        "sourcesChecked": len(sources),
        "sourcesSucceeded": successful,
        "sourcesFailed": failed,
        "sourceStats": source_stats,
        "articles": final_articles
    }

    save_json(OUTPUT_FILE, result)

    logging.info("--------------------------------")
    logging.info("Status: %s", status)
    logging.info("Sumber berhasil: %d/%d", successful, len(sources))
    logging.info("Berita baru lolos filter: %d", len(all_articles))
    logging.info("Total berita tersimpan: %d", len(final_articles))
    logging.info("--------------------------------")

    if successful == 0:
        raise RuntimeError("Semua sumber RSS gagal diakses.")


if __name__ == "__main__":
    main()
