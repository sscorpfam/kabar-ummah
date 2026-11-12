import os
import re
import json
import html
import hashlib
import logging

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, urljoin, parse_qsl, urlencode
from html.parser import HTMLParser
from difflib import SequenceMatcher
from calendar import timegm

import requests
import feedparser


# =========================================================
# KONFIGURASI
# =========================================================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCES_FILE = os.path.join(BASE_DIR, "aggregator", "sources.json")
CATEGORIES_FILE = os.path.join(BASE_DIR, "aggregator", "categories.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "data", "news.json")
TRANSLATION_CACHE_FILE = os.path.join(BASE_DIR, "data", "translation_cache.json")

MAX_ARTICLES = 500
MAX_AGE_DAYS = 7  # Hapus berita yang lebih lama dari 7 hari dari news.json
TIMEOUT = 25
IMAGE_TIMEOUT = 12

DUPLICATE_TIME_WINDOW_HOURS = 72
TITLE_SIMILARITY_THRESHOLD = 0.82
DESCRIPTION_SIMILARITY_THRESHOLD = 0.48

TRANSLATION_SOURCE_LANGUAGE = "en"
TRANSLATION_TARGET_LANGUAGE = "id"

RSS_HEADERS = {
    "User-Agent": "KabarUmmahNewsBot/1.0 (+https://github.com/sscorpfam/kabar-ummah)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
}

PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

TRANSLATION_MODEL = None
TRANSLATOR_INITIALIZED = False
TRANSLATOR_FAILED = False
TRANSLATION_CACHE = None
TRANSLATION_CACHE_DIRTY = False


# =========================================================
# JSON
# =========================================================

def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        logging.warning("File belum ditemukan: %s", path)
        return default
    except Exception as error:
        logging.error("Gagal membaca %s: %s", path, error)
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)

    # Penulisan atomik mencegah file JSON terpotong jika proses gagal.
    os.replace(temp_path, path)


# =========================================================
# TEKS DAN URL
# =========================================================

def clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_text(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def valid_http_url(url):
    if not url:
        return False
    try:
        parts = urlsplit(str(url).strip())
        return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)
    except Exception:
        return False


def normalize_url(url):
    if not valid_http_url(url):
        return ""

    parts = urlsplit(str(url).strip())
    tracking_names = {
        "fbclid", "gclid", "dclid", "mc_cid", "mc_eid",
        "ref_src", "traffic_source"
    }

    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in tracking_names:
            continue
        query_items.append((key, value))

    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        urlencode(sorted(query_items), doseq=True),
        ""
    ))


def generate_id(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# =========================================================
# TANGGAL
# =========================================================

def get_published_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")

    if parsed:
        try:
            return datetime.fromtimestamp(timegm(parsed), timezone.utc).isoformat()
        except Exception:
            pass

    for field in ("published", "updated", "pubDate", "date"):
        value = entry.get(field)
        if not value:
            continue
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return date.astimezone(timezone.utc).isoformat()
        except Exception:
            try:
                date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return date.astimezone(timezone.utc).isoformat()
            except Exception:
                continue

    return datetime.now(timezone.utc).isoformat()


def get_article_datetime(article):
    value = article.get("publishedAt", "")
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# =========================================================
# RSS PARSER
# =========================================================

def parse_rss_safely(content, response_encoding=None):
    feed = feedparser.parse(content)
    if feed.entries and not feed.bozo:
        return feed

    encodings = []
    if response_encoding:
        encodings.append(response_encoding)
    encodings.extend(["utf-8", "latin-1"])

    for encoding in dict.fromkeys(encodings):
        try:
            text = content.decode(encoding, errors="replace")
            text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
            text = re.sub(
                r'(<\?xml[^>]*encoding\s*=\s*)["\'][^"\']+["\']',
                r'\1"UTF-8"',
                text,
                count=1,
                flags=re.IGNORECASE
            )
            repaired = feedparser.parse(text.encode("utf-8"))
            if repaired.entries:
                logging.info("RSS berhasil dibaca setelah perbaikan XML.")
                return repaired
        except Exception as error:
            logging.debug("Perbaikan RSS (%s) gagal: %s", encoding, error)

    return feed


# =========================================================
# ARGOS TRANSLATE
# =========================================================

def load_translation_cache():
    global TRANSLATION_CACHE

    if TRANSLATION_CACHE is None:
        data = load_json(TRANSLATION_CACHE_FILE, default={})
        TRANSLATION_CACHE = data if isinstance(data, dict) else {}
        logging.info("Cache terjemahan dimuat: %d entri", len(TRANSLATION_CACHE))

    return TRANSLATION_CACHE


def save_translation_cache():
    global TRANSLATION_CACHE_DIRTY

    if TRANSLATION_CACHE is None:
        return

    try:
        # Selalu simpan, termasuk cache kosong, agar file tersedia di repo.
        save_json(TRANSLATION_CACHE_FILE, TRANSLATION_CACHE)
        TRANSLATION_CACHE_DIRTY = False
        logging.info("Cache terjemahan tersimpan: %d entri", len(TRANSLATION_CACHE))
    except Exception as error:
        logging.error("Gagal menyimpan cache terjemahan: %s", error)


def get_translation_cache_key(text):
    raw = f"{TRANSLATION_SOURCE_LANGUAGE}|{TRANSLATION_TARGET_LANGUAGE}|{text.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def initialize_translator():
    global TRANSLATION_MODEL, TRANSLATOR_INITIALIZED, TRANSLATOR_FAILED

    if TRANSLATOR_INITIALIZED:
        return TRANSLATION_MODEL is not None
    if TRANSLATOR_FAILED:
        return False

    try:
        import argostranslate.package
        import argostranslate.translate

        from_code = TRANSLATION_SOURCE_LANGUAGE
        to_code = TRANSLATION_TARGET_LANGUAGE

        def find_pair():
            languages = argostranslate.translate.get_installed_languages()
            source = next((x for x in languages if x.code == from_code), None)
            target = next((x for x in languages if x.code == to_code), None)
            return source, target

        source_lang, target_lang = find_pair()
        translation = None

        if source_lang and target_lang:
            translation = source_lang.get_translation(target_lang)

        if translation is None:
            logging.info("Model Argos en-id belum terpasang. Mengunduh model...")
            argostranslate.package.update_package_index()
            packages = argostranslate.package.get_available_packages()

            package = next(
                (p for p in packages if p.from_code == from_code and p.to_code == to_code),
                None
            )
            if package is None:
                raise RuntimeError("Model Argos Inggris-Indonesia tidak ditemukan.")

            download_path = package.download()
            if not download_path:
                raise RuntimeError("Pengunduhan model Argos gagal.")

            argostranslate.package.install_from_path(download_path)

            clear_cache = getattr(
                argostranslate.translate.get_installed_languages,
                "cache_clear",
                None
            )
            if callable(clear_cache):
                clear_cache()

            source_lang, target_lang = find_pair()
            if not source_lang or not target_lang:
                raise RuntimeError("Bahasa en/id tidak terdeteksi setelah instalasi.")

            translation = source_lang.get_translation(target_lang)

        if translation is None or not callable(getattr(translation, "translate", None)):
            raise RuntimeError("Objek penerjemah en-id tidak valid.")

        TRANSLATION_MODEL = translation
        TRANSLATOR_INITIALIZED = True
        logging.info("Argos Translate siap: English -> Indonesian.")
        return True

    except Exception as error:
        TRANSLATOR_FAILED = True
        TRANSLATION_MODEL = None
        logging.exception("Gagal menginisialisasi Argos Translate: %s", error)
        return False


def translate_to_indonesian(text):
    global TRANSLATION_CACHE_DIRTY

    original = str(text or "").strip()
    if not original:
        return original

    cache = load_translation_cache()
    key = get_translation_cache_key(original)

    cached = cache.get(key)
    if isinstance(cached, str) and cached.strip():
        return cached.strip()

    if not initialize_translator() or TRANSLATION_MODEL is None:
        return original

    try:
        translated = str(TRANSLATION_MODEL.translate(original) or "").strip()
        if not translated or translated == original:
            return original

        cache[key] = translated
        TRANSLATION_CACHE_DIRTY = True
        return translated

    except Exception:
        logging.exception("Gagal menerjemahkan: %s", original[:120])
        return original


def translate_article(title, description, language):
    original_title = str(title or "").strip()
    original_description = str(description or "").strip()
    language = str(language or "id").lower().strip()

    if language not in ("en", "eng", "english"):
        return {
            "title": original_title,
            "description": original_description,
            "originalTitle": original_title,
            "originalDescription": original_description,
            "originalLanguage": language,
            "language": language,
            "translated": False,
            "translationStatus": "not_required"
        }

    translated_title = translate_to_indonesian(original_title)
    translated_description = (
        translate_to_indonesian(original_description)
        if original_description else ""
    )

    title_ok = bool(translated_title) and translated_title != original_title
    description_ok = (
        not original_description
        or (
            bool(translated_description)
            and translated_description != original_description
        )
    )

    if title_ok and description_ok:
        status = "success"
    elif title_ok or description_ok:
        status = "partial"
    else:
        status = "failed"

    return {
        "title": translated_title or original_title,
        "description": translated_description or original_description,
        "originalTitle": original_title,
        "originalDescription": original_description,
        "originalLanguage": "en",
        "language": "id" if status == "success" else "en",
        "translated": status == "success",
        "translationStatus": status
    }


# =========================================================
# EKSTRAK GAMBAR
# =========================================================

class ImageHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.jsonld_scripts = []
        self.inside_jsonld = False
        self.script_data = []

    def add_srcset(self, srcset):
        candidates = []
        for item in (srcset or "").split(","):
            parts = item.strip().split()
            if not parts:
                continue
            width = 0
            if len(parts) > 1:
                descriptor = parts[1].lower()
                try:
                    if descriptor.endswith("w"):
                        width = int(descriptor[:-1])
                    elif descriptor.endswith("x"):
                        width = int(float(descriptor[:-1]) * 1000)
                except ValueError:
                    pass
            candidates.append((width, parts[0]))

        candidates.sort(key=lambda x: x[0], reverse=True)
        self.images.extend(url for _, url in candidates)

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
                "og:image", "og:image:url", "og:image:secure_url",
                "twitter:image", "twitter:image:src", "image",
                "thumbnail", "thumbnailurl"
            ):
                self.images.append(attrs.get("content", ""))

        elif tag == "img":
            for key in (
                "src", "data-src", "data-original", "data-lazy-src",
                "data-original-src", "data-lazy", "data-image",
                "data-url", "data-actualsrc", "data-echo"
            ):
                if attrs.get(key):
                    self.images.append(attrs[key])

            self.add_srcset(attrs.get("srcset") or attrs.get("data-srcset"))

        elif tag == "source":
            self.add_srcset(attrs.get("srcset") or attrs.get("data-srcset"))

        elif tag == "link" and "image_src" in attrs.get("rel", "").lower():
            self.images.append(attrs.get("href", ""))

        elif tag == "script" and "application/ld+json" in attrs.get("type", "").lower():
            self.inside_jsonld = True
            self.script_data = []

    def handle_data(self, data):
        if self.inside_jsonld:
            self.script_data.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.inside_jsonld:
            content = "".join(self.script_data).strip()
            if content:
                self.jsonld_scripts.append(content)
            self.inside_jsonld = False
            self.script_data = []


def collect_jsonld_images(data, candidates):
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in ("image", "thumbnailurl", "contenturl", "thumbnail"):
                if isinstance(value, str):
                    candidates.append(value)
                elif isinstance(value, (dict, list)):
                    collect_jsonld_images(value, candidates)
            elif isinstance(value, (dict, list)):
                collect_jsonld_images(value, candidates)
    elif isinstance(data, list):
        for item in data:
            collect_jsonld_images(item, candidates)


def find_valid_image(candidates, base_url=""):
    rejected = ("favicon", "avatar", "placeholder", "default-image", "no-image", "blank.gif")

    for candidate in candidates:
        if not isinstance(candidate, str):
            continue

        candidate = html.unescape(candidate.strip().strip("\"'"))
        if not candidate or candidate.lower().startswith(("data:", "javascript:", "blob:")):
            continue

        if candidate.startswith("//"):
            candidate = "https:" + candidate
        elif base_url:
            candidate = urljoin(base_url, candidate)

        if not valid_http_url(candidate):
            continue

        lower = candidate.lower()
        if any(word in lower for word in rejected):
            continue
        if urlsplit(lower).path.endswith((".svg", ".ico")):
            continue

        return candidate

    return ""


def extract_html_image(page_html, base_url=""):
    if not page_html:
        return ""

    try:
        parser = ImageHTMLParser()
        parser.feed(page_html)

        candidates = list(parser.images)
        for script in parser.jsonld_scripts:
            try:
                collect_jsonld_images(json.loads(script), candidates)
            except (ValueError, TypeError):
                continue

        return find_valid_image(candidates, base_url)

    except Exception as error:
        logging.debug("Gagal membaca gambar HTML: %s", error)
        return ""


def extract_image_from_article_page(article_url):
    if not valid_http_url(article_url):
        return ""

    try:
        response = requests.get(
            article_url,
            headers=PAGE_HEADERS,
            timeout=IMAGE_TIMEOUT,
            allow_redirects=True
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and "html" not in content_type:
            return ""

        return extract_html_image(response.text, response.url)

    except Exception as error:
        logging.warning("Gagal mengambil gambar %s: %s", article_url, error)
        return ""


def extract_image(entry, article_url=""):
    candidates = []

    def collect_media(value):
        if isinstance(value, dict):
            image_url = value.get("url") or value.get("href")
            if image_url:
                candidates.append(image_url)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    collect_media(nested)
        elif isinstance(value, list):
            for item in value:
                collect_media(item)

    for field in ("media_content", "media_thumbnail", "media_group"):
        collect_media(entry.get(field, []))

    for item in entry.get("enclosures", []):
        if isinstance(item, dict):
            candidates.append(item.get("href") or item.get("url") or "")

    for item in entry.get("links", []):
        if isinstance(item, dict) and item.get("rel") == "enclosure":
            candidates.append(item.get("href", ""))

    html_fields = []
    for item in entry.get("content", []):
        if isinstance(item, dict):
            html_fields.append(item.get("value", ""))

    html_fields.extend([entry.get("summary", ""), entry.get("description", "")])

    for content in html_fields:
        image = extract_html_image(str(content or ""), article_url)
        if image:
            candidates.append(image)

    image = find_valid_image(candidates, article_url)
    return image or extract_image_from_article_page(article_url)


def refresh_missing_images(articles):
    missing = [
        article for article in articles
        if not article.get("image") and article.get("originalUrl")
    ]

    if not missing:
        logging.info("Tidak ada gambar kosong yang perlu diperiksa ulang.")
        return articles

    logging.info("Memeriksa ulang gambar untuk %d berita.", len(missing))
    recovered = 0

    for index, article in enumerate(missing, start=1):
        logging.info(
            "[%d/%d] Mencari gambar: %s",
            index, len(missing), article.get("title", "")[:90]
        )
        image = extract_image_from_article_page(article.get("originalUrl", ""))
        if image:
            article["image"] = image
            recovered += 1

    logging.info("Pemulihan gambar: %d berhasil dari %d.", recovered, len(missing))
    return articles


# =========================================================
# FILTER BERITA SEPAK BOLA / OLAHRAGA
# =========================================================

FOOTBALL_KEYWORDS = {
    # Bahasa Inggris
    "football", "soccer", "premier league",
    "champions league", "world cup",
    "fifa", "uefa", "afc",
    "match", "matches", "kickoff",
    "goalkeeper", "striker", "midfielder",
    "defender", "penalty shootout",
    "transfer window", "football club",
    "soccer club", "footballer",

    # Bahasa Indonesia
    "sepak bola", "liga", "klub",
    "pemain", "pertandingan",
    "gol", "penalti", "kiper",
    "striker", "gelandang", "bek",
    "transfer pemain", "bursa transfer",
    "piala dunia", "liga champions",
    "tim nasional", "kesebelasan",
    "pelatih sepak bola",

    # Istilah tambahan
    "al nassr", "al hilal",
    "al ittihad", "al ahly",
    "real madrid", "barcelona",
    "manchester united",
    "manchester city",
    "liverpool fc", "arsenal fc",
    "chelsea fc", "psg",
    "inter miami", "juventus fc"
}


def is_football_news(title, description):
    """
    Menolak berita sepak bola tanpa melihat negara sumber.
    Judul menjadi indikator utama.
    Deskripsi menggunakan minimal 2 keyword.
    """

    title_text = normalize_text(title or "")
    description_text = normalize_text(description or "")

    def contains_keyword(text, keyword):
        pattern = (
            r"(?<!\w)"
            + re.escape(normalize_text(keyword))
            + r"(?!\w)"
        )
        return bool(
            re.search(pattern, text, flags=re.UNICODE)
        )

    # 1. Jika judul mengandung keyword sepak bola,
    # langsung tolak.
    for keyword in FOOTBALL_KEYWORDS:
        if contains_keyword(title_text, keyword):
            return True

    # 2. Jika deskripsi mengandung minimal 2 keyword,
    # anggap sebagai berita sepak bola.
    matches = sum(
        1 for keyword in FOOTBALL_KEYWORDS
        if contains_keyword(description_text, keyword)
    )

    return matches >= 2


# =========================================================
# KATEGORISASI: REGEX KATA/FRASA UTUH
# =========================================================

def categorize(title, description, categories):
    text = normalize_text(f"{title or ''} {description or ''}")
    scores = {}
    matched = {}

    for category, keywords in categories.items():
        if not isinstance(keywords, list):
            continue

        score = 0
        found = []

        for raw_keyword in keywords:
            keyword = normalize_text(raw_keyword)
            if not keyword:
                continue

            # Hindari substring: "dai" tidak cocok dengan "pegadaian".
            pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
            if re.search(pattern, text, flags=re.UNICODE):
                found.append(str(raw_keyword).strip())
                score += 3 if len(keyword.split()) > 1 else 2

        if found:
            scores[category] = score
            matched[category] = found

    if not scores:
        return None, []

    best = max(scores, key=scores.get)
    tags = sorted(matched, key=lambda item: scores[item], reverse=True)
    return best, tags


# =========================================================
# PENGAMBILAN BERITA RSS
# =========================================================

def fetch_source(source, categories):
    result = {
        "name": source.get("name", "Unknown"),
        "status": "error",
        "raw": 0,
        "accepted": 0,
        "filtered": 0,
        "translated": 0,
        "translationPartial": 0,
        "translationFailed": 0,
        "error": None
    }

    articles = []

    try:
        source_url = source.get("url", "")
        if not valid_http_url(source_url):
            raise ValueError("URL RSS tidak valid.")

        response = requests.get(
            source_url,
            headers=RSS_HEADERS,
            timeout=TIMEOUT
        )
        response.raise_for_status()

        feed = parse_rss_safely(response.content, response.encoding)
        entries = feed.entries
        result["raw"] = len(entries)

        if not entries:
            reason = getattr(feed, "bozo_exception", None)
            raise ValueError(
                "RSS tidak menghasilkan artikel yang dapat dibaca."
                + (f" Detail: {reason}" if reason else "")
            )

        result["status"] = "success"
        source_language = str(source.get("language", "id")).lower().strip()

        for entry in entries:
            original_title = clean_text(entry.get("title", ""))
            original_description = clean_text(
                entry.get("summary", "") or entry.get("description", "")
            )
            url = normalize_url(entry.get("link", ""))

            if not original_title or not url:
                result["filtered"] += 1
                continue

            # FILTER SEPAK BOLA
            if is_football_news(
                original_title,
                original_description
            );
            result["filtered"] += 1
            logging.info(
                "DIBUANG (SEPAK BOLA): %s",
                original_titile[:150]
            )
            continue

            # PENTING: terjemahkan terlebih dahulu, baru kategorisasi.
            translation = translate_article(
                original_title,
                original_description,
                source_language
            )

            title = translation["title"]
            description = translation["description"]

            status = translation["translationStatus"]
            if status == "success":
                result["translated"] += 1
            elif status == "partial":
                result["translationPartial"] += 1
            elif status == "failed":
                result["translationFailed"] += 1

            category, tags = categorize(title, description, categories)
            if not category:
                result["filtered"] += 1
                continue

            image_url = extract_image(entry, url)
            source_name = source.get("name", "Unknown")
            source_country = source.get("country", source.get("sourceCountry", ""))

            article = {
                "id": generate_id(url),
                "title": title,
                "description": description[:2000],
                "originalTitle": translation["originalTitle"],
                "originalDescription": translation["originalDescription"][:2000],
                "translated": translation["translated"],
                "translationStatus": translation["translationStatus"],
                "originalLanguage": translation["originalLanguage"],
                "language": translation["language"],
                "image": image_url,
                "category": category,
                "tags": tags,
                "source": source_name,
                "sources": [source_name],
                "country": source_country,
                "sourceCountry": source_country,
                "region": source.get("region", ""),
                "publishedAt": get_published_date(entry),
                "originalUrl": url
            }

            articles.append(article)

        result["accepted"] = len(articles)

        logging.info(
            "%s | RSS: %d | Lolos: %d | Disaring: %d | "
            "Terjemahan sukses: %d | Sebagian: %d | Gagal: %d",
            result["name"], result["raw"], result["accepted"],
            result["filtered"], result["translated"],
            result["translationPartial"], result["translationFailed"]
        )

        return articles, result

    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
        logging.exception("Sumber RSS %s gagal: %s", result["name"], error)
        return [], result


# =========================================================
# DEDUPLIKASI BERITA
# =========================================================

STOPWORDS = {
    "yang", "dan", "di", "ke", "dari", "untuk", "pada", "dengan",
    "ini", "itu", "dalam", "oleh", "karena", "akan", "telah",
    "the", "a", "an", "of", "to", "in", "on", "at", "for",
    "with", "and", "is", "are", "as", "by", "from"
}

WORD_NORMALIZATION = {
    "killed": "kill", "kills": "kill", "killing": "kill",
    "attacked": "attack", "attacks": "attack", "attacking": "attack",
    "wounded": "wound", "wounds": "wound", "wounding": "wound",
    "injured": "injure", "injures": "injure", "injuring": "injure",
    "flooded": "flood", "floods": "flood", "flooding": "flood",
    "died": "die", "dies": "die", "dying": "die",
    "burned": "burn", "burns": "burn", "burning": "burn",
    "collapsed": "collapse", "collapses": "collapse", "collapsing": "collapse",
    "arrested": "arrest", "arrests": "arrest", "arresting": "arrest",
    "detained": "detain", "detains": "detain", "detaining": "detain",
    "bombed": "bomb", "bombs": "bomb", "bombing": "bomb",
    "struck": "strike", "strikes": "strike", "striking": "strike",
    "rescued": "rescue", "rescues": "rescue", "rescuing": "rescue"
}

EVENT_ACTIONS = {
    "kill", "attack", "bomb", "strike", "wound", "injure",
    "arrest", "detain", "flood", "fire", "burn", "collapse",
    "die", "rescue", "release", "capture", "evacuate",
    "explode", "destroy"
}


def news_tokens(value):
    tokens = normalize_text(value).split()
    return {
        WORD_NORMALIZATION.get(token, token)
        for token in tokens
        if len(token) >= 3 and token not in STOPWORDS
    }


def token_jaccard(a, b):
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def token_overlap(a, b):
    smaller = min(len(a), len(b))
    return len(a & b) / smaller if smaller else 0.0


def is_within_duplicate_window(article_a, article_b):
    date_a = get_article_datetime(article_a)
    date_b = get_article_datetime(article_b)

    if not date_a or not date_b:
        return False

    seconds = abs((date_a - date_b).total_seconds())
    return seconds <= DUPLICATE_TIME_WINDOW_HOURS * 3600


def is_same_news(article_a, article_b):
    title_a = normalize_text(article_a.get("title", ""))
    title_b = normalize_text(article_b.get("title", ""))

    if not title_a or not title_b:
        return False

    if not is_within_duplicate_window(article_a, article_b):
        return False

    tokens_a = news_tokens(article_a.get("title", ""))
    tokens_b = news_tokens(article_b.get("title", ""))
    common_title = tokens_a & tokens_b

    if not tokens_a or not tokens_b or len(common_title) < 3:
        return False

    jaccard = token_jaccard(tokens_a, tokens_b)
    overlap = token_overlap(tokens_a, tokens_b)
    sequence = SequenceMatcher(None, title_a, title_b).ratio()

    if sequence >= TITLE_SIMILARITY_THRESHOLD and len(common_title) >= 3:
        return True

    if jaccard >= 0.68 and len(common_title) >= 4:
        return True

    shared_actions = common_title & EVENT_ACTIONS
    if len(common_title) >= 5 and overlap >= 0.65 and shared_actions:
        return True

    desc_a = news_tokens(article_a.get("description", ""))
    desc_b = news_tokens(article_b.get("description", ""))
    common_desc = desc_a & desc_b

    if (
        len(common_desc) >= 8
        and token_jaccard(desc_a, desc_b) >= DESCRIPTION_SIMILARITY_THRESHOLD
        and len(common_title) >= 2
        and jaccard >= 0.25
    ):
        return True

    combined_a = tokens_a | desc_a
    combined_b = tokens_b | desc_b
    return (
        len(combined_a & combined_b) >= 10
        and token_jaccard(combined_a, combined_b) >= 0.50
        and len(common_title) >= 3
        and overlap >= 0.50
    )


def article_quality(article):
    return (
        1 if article.get("image") else 0,
        len(clean_text(article.get("description", ""))),
        len(clean_text(article.get("title", "")))
    )


def translation_succeeded(article):
    return (
        article.get("translated") is True
        and str(article.get("language", "")).lower() == "id"
        and article.get("translationStatus") == "success"
    )


def merge_duplicate_metadata(preferred, other):
    result = dict(preferred)

    # Jika salah satu versi memiliki terjemahan valid, prioritaskan versi itu.
    if translation_succeeded(other) and not translation_succeeded(preferred):
        for key in (
            "title", "description", "originalTitle", "originalDescription",
            "originalLanguage", "language", "translated", "translationStatus"
        ):
            if key in other:
                result[key] = other[key]

    # Pilih deskripsi yang lebih lengkap tanpa menimpa terjemahan sukses.
    if translation_succeeded(result):
        if len(clean_text(other.get("description", ""))) > len(
            clean_text(result.get("description", ""))
        ):
            # Jangan menukar teks beda bahasa; hanya isi jika bahasa sama.
            if other.get("language") == "id":
                result["description"] = other.get("description", "")
    elif len(clean_text(other.get("description", ""))) > len(
        clean_text(result.get("description", ""))
    ):
        result["description"] = other.get("description", "")

    for field in ("image", "originalUrl", "originalTitle", "originalDescription"):
        if not result.get(field) and other.get(field):
            result[field] = other[field]

    if translation_succeeded(result):
        result["translated"] = True
        result["translationStatus"] = "success"
        result["language"] = "id"
    else:
        original_language = str(
            result.get("originalLanguage", result.get("language", "id")) or "id"
        ).lower()
        if original_language in ("en", "eng", "english"):
            result["originalLanguage"] = "en"
            result["language"] = "en"
            result["translated"] = False
            if result.get("translationStatus") not in ("partial", "failed"):
                result["translationStatus"] = "failed"

    tags = []
    for tag in (result.get("tags") or []) + (other.get("tags") or []):
        if tag and tag not in tags:
            tags.append(tag)
    result["tags"] = tags

    sources = []
    for source in (
        (result.get("sources") or []) + [result.get("source", "")]
        + (other.get("sources") or []) + [other.get("source", "")]
    ):
        if source and source not in sources:
            sources.append(source)
    if sources:
        result["sources"] = sources

    return result


# =========================================================
# MEMUAT DAN MENGGABUNGKAN BERITA LAMA/BARU
# =========================================================

def load_previous_articles():
    previous = load_json(OUTPUT_FILE, default={})
    if not isinstance(previous, dict):
        return []

    articles = previous.get("articles", [])
    return articles if isinstance(articles, list) else []


def merge_articles(new_articles, old_articles):
    # URL identik digabung terlebih dahulu.
    url_map = {}

    for article in old_articles + new_articles:
        if not isinstance(article, dict):
            continue

        url = normalize_url(article.get("originalUrl", ""))
        if not url:
            continue

        article = dict(article)
        article["originalUrl"] = url
        article["id"] = generate_id(url)

        existing = url_map.get(url)
        if existing:
            if article_quality(article) > article_quality(existing):
                article = merge_duplicate_metadata(article, existing)
            else:
                article = merge_duplicate_metadata(existing, article)

        url_map[url] = article

    # Retensi berita: hanya simpan artikel dalam rentang 7 hari terakhir.
    # Artikel dengan tanggal tidak valid juga dibuang agar tidak menumpuk selamanya.
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    candidates = []
    expired_count = 0
    invalid_date_count = 0

    for article in url_map.values():
        date = get_article_datetime(article)

        if date is None:
            invalid_date_count += 1
            continue

        if date < cutoff:
            expired_count += 1
            continue

        candidates.append(article)

    logging.info(
        "Pembersihan berita: %d kedaluwarsa (lebih dari %d hari), "
        "%d tanggal tidak valid, %d dipertahankan.",
        expired_count, MAX_AGE_DAYS, invalid_date_count, len(candidates)
    )

    candidates.sort(key=lambda x: x.get("publishedAt", ""), reverse=True)

    # Periksa ulang artikel lama yang masih berbahasa Inggris.
    checked = succeeded = partial = failed = skipped = 0

    for article in candidates:
        language = str(
            article.get("originalLanguage", article.get("language", "id")) or "id"
        ).lower().strip()

        if language not in ("en", "eng", "english"):
            skipped += 1
            continue

        if translation_succeeded(article):
            skipped += 1
            continue

        original_title = str(
            article.get("originalTitle") or article.get("title") or ""
        ).strip()
        original_description = str(
            article.get("originalDescription") or article.get("description") or ""
        ).strip()

        if not original_title:
            failed += 1
            continue

        checked += 1
        translated = translate_article(
            original_title, original_description, "en"
        )

        article["title"] = translated["title"]
        article["description"] = translated["description"][:2000]
        article["originalTitle"] = translated["originalTitle"]
        article["originalDescription"] = translated["originalDescription"][:2000]
        article["originalLanguage"] = "en"
        article["language"] = translated["language"]
        article["translated"] = translated["translated"]
        article["translationStatus"] = translated["translationStatus"]

        if translated["translationStatus"] == "success":
            succeeded += 1
        elif translated["translationStatus"] == "partial":
            partial += 1
        else:
            failed += 1

    logging.info(
        "Pemeriksaan berita lama: diperiksa=%d, sukses=%d, sebagian=%d, gagal=%d, dilewati=%d",
        checked, succeeded, partial, failed, skipped
    )

    save_translation_cache()

    # Dedup berdasarkan ID.
    id_map = {}
    for article in candidates:
        article_id = article.get("id") or generate_id(article.get("originalUrl", ""))
        article["id"] = article_id

        if article_id in id_map:
            existing = id_map[article_id]
            if article_quality(article) > article_quality(existing):
                id_map[article_id] = merge_duplicate_metadata(article, existing)
            else:
                id_map[article_id] = merge_duplicate_metadata(existing, article)
        else:
            id_map[article_id] = article

    valid_articles = sorted(
        id_map.values(),
        key=lambda x: x.get("publishedAt", ""),
        reverse=True
    )

    # Deduplikasi peristiwa lintas media.
    unique = []
    duplicate_count = 0

    for article in valid_articles:
        match_index = None

        for index, existing in enumerate(unique):
            if is_same_news(article, existing):
                match_index = index
                break

        if match_index is None:
            unique.append(article)
        else:
            duplicate_count += 1
            existing = unique[match_index]

            if article_quality(article) > article_quality(existing):
                unique[match_index] = merge_duplicate_metadata(article, existing)
            else:
                unique[match_index] = merge_duplicate_metadata(existing, article)

    unique.sort(key=lambda x: x.get("publishedAt", ""), reverse=True)
    final_articles = unique[:MAX_ARTICLES]

    english_remaining = sum(
        1 for item in final_articles
        if str(item.get("language", "")).lower() in ("en", "eng", "english")
    )

    logging.info("Artikel setelah URL dedup: %d", len(candidates))
    logging.info("Duplikasi peristiwa digabung: %d", duplicate_count)
    logging.info("Total berita unik: %d", len(final_articles))
    logging.info("Berita Inggris yang tersisa: %d", english_remaining)

    return final_articles


# =========================================================
# MAIN
# =========================================================

def main():
    logging.info("========== MEMULAI UPDATE KABAR UMMAH ==========")

    sources = load_json(SOURCES_FILE, default=[])
    categories = load_json(CATEGORIES_FILE, default={})

    if not isinstance(sources, list) or not sources:
        raise RuntimeError("Daftar sumber RSS kosong atau tidak valid.")

    if not isinstance(categories, dict) or not categories:
        raise RuntimeError("Daftar kategori kosong atau tidak valid.")

    load_translation_cache()
    previous_articles = load_previous_articles()

    logging.info("Berita lama ditemukan: %d", len(previous_articles))

    all_new_articles = []
    source_stats = []

    for index, source in enumerate(sources, start=1):
        logging.info(
            "Memproses sumber %d/%d: %s",
            index, len(sources), source.get("name", "Unknown")
        )
        articles, stats = fetch_source(source, categories)
        all_new_articles.extend(articles)
        source_stats.append(stats)

    successful = sum(1 for item in source_stats if item["status"] == "success")
    failed_sources = len(source_stats) - successful

    logging.info(
        "RSS selesai: %d berhasil, %d gagal.",
        successful, failed_sources
    )

    # Perlindungan: jangan ubah news.json jika semua sumber gagal.
    if successful == 0:
        for item in source_stats:
            logging.error(
                "%s | status=%s | error=%s",
                item.get("name"), item.get("status"), item.get("error")
            )
        save_translation_cache()
        raise RuntimeError(
            "Semua sumber RSS gagal. news.json lama dipertahankan."
        )

    final_articles = merge_articles(all_new_articles, previous_articles)
    final_articles = refresh_missing_images(final_articles)
    save_translation_cache()

    images_found = sum(1 for item in final_articles if item.get("image"))
    images_missing = len(final_articles) - images_found

    translated_count = sum(
        1 for item in final_articles
        if item.get("translationStatus") == "success"
    )
    partial_count = sum(
        1 for item in final_articles
        if item.get("translationStatus") == "partial"
    )
    failed_translation_count = sum(
        1 for item in final_articles
        if item.get("translationStatus") == "failed"
    )

    status = "success" if failed_sources == 0 else "partial"

    output = {
        "status": status,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "total": len(final_articles),
        "newArticles": len(all_new_articles),
        "sourcesChecked": len(sources),
        "sourcesSucceeded": successful,
        "sourcesFailed": failed_sources,
        "sourceStats": source_stats,
        "translation": {
            "engine": "Argos Translate",
            "sourceLanguage": "en",
            "targetLanguage": "id",
            "translatedArticles": translated_count,
            "partialArticles": partial_count,
            "failedArticles": failed_translation_count,
            "cacheEntries": len(load_translation_cache())
        },
        "images": {
            "found": images_found,
            "missing": images_missing
        },
        "articles": final_articles
    }

    save_json(OUTPUT_FILE, output)

    logging.info("========== UPDATE SELESAI ==========")
    logging.info("Status                 : %s", status)
    logging.info("Sumber RSS berhasil    : %d/%d", successful, len(sources))
    logging.info("Berita baru lolos      : %d", len(all_new_articles))
    logging.info("Berita lama            : %d", len(previous_articles))
    logging.info("Total berita tersimpan : %d", len(final_articles))
    logging.info("Terjemahan berhasil    : %d", translated_count)
    logging.info("Terjemahan sebagian    : %d", partial_count)
    logging.info("Terjemahan gagal       : %d", failed_translation_count)
    logging.info("Gambar ditemukan       : %d", images_found)
    logging.info("Gambar kosong          : %d", images_missing)
    logging.info("====================================")


if __name__ == "__main__":
    main()
