import os
import re
import json
import html
import hashlib
import logging
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import requests
import feedparser
from deep_translator import GoogleTranslator


# =========================================================
# KONFIGURASI
# =========================================================

# Pertahankan struktur direktori proyek seperti kode sebelumnya.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCES_FILE = os.path.join(BASE_DIR, "aggregator", "sources.json")
CATEGORIES_FILE = os.path.join(BASE_DIR, "aggregator", "categories.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "data", "news.json")

MAX_ARTICLES = 500
MAX_AGE_DAYS = 90
TIMEOUT = 25
TRANSLATION_RETRIES = 2
TRANSLATION_RETRY_DELAY = 2

HEADERS = {
    "User-Agent": "KabarUmmahNewsBot/1.0 (+https://github.com/sscorpfam/kabar-ummah)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


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
    "masjid al-aqsa", "al aqsa",
)

CONTEXT_TITLE_KEYWORDS = (
    "haji", "hajj", "umrah", "umroh", "jamaah", "jemaah", "masjid",
    "mosque", "pesantren", "santri", "madrasah", "musholla", "musala",
    "makkah", "mekkah", "madinah", "muhammadiyah", "nahdlatul ulama",
    "persis", "mui", "kemenag", "kemenhaj", "menhaj", "baznas",
    "majelis taklim",
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
    "jamaah umrah", "jemaah umrah", "perhajian", "tanah suci",
    "masjidil haram", "masjid nabawi", "masjid al-aqsa",
    "pendidikan pesantren", "pondok pesantren", "madrasah islam",
    "organisasi islam", "ormas islam", "umat islam", "umat muslim",
    "masyarakat muslim", "kehidupan muslim", "hak muslim", "pengungsi muslim",
    "diskriminasi muslim", "kemanusiaan palestina", "rakyat palestina",
)

PROMOTIONAL_KEYWORDS = (
    "kurir terbaik", "promo", "diskon", "brand ambassador", "endorsement",
    "program loyalitas", "peluncuran produk", "produk terbaru", "kampanye merek",
    "kolaborasi perusahaan", "kerja sama komersial", "apresiasi pelanggan",
    "hadiah pelanggan", "paket wisata", "paket perjalanan", "sponsor utama",
)


def clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def contains_keyword(text, keywords):
    text = clean_text(text).lower()
    for keyword in keywords:
        pattern = r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            return True
    return False


def article_original_text(article, field):
    """Gunakan teks asli untuk filter bila artikel sudah diterjemahkan."""
    original_field = "originalTitle" if field == "title" else "originalDescription"
    return article.get(original_field) or article.get(field, "")


def is_islamic_news(title, description=""):
    title_text = clean_text(title or "").lower()
    description_text = clean_text(description or "").lower()
    combined_text = f"{title_text} {description_text}"

    if contains_keyword(title_text, DIRECT_ISLAMIC_KEYWORDS):
        return True

    has_context_title = contains_keyword(title_text, CONTEXT_TITLE_KEYWORDS)
    context_matches = sum(
        1 for keyword in ISLAMIC_CONTEXT_KEYWORDS
        if contains_keyword(combined_text, (keyword,))
    )

    if has_context_title and context_matches >= 2:
        if contains_keyword(combined_text, PROMOTIONAL_KEYWORDS):
            substantive = (
                "manasik", "ihram", "tawaf", "sa'i", "miqat", "petugas haji",
                "pelayanan haji", "penyelenggaraan haji", "kuota haji",
                "embarkasi haji", "kebijakan haji", "aturan haji",
                "jamaah haji", "jemaah haji", "ibadah umrah", "ibadah haji",
            )
            if not contains_keyword(combined_text, substantive):
                return False
        return True

    organization_terms = (
        "muhammadiyah", "nahdlatul ulama", "persis", "mui", "kemenag",
        "kemenhaj", "baznas", "pesantren", "madrasah",
    )
    if contains_keyword(combined_text, organization_terms) and context_matches >= 2:
        return True

    return False


# =========================================================
# JSON
# =========================================================


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        logging.warning("File tidak ditemukan: %s", path)
        return default
    except Exception as error:
        logging.error("Gagal membaca %s: %s", path, error)
        return default


def save_json(path, data):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


# =========================================================
# URL DAN ID
# =========================================================


def valid_http_url(url):
    if not url:
        return False
    try:
        parts = urlsplit(str(url).strip())
        return parts.scheme in ("http", "https") and bool(parts.netloc)
    except Exception:
        return False


def normalize_url(url):
    if not valid_http_url(url):
        return ""
    parts = urlsplit(str(url).strip())
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        parts.query,
        "",
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
    html_fields.append(entry.get("description", ""))

    for content in html_fields:
        match = re.search(
            r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']',
            content or "",
            re.IGNORECASE,
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
            keyword = str(keyword).lower().strip()
            if not keyword:
                continue
            pattern = r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"
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
        reverse=True,
    )
    return best_category, matched_categories


# =========================================================
# PENERJEMAHAN BERITA INGGRIS
# =========================================================


def is_english_language(language):
    language = str(language or "").strip().lower()
    return language in ("en", "eng", "english", "en-us", "en-gb")


def translate_text(text, translator):
    text = str(text or "").strip()
    if not text:
        return ""

    last_error = None
    for attempt in range(TRANSLATION_RETRIES + 1):
        try:
            translated = translator.translate(text)
            if not translated or not str(translated).strip():
                raise ValueError("Hasil terjemahan kosong")
            return str(translated).strip()
        except Exception as error:
            last_error = error
            if attempt < TRANSLATION_RETRIES:
                time.sleep(TRANSLATION_RETRY_DELAY)

    raise RuntimeError(str(last_error))


def translate_article(article):
    """Terjemahkan judul dan deskripsi artikel berbahasa Inggris."""
    language = article.get("language", "id")

    if not is_english_language(language):
        article.setdefault("translationStatus", "not_needed")
        return article

    # Artikel yang pernah sukses diterjemahkan tidak diterjemahkan ulang.
    if article.get("translationStatus") == "success":
        return article

    original_title = article.get("originalTitle") or article.get("title", "")
    original_description = (
        article.get("originalDescription") or article.get("description", "")
    )

    try:
        translator = GoogleTranslator(source="auto", target="id")
        translated_title = translate_text(original_title, translator)
        translated_description = translate_text(original_description, translator)

        if original_title and not translated_title:
            raise ValueError("Judul hasil terjemahan kosong")
        if original_description and not translated_description:
            raise ValueError("Deskripsi hasil terjemahan kosong")

        article["originalTitle"] = original_title
        article["originalDescription"] = original_description
        article["title"] = translated_title
        article["description"] = translated_description
        article["translationStatus"] = "success"
        article["translatedTo"] = "id"

        logging.info("Terjemahan berhasil: %s", original_title[:100])

    except Exception as error:
        # Jangan hilangkan berita jika layanan terjemahan sedang bermasalah.
        article["originalTitle"] = original_title
        article["originalDescription"] = original_description
        article["title"] = original_title
        article["description"] = original_description
        article["translationStatus"] = "failed"
        article["translationError"] = str(error)[:300]
        logging.warning(
            "Terjemahan gagal untuk '%s': %s",
            original_title[:100], error,
        )

    return article


def translate_saved_articles(articles):
    """Hanya menerjemahkan artikel final yang akan disimpan."""
    translated_count = 0
    skipped_count = 0
    failed_count = 0

    for article in articles:
        if not isinstance(article, dict):
            continue

        if not is_english_language(article.get("language", "id")):
            article.setdefault("translationStatus", "not_needed")
            skipped_count += 1
            continue

        if article.get("translationStatus") == "success":
            skipped_count += 1
            continue

        translate_article(article)
        if article.get("translationStatus") == "success":
            translated_count += 1
        elif article.get("translationStatus") == "failed":
            failed_count += 1

    logging.info(
        "Penerjemahan selesai | berhasil: %d | dilewati: %d | gagal: %d",
        translated_count, skipped_count, failed_count,
    )
    return articles


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
        "error": None,
    }

    try:
        response = requests.get(
            source["url"],
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        feed = feedparser.parse(response.content)
        entries = feed.entries
        result["raw"] = len(entries)

        if not entries and feed.bozo:
            raise ValueError("RSS tidak dapat dibaca: " + str(feed.bozo_exception))

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

            # Filter dan kategori dilakukan sebelum penerjemahan.
            if not is_islamic_news(title, description):
                result["filtered"] += 1
                continue

            category, tags = categorize(title, description, categories)
            if not category:
                result["filtered"] += 1
                continue

            source_language = str(source.get("language", "id")).strip().lower()

            article = {
                "id": generate_id(url),
                "title": title,
                "description": description[:800],
                "image": extract_image(entry),
                "category": category,
                "tags": tags,
                "source": source.get("name", "Unknown"),
                "sourceCountry": source.get("country", ""),
                "region": "",
                "language": source_language,
                "country": source.get("country", ""),
                "publishedAt": get_published_date(entry),
                "originalUrl": url,
            }
            articles.append(article)

        result["accepted"] = len(articles)
        logging.info(
            "%s | RSS: %d | Lolos: %d | Disaring: %d",
            result["name"], result["raw"], result["accepted"], result["filtered"],
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
# MERGE, DEDUPLIKASI, DAN FILTER BERITA LAMA
# =========================================================


def merge_articles(new_articles, old_articles):
    merged = {}

    # Berita lama: filter berdasarkan teks asli jika pernah diterjemahkan.
    for article in old_articles:
        if not isinstance(article, dict):
            continue

        title_for_filter = article_original_text(article, "title")
        description_for_filter = article_original_text(article, "description")
        if not is_islamic_news(title_for_filter, description_for_filter):
            continue

        url = normalize_url(article.get("originalUrl", ""))
        if url:
            # Pertahankan artikel lama, termasuk status terjemahannya.
            merged[url] = article

    # Berita baru menggantikan berita lama bila URL sama.
    for article in new_articles:
        if not isinstance(article, dict):
            continue
        if not is_islamic_news(article.get("title", ""), article.get("description", "")):
            continue

        url = normalize_url(article.get("originalUrl", ""))
        if url:
            # Jika URL dan isi artikel sama dengan versi lama yang sudah
            # diterjemahkan, gunakan kembali hasil terjemahan agar tidak
            # menerjemahkan artikel yang sama setiap workflow berjalan.
            old_article = merged.get(url)
            if old_article and old_article.get("translationStatus") == "success":
                old_original_title = old_article.get("originalTitle", "")
                old_original_description = old_article.get("originalDescription", "")
                new_title = article.get("title", "")
                new_description = article.get("description", "")

                if (
                    old_original_title == new_title
                    and old_original_description == new_description
                ):
                    article["title"] = old_article.get("title", new_title)
                    article["description"] = old_article.get("description", new_description)
                    article["originalTitle"] = old_original_title
                    article["originalDescription"] = old_original_description
                    article["translationStatus"] = "success"
                    article["translatedTo"] = old_article.get("translatedTo", "id")

            merged[url] = article

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    valid_articles = []

    for article in merged.values():
        try:
            published_at = article.get("publishedAt", "")
            date = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if date < cutoff:
                continue
        except Exception:
            # Jika tanggal tidak valid, jangan buang artikel hanya karena itu.
            pass
        valid_articles.append(article)

    valid_articles.sort(
        key=lambda item: item.get("publishedAt", ""),
        reverse=True,
    )
    return valid_articles[:MAX_ARTICLES]


# =========================================================
# MAIN
# =========================================================


def main():
    sources = load_json(SOURCES_FILE, default=[])
    categories = load_json(CATEGORIES_FILE, default={})

    if not isinstance(sources, list) or not sources:
        raise RuntimeError("Daftar RSS kosong atau format sources.json tidak valid.")
    if not isinstance(categories, dict) or not categories:
        raise RuntimeError("Kategori berita kosong atau format categories.json tidak valid.")

    previous_articles = load_previous_articles()
    all_articles = []
    source_stats = []

    for source in sources:
        if not isinstance(source, dict) or not source.get("url"):
            logging.warning("Konfigurasi sumber RSS tidak valid: %s", source)
            source_stats.append({
                "name": source.get("name", "Unknown") if isinstance(source, dict) else "Unknown",
                "status": "error",
                "raw": 0,
                "accepted": 0,
                "filtered": 0,
                "error": "Konfigurasi sumber atau URL tidak tersedia",
            })
            continue

        articles, stats = fetch_source(source, categories)
        all_articles.extend(articles)
        source_stats.append(stats)

    successful = sum(1 for item in source_stats if item["status"] == "success")
    failed = len(source_stats) - successful

    # Gabungkan, deduplikasi, filter berita lama, dan batasi jumlah/usia berita.
    final_articles = merge_articles(all_articles, previous_articles)

    # PENTING: terjemahan dilakukan setelah merge.
    # Jadi hanya artikel final yang akan masuk news.json yang diterjemahkan.
    final_articles = translate_saved_articles(final_articles)

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
        "articles": final_articles,
    }

    save_json(OUTPUT_FILE, result)

    logging.info("--------------------------------")
    logging.info("Status: %s", status)
    logging.info("Sumber berhasil: %d/%d", successful, len(sources))
    logging.info("Berita baru lolos filter: %d", len(all_articles))
    logging.info("Total berita tersimpan: %d", len(final_articles))
    logging.info("File tersimpan: %s", OUTPUT_FILE)
    logging.info("--------------------------------")

    # Simpan hasil terlebih dahulu, baru gagalkan workflow bila semua RSS gagal.
    if successful == 0:
        raise RuntimeError("Semua sumber RSS gagal diakses.")


if __name__ == "__main__":
    main()
