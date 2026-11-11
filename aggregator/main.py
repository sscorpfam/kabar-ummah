
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

TRANSLATION_CACHE_FILE = os.path.join(
    BASE_DIR, "data", "translation_cache.json"
)

MAX_ARTICLES = 500
MAX_AGE_DAYS = 90

TIMEOUT = 25
IMAGE_TIMEOUT = 12

DUPLICATE_TIME_WINDOW_HOURS = 72

TITLE_SIMILARITY_THRESHOLD = 0.82
DESCRIPTION_SIMILARITY_THRESHOLD = 0.48

TRANSLATION_SOURCE_LANGUAGE = "en"
TRANSLATION_TARGET_LANGUAGE = "id"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"
}

RSS_HEADERS = {
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
# TRANSLATION STATE
# =========================================================

TRANSLATOR_READY = False
TRANSLATOR_ERROR = None

TRANSLATION_CACHE = None
TRANSLATION_CACHE_DIRTY = False

TRANSLATION_MODEL = None


# =========================================================
# HTML IMAGE PARSER
# =========================================================

class ImageHTMLParser(HTMLParser):

    def __init__(self):

        super().__init__()

        self.images = []
        self.jsonld_scripts = []

        self._inside_jsonld = False
        self._script_data = []

    def handle_starttag(self, tag, attrs):

        attrs = dict(attrs)
        tag = tag.lower()

        # META IMAGE

        if tag == "meta":

            key = (
                attrs.get("property", "")
                or attrs.get("name", "")
                or attrs.get("itemprop", "")
            ).lower()

            if key in (
                "og:image",
                "og:image:url",
                "og:image:secure_url",
                "twitter:image",
                "twitter:image:src",
                "image",
                "thumbnail",
                "thumbnailurl"
            ):

                self.images.append(
                    attrs.get("content", "")
                )

        # IMG ELEMENT

        elif tag == "img":

            image_attributes = (
                "src",
                "data-src",
                "data-original",
                "data-lazy-src",
                "data-original-src",
                "data-lazy",
                "data-image",
                "data-url",
                "data-actualsrc",
                "data-echo",
                "data-fallback-src"
            )

            for key in image_attributes:

                value = attrs.get(key)

                if value:
                    self.images.append(value)

            srcset = (
                attrs.get("srcset", "")
                or attrs.get("data-srcset", "")
            )

            if srcset:
                self.add_srcset(srcset)

        # SOURCE ELEMENT

        elif tag == "source":

            srcset = (
                attrs.get("srcset", "")
                or attrs.get("data-srcset", "")
            )

            if srcset:
                self.add_srcset(srcset)

        # LINK IMAGE

        elif tag == "link":

            rel = attrs.get("rel", "").lower()

            if "image_src" in rel:

                self.images.append(
                    attrs.get("href", "")
                )

        # JSON-LD

        elif tag == "script":

            script_type = attrs.get(
                "type", ""
            ).lower()

            if "application/ld+json" in script_type:

                self._inside_jsonld = True
                self._script_data = []

    def add_srcset(self, srcset):

        candidates = []

        for item in srcset.split(","):

            parts = item.strip().split()

            if not parts:
                continue

            url = parts[0]
            width = 0

            if len(parts) > 1:

                descriptor = parts[1].lower()

                try:

                    if descriptor.endswith("w"):

                        width = int(
                            descriptor[:-1]
                        )

                    elif descriptor.endswith("x"):

                        width = int(
                            float(descriptor[:-1]) * 1000
                        )

                except ValueError:
                    pass

            candidates.append(
                (width, url)
            )

        candidates.sort(
            key=lambda item: item[0],
            reverse=True
        )

        for _, url in candidates:
            self.images.append(url)

    def handle_data(self, data):

        if self._inside_jsonld:
            self._script_data.append(data)

    def handle_endtag(self, tag):

        if (
            tag.lower() == "script"
            and self._inside_jsonld
        ):

            content = "".join(
                self._script_data
            ).strip()

            if content:
                self.jsonld_scripts.append(content)

            self._inside_jsonld = False
            self._script_data = []


# =========================================================
# RSS PARSER
# =========================================================

def parse_rss_safely(content, response_encoding=None):
    """
    Membaca RSS dengan beberapa percobaan encoding.
    Membersihkan karakter kontrol ilegal pada XML
    jika feedparser menemukan XML yang tidak valid.
    """

    feed = feedparser.parse(content)

    if not feed.bozo and feed.entries:
        return feed

    logging.warning(
        "RSS memiliki format XML bermasalah. "
        "Mencoba memperbaiki encoding dan karakter XML."
    )

    # Percobaan kedua: deteksi encoding.

    try:

        if response_encoding:

            text = content.decode(
                response_encoding,
                errors="replace"
            )

        else:

            text = content.decode(
                "utf-8",
                errors="replace"
            )

        text = re.sub(
            r"[\x00-\x08\x0B\x0C\x0E-\x1F]",
            "",
            text
        )

        text = re.sub(
            r'(<\?xml[^>]*encoding\s*=\s*)["\'][^"\']+["\']',
            r'\1"UTF-8"',
            text,
            count=1,
            flags=re.IGNORECASE
        )

        repaired = text.encode("utf-8")

        feed = feedparser.parse(repaired)

        if feed.entries:

            logging.info(
                "RSS berhasil dibaca setelah perbaikan XML."
            )

            return feed

    except Exception as error:

        logging.warning(
            "Percobaan perbaikan encoding gagal: %s",
            error
        )

    # Percobaan ketiga.

    try:

        encoding = response_encoding or "utf-8"

        text = content.decode(
            encoding,
            errors="replace"
        )

        text = re.sub(
            r"[\x00-\x08\x0B\x0C\x0E-\x1F]",
            "",
            text
        )

        feed = feedparser.parse(
            text.encode("utf-8")
        )

        if feed.entries:
            return feed

    except Exception as error:

        logging.error(
            "Semua percobaan membaca RSS gagal: %s",
            error
        )

    return feed


# =========================================================
# JSON FUNCTIONS
# =========================================================

def load_json(path, default=None):

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as file:

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

    with open(
        temp_path,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        temp_path,
        path
    )


# =========================================================
# ARGOS TRANSLATE
# ENGLISH TO INDONESIAN
# =========================================================

def load_translation_cache():

    global TRANSLATION_CACHE

    if TRANSLATION_CACHE is not None:
        return TRANSLATION_CACHE

    data = load_json(
        TRANSLATION_CACHE_FILE,
        default={}
    )

    if isinstance(data, dict):

        TRANSLATION_CACHE = data

    else:

        TRANSLATION_CACHE = {}

    logging.info(
        "Cache terjemahan dimuat: %d entri",
        len(TRANSLATION_CACHE)
    )

    return TRANSLATION_CACHE


def save_translation_cache():

    global TRANSLATION_CACHE_DIRTY

    if TRANSLATION_CACHE is None:
        return

    if not TRANSLATION_CACHE_DIRTY:
        return

    try:

        save_json(
            TRANSLATION_CACHE_FILE,
            TRANSLATION_CACHE
        )

        TRANSLATION_CACHE_DIRTY = False

        logging.info(
            "Cache terjemahan berhasil disimpan: %d entri",
            len(TRANSLATION_CACHE)
        )

    except Exception as error:

        logging.error(
            "Gagal menyimpan cache terjemahan: %s",
            error
        )


def get_translation_cache_key(text):

    content = (
        TRANSLATION_SOURCE_LANGUAGE
        + "|"
        + TRANSLATION_TARGET_LANGUAGE
        + "|"
        + text.strip()
    )

    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()



def initialize_translator():

    global TRANSLATION_MODEL

    # ---------------------------------------------
    # 1. Periksa apakah translator sudah tersedia
    # ---------------------------------------------

    if TRANSLATION_MODEL is not None:
        return True

    try:

        import argostranslate.package
        import argostranslate.translate

        logging.info(
            "Memulai inisialisasi Argos Translate..."
        )

        from_code = "en"
        to_code = "id"

        # -----------------------------------------
        # 2. Periksa bahasa yang sudah terpasang
        # -----------------------------------------

        installed_languages = (
            argostranslate.translate.get_installed_languages()
        )

        from_lang = next(
            (
                lang for lang in installed_languages
                if lang.code == from_code
            ),
            None
        )

        to_lang = next(
            (
                lang for lang in installed_languages
                if lang.code == to_code
            ),
            None
        )

        translation = None

        # -----------------------------------------
        # 3. Cari pasangan bahasa yang sudah tersedia
        # -----------------------------------------

        if from_lang is not None and to_lang is not None:

            translation = from_lang.get_translation(
                to_lang
            )

        # -----------------------------------------
        # 4. Jika model belum tersedia, instal model
        # -----------------------------------------

        if translation is None:

            logging.info(
                "Model Inggris-Indonesia belum tersedia. "
                "Memulai pengunduhan model..."
            )

            # Perbarui daftar model Argos.

            argostranslate.package.update_package_index()

            available_packages = (
                argostranslate.package.get_available_packages()
            )

            # Cari model en -> id.

            available_package = next(
                (
                    package
                    for package in available_packages
                    if package.from_code == from_code
                    and package.to_code == to_code
                ),
                None
            )

            if available_package is None:

                raise RuntimeError(
                    "Model penerjemah Inggris-Indonesia "
                    "tidak ditemukan pada indeks Argos."
                )

            logging.info(
                "Model ditemukan: %s -> %s",
                from_code,
                to_code
            )

            # Unduh model.

            download_path = available_package.download()

            if not download_path:

                raise RuntimeError(
                    "Pengunduhan model Argos gagal."
                )

            logging.info(
                "Model berhasil diunduh. "
                "Memulai instalasi..."
            )

            # Instal model.

            argostranslate.package.install_from_path(
                download_path
            )

            logging.info(
                "Instalasi model selesai."
            )

            # -------------------------------------
            # 5. Muat ulang daftar bahasa
            # -------------------------------------

            # Argos menggunakan cache pada fungsi ini.
            # Bersihkan cache agar model baru terdeteksi.

            clear_cache = getattr(
                argostranslate.translate.get_installed_languages,
                "cache_clear",
                None
            )

            if callable(clear_cache):
                clear_cache()

            # Ambil kembali bahasa yang sudah terpasang.

            installed_languages = (
                argostranslate.translate.get_installed_languages()
            )

            from_lang = next(
                (
                    lang for lang in installed_languages
                    if lang.code == from_code
                ),
                None
            )

            to_lang = next(
                (
                    lang for lang in installed_languages
                    if lang.code == to_code
                ),
                None
            )

            if from_lang is None or to_lang is None:

                raise RuntimeError(
                    "Model sudah diinstal, tetapi bahasa "
                    "Inggris atau Indonesia tidak terdeteksi."
                )

            # -------------------------------------
            # 6. Ambil objek penerjemah
            # -------------------------------------

            translation = from_lang.get_translation(
                to_lang
            )

        # -----------------------------------------
        # 7. Validasi objek penerjemah
        # -----------------------------------------

        if translation is None:

            raise RuntimeError(
                "Pasangan penerjemah en -> id "
                "tidak berhasil dibuat."
            )

        if not callable(
            getattr(translation, "translate", None)
        ):

            raise RuntimeError(
                "Objek penerjemah tidak memiliki "
                "metode translate()."
            )

        # -----------------------------------------
        # 8. Simpan translator secara global
        # -----------------------------------------

        TRANSLATION_MODEL = translation

        logging.info(
            "========================================"
        )

        logging.info(
            "Argos Translate berhasil diinisialisasi."
        )

        logging.info(
            "Bahasa sumber  : Inggris (en)"
        )

        logging.info(
            "Bahasa tujuan  : Indonesia (id)"
        )

        logging.info(
            "Translator siap digunakan."
        )

        logging.info(
            "========================================"
        )

        return True

    except Exception:

        TRANSLATION_MODEL = None

        logging.exception(
            "Gagal menginisialisasi penerjemah Argos."
        )

        return False




def translate_to_indonesian(text):

    global TRANSLATION_CACHE_DIRTY

    # ---------------------------------------------
    # 1. Validasi teks
    # ---------------------------------------------

    if text is None:
        return text

    original_text = str(text).strip()

    if not original_text:
        return original_text

    # ---------------------------------------------
    # 2. Muat cache terjemahan
    # ---------------------------------------------

    try:

        cache = load_translation_cache()

        if not isinstance(cache, dict):
            cache = {}

    except Exception:

        logging.exception(
            "Gagal memuat cache terjemahan."
        )

        cache = {}

    # ---------------------------------------------
    # 3. Buat kunci cache
    # ---------------------------------------------

    try:

        cache_key = get_translation_cache_key(
            original_text
        )

    except Exception:

        logging.exception(
            "Gagal membuat cache key terjemahan."
        )

        return original_text

    # ---------------------------------------------
    # 4. Periksa cache terlebih dahulu
    # ---------------------------------------------

    cached_translation = cache.get(
        cache_key
    )

    if (
        isinstance(cached_translation, str)
        and cached_translation.strip()
    ):

        logging.debug(
            "Terjemahan ditemukan di cache."
        )

        return cached_translation.strip()

    # ---------------------------------------------
    # 5. Inisialisasi penerjemah
    # ---------------------------------------------

    try:

        translator_ready = initialize_translator()

    except Exception:

        logging.exception(
            "Terjadi kesalahan saat menginisialisasi "
            "Argos Translate."
        )

        return original_text

    if not translator_ready or TRANSLATION_MODEL is None:

        logging.error(
            "Translator tidak tersedia. "
            "Teks tidak dapat diterjemahkan."
        )

        return original_text

    # ---------------------------------------------
    # 6. Proses penerjemahan
    # ---------------------------------------------

    try:

        translated_text = TRANSLATION_MODEL.translate(
            original_text
        )

        translated_text = str(
            translated_text or ""
        ).strip()

        # -----------------------------------------
        # 7. Validasi hasil terjemahan
        # -----------------------------------------

        if not translated_text:

            logging.warning(
                "Hasil terjemahan kosong. "
                "Teks asli akan digunakan."
            )

            return original_text

        if translated_text == original_text:

            logging.warning(
                "Hasil terjemahan sama dengan teks asli. "
                "Periksa model bahasa yang digunakan."
            )

            return original_text

        # -----------------------------------------
        # 8. Simpan hasil ke cache
        # -----------------------------------------

        cache[cache_key] = translated_text

        TRANSLATION_CACHE_DIRTY = True

        logging.debug(
            "Berhasil menerjemahkan teks: %s",
            original_text[:100]
        )

        return translated_text

    # ---------------------------------------------
    # 9. Tangani kesalahan penerjemahan
    # ---------------------------------------------

    except Exception:

        logging.exception(
            "Gagal menerjemahkan teks: %s",
            original_text[:150]
        )

        return original_text



def translate_article(title, description, language):

    original_title = title
    original_description = description

    language = str(
        language or "id"
    ).lower().strip()

    # ---------------------------------------------
    # Hanya terjemahkan sumber berbahasa Inggris
    # ---------------------------------------------

    if language not in (
        "en",
        "eng",
        "english"
    ):

        return {
            "title": original_title,
            "description": original_description,
            "originalTitle": original_title,
            "originalDescription": original_description,
            "translated": False
        }

    # ---------------------------------------------
    # Terjemahkan judul dan deskripsi
    # ---------------------------------------------

    translated_title = translate_to_indonesian(
        original_title
    )

    translated_description = translate_to_indonesian(
        original_description
    ) if original_description else ""

    translated = (
        translated_title != original_title
        or translated_description != original_description
    )

    return {
        "title": translated_title,
        "description": translated_description,
        "originalTitle": original_title,
        "originalDescription": original_description,
        "translated": translated
    }


# =========================================================
# TEXT FUNCTIONS
# =========================================================

def clean_text(value):

    value = html.unescape(
        str(value or "")
    )

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

        parts = urlsplit(
            str(url).strip()
        )

        return (
            parts.scheme.lower() in ("http", "https")
            and bool(parts.netloc)
        )

    except Exception:
        return False


def normalize_url(url):

    if not valid_http_url(url):
        return ""

    parts = urlsplit(
        url.strip()
    )

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

        query_items.append(
            (key, value)
        )

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

    parsed = entry.get(
        "published_parsed"
    )

    if not parsed:

        parsed = entry.get(
            "updated_parsed"
        )

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

            date = parsedate_to_datetime(
                value
            )

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

    value = article.get(
        "publishedAt", ""
    )

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

        return date.astimezone(
            timezone.utc
        )

    except (ValueError, TypeError):
        return None


# =========================================================
# IMAGE URL VALIDATION
# =========================================================

def find_valid_image(candidates, base_url=""):

    rejected_extensions = (
        ".svg",
        ".ico"
    )

    rejected_names = (
        "favicon",
        "avatar",
        "placeholder",
        "default-image",
        "no-image",
        "blank.gif"
    )

    for candidate in candidates:

        if not isinstance(candidate, str):
            continue

        candidate = html.unescape(
            candidate.strip()
        )

        candidate = candidate.strip(
            "\"'"
        )

        if not candidate:
            continue

        if candidate.lower().startswith(
            (
                "data:",
                "javascript:",
                "blob:"
            )
        ):
            continue

        if candidate.startswith("//"):

            candidate = "https:" + candidate

        elif base_url:

            candidate = urljoin(
                base_url,
                candidate
            )

        if not valid_http_url(candidate):
            continue

        lower_url = candidate.lower()

        if any(
            name in lower_url
            for name in rejected_names
        ):
            continue

        path = urlsplit(
            lower_url
        ).path

        if path.endswith(
            rejected_extensions
        ):
            continue

        return candidate

    return ""


# =========================================================
# JSON-LD IMAGE EXTRACTION
# =========================================================

def collect_jsonld_images(data, candidates):

    if isinstance(data, dict):

        for key, value in data.items():

            key_lower = key.lower()

            if key_lower in (
                "image",
                "thumbnailurl",
                "contenturl",
                "thumbnail"
            ):

                if isinstance(value, str):

                    candidates.append(value)

                elif isinstance(value, list):

                    for item in value:

                        if isinstance(item, str):

                            candidates.append(item)

                        elif isinstance(item, dict):

                            collect_jsonld_images(
                                item,
                                candidates
                            )

                elif isinstance(value, dict):

                    collect_jsonld_images(
                        value,
                        candidates
                    )

            elif isinstance(
                value,
                (dict, list)
            ):

                collect_jsonld_images(
                    value,
                    candidates
                )

    elif isinstance(data, list):

        for item in data:

            collect_jsonld_images(
                item,
                candidates
            )


# =========================================================
# HTML IMAGE EXTRACTION
# =========================================================

def extract_html_images(
    page_html,
    base_url=""
):

    if not page_html:
        return ""

    candidates = []

    try:

        parser = ImageHTMLParser()

        parser.feed(
            page_html
        )

        candidates.extend(
            parser.images
        )

        for script in parser.jsonld_scripts:

            try:

                json_data = json.loads(
                    script
                )

                collect_jsonld_images(
                    json_data,
                    candidates
                )

            except (
                ValueError,
                TypeError
            ):
                continue

        return find_valid_image(
            candidates,
            base_url
        )

    except Exception as error:

        logging.warning(
            "Gagal membaca gambar HTML: %s",
            error
        )

        return ""


# =========================================================
# DIRECT ARTICLE PAGE IMAGE
# =========================================================

def extract_image_from_article_page(article_url):

    if not valid_http_url(article_url):
        return ""

    try:

        response = requests.get(
            article_url,
            headers={
                **HEADERS,
                "Referer": article_url
            },
            timeout=IMAGE_TIMEOUT,
            allow_redirects=True
        )

        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        if (
            content_type
            and "html" not in content_type
        ):

            logging.warning(
                "Halaman bukan HTML: %s",
                article_url
            )

            return ""

        image_url = extract_html_images(
            response.text,
            response.url
        )

        if image_url:

            logging.info(
                "Gambar ditemukan pada halaman artikel: %s",
                article_url
            )

            return image_url

        logging.warning(
            "Halaman dibuka tetapi metadata gambar tidak ditemukan: %s",
            article_url
        )

    except Exception as error:

        logging.warning(
            "Gagal membuka halaman artikel %s: %s",
            article_url,
            error
        )

    return ""


# =========================================================
# IMAGE EXTRACTION FROM RSS
# =========================================================

def extract_image(entry, article_url=""):

    candidates = []

    def collect_media(value):

        if isinstance(value, dict):

            image_url = (
                value.get("url", "")
                or value.get("href", "")
            )

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

    # MEDIA RSS

    for field in (
        "media_content",
        "media_thumbnail",
        "media_group"
    ):

        collect_media(
            entry.get(field, [])
        )

    # ENCLOSURE RSS

    for item in entry.get(
        "enclosures", []
    ):

        if isinstance(item, dict):

            candidates.append(
                item.get("href", "")
                or item.get("url", "")
            )

    # LINK ENCLOSURE

    for item in entry.get(
        "links", []
    ):

        if isinstance(item, dict):

            if item.get("rel") == "enclosure":

                candidates.append(
                    item.get("href", "")
                )

    # HTML CONTENT RSS

    html_fields = []

    for item in entry.get(
        "content", []
    ):

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

        image_url = extract_html_images(
            str(content),
            article_url
        )

        if image_url:
            candidates.append(image_url)

    image_url = find_valid_image(
        candidates,
        article_url
    )

    if image_url:
        return image_url

    return extract_image_from_article_page(
        article_url
    )


# =========================================================
# REFRESH MISSING IMAGES
# =========================================================

def refresh_missing_images(articles):

    missing_articles = [
        article
        for article in articles
        if not article.get("image")
    ]

    total_missing = len(
        missing_articles
    )

    if total_missing == 0:

        logging.info(
            "Semua berita sudah memiliki gambar."
        )

        return articles

    logging.info(
        "========================================"
    )

    logging.info(
        "Memeriksa ulang %d berita tanpa gambar.",
        total_missing
    )

    logging.info(
        "========================================"
    )

    recovered = 0
    failed = 0

    for index, article in enumerate(
        missing_articles,
        start=1
    ):

        title = article.get(
            "title", "Tanpa judul"
        )

        article_url = article.get(
            "originalUrl", ""
        )

        logging.info(
            "[%d/%d] Mencari gambar: %s",
            index,
            total_missing,
            title
        )

        if not valid_http_url(article_url):

            logging.warning(
                "URL artikel tidak valid: %s",
                article_url
            )

            failed += 1
            continue

        image_url = extract_image_from_article_page(
            article_url
        )

        if image_url:

            article["image"] = image_url

            recovered += 1

            logging.info(
                "BERHASIL mendapatkan gambar: %s",
                title
            )

        else:

            failed += 1

            logging.warning(
                "Gambar belum ditemukan: %s",
                title
            )

    logging.info(
        "========================================"
    )

    logging.info(
        "Hasil pemulihan gambar: %d berhasil, %d belum ditemukan.",
        recovered,
        failed
    )

    logging.info(
        "========================================"
    )

    return articles


# =========================================================
# NEWS CATEGORIZATION
# =========================================================

def categorize(
    title,
    description,
    categories
):

    text = normalize_text(
        title + " " + description
    )

    scores = {}
    matched_keywords = {}

    for category, keywords in categories.items():

        score = 0
        matches = []

        for keyword in keywords:

            keyword = normalize_text(
                keyword
            )

            if not keyword:
                continue

            pattern = (
                r"(?<!\w)"
                + re.escape(keyword)
                + r"(?!\w)"
            )

            if re.search(
                pattern,
                text
            ):

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

    return (
        best_category,
        matched_categories
    )


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
        "translated": 0,
        "translationFailed": 0,
        "error": None
    }

    try:

        # 1. Mengambil respons RSS

        response = requests.get(
            source["url"],
            headers=RSS_HEADERS,
            timeout=TIMEOUT
        )

        response.raise_for_status()

        # 2. Membaca RSS

        feed = parse_rss_safely(
            response.content,
            response.encoding
        )

        if feed.bozo and not feed.entries:

            raise ValueError(
                "RSS tidak dapat dibaca: "
                + str(feed.bozo_exception)
            )

        entries = feed.entries

        result["raw"] = len(entries)

        if not entries:

            raise ValueError(
                "RSS tidak menghasilkan artikel. "
                "Periksa apakah URL mengembalikan XML RSS "
                "atau justru halaman HTML."
            )

        result["status"] = "success"

        articles = []

        source_language = str(
            source.get("language", "id")
        ).lower().strip()

        # 3. Memproses setiap artikel

        for entry in entries:

            original_title = clean_text(
                entry.get("title", "")
            )

            original_description = clean_text(
                entry.get("summary", "")
            )

            if not original_description:

                original_description = clean_text(
                    entry.get("description", "")
                )

            url = normalize_url(
                entry.get("link", "")
            )

            if not original_title or not url:

                result["filtered"] += 1
                continue

            # 4. Terjemahkan sebelum kategorisasi

            translation = translate_article(
                original_title,
                original_description,
                source_language
            )

            title = translation["title"]

            description = translation["description"]

            if translation["translated"]:

                result["translated"] += 1

            elif source_language in (
                "en",
                "eng",
                "english"
            ):

                result["translationFailed"] += 1

            # 5. Kategorisasi menggunakan teks terjemahan

            category, tags = categorize(
                title,
                description,
                categories
            )

            if not category:

                result["filtered"] += 1
                continue

            # 6. Mengambil gambar

            image_url = extract_image(
                entry,
                url
            )

            # 7. Membuat objek artikel

            article = {
                "id": generate_id(url),

                "title": title,

                "description": description[:2000],

                "originalTitle": original_title,

                "originalDescription": (
                    original_description[:2000]
                ),

                "translated": translation["translated"],

                "originalLanguage": source_language,

                "image": image_url,

                "category": category,

                "tags": tags,

                "source": source["name"],

                "language": "id",

                "country": source.get(
                    "country", ""
                ),

                "publishedAt": get_published_date(
                    entry
                ),

                "originalUrl": url
            }

            articles.append(article)

        # 8. Statistik

        result["accepted"] = len(articles)

        logging.info(
            (
                "%s | RSS: %d | Lolos: %d | "
                "Disaring: %d | Diterjemahkan: %d | "
                "Terjemahan gagal: %d"
            ),

            result["name"],
            result["raw"],
            result["accepted"],
            result["filtered"],
            result["translated"],
            result["translationFailed"]
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

    if not isinstance(
        previous,
        dict
    ):
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

    tokens = normalize_text(
        value
    ).split()

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


def token_jaccard(
    tokens_a,
    tokens_b
):

    if not tokens_a or not tokens_b:
        return 0.0

    union = tokens_a | tokens_b

    if not union:
        return 0.0

    return (
        len(tokens_a & tokens_b)
        / len(union)
    )


def token_overlap(
    tokens_a,
    tokens_b
):

    if not tokens_a or not tokens_b:
        return 0.0

    smaller = min(
        len(tokens_a),
        len(tokens_b)
    )

    if smaller == 0:
        return 0.0

    return (
        len(tokens_a & tokens_b)
        / smaller
    )


def is_within_duplicate_window(
    article_a,
    article_b
):

    date_a = get_article_datetime(
        article_a
    )

    date_b = get_article_datetime(
        article_b
    )

    if not date_a or not date_b:
        return False

    difference = abs(
        (date_a - date_b).total_seconds()
    )

    return (
        difference
        <= DUPLICATE_TIME_WINDOW_HOURS * 3600
    )


def is_same_news(
    article_a,
    article_b
):

    title_a = normalize_text(
        article_a.get("title", "")
    )

    title_b = normalize_text(
        article_b.get("title", "")
    )

    if not title_a or not title_b:
        return False

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

    # JUDUL SANGAT MIRIP

    if (
        len(common_title) >= 3
        and (
            sequence_similarity >= TITLE_SIMILARITY_THRESHOLD
            or title_similarity >= 0.68
        )
    ):
        return True

    # POLA PERISTIWA YANG SAMA

    shared_actions = (
        common_title & EVENT_ACTIONS
    )

    if (
        len(common_title) >= 5
        and title_overlap >= 0.65
        and shared_actions
    ):
        return True

    # PERBANDINGAN DESKRIPSI

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

    if (
        len(common_description) >= 8
        and description_similarity
        >= DESCRIPTION_SIMILARITY_THRESHOLD
    ):
        return True

    if (
        len(common_description) >= 8
        and description_overlap >= 0.72
        and len(common_title) >= 2
        and title_similarity >= 0.25
    ):
        return True

    # GABUNGAN JUDUL DAN DESKRIPSI

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

    return (
        len(description),
        1 if article.get("image") else 0,
        len(title)
    )


def merge_duplicate_metadata(
    preferred,
    other
):

    result = dict(preferred)

    # DESCRIPTION TERLENGKAP

    preferred_description = clean_text(
        result.get("description", "")
    )

    other_description = clean_text(
        other.get("description", "")
    )

    if len(other_description) > len(
        preferred_description
    ):

        result["description"] = (
            other_description[:2000]
        )

    # GAMBAR

    if (
        not result.get("image")
        and other.get("image")
    ):

        result["image"] = other["image"]

    # TAG

    tags = []

    for tag in (
        (result.get("tags") or [])
        + (other.get("tags") or [])
    ):

        if tag not in tags:
            tags.append(tag)

    result["tags"] = tags

    # SUMBER MEDIA

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

    # PERTAHANKAN INFORMASI BAHASA ASLI

    if not result.get("originalTitle"):

        result["originalTitle"] = (
            other.get("originalTitle", "")
        )

    if not result.get("originalDescription"):

        result["originalDescription"] = (
            other.get("originalDescription", "")
        )

    if not result.get("originalLanguage"):

        result["originalLanguage"] = (
            other.get("originalLanguage", "")
        )

    if result.get("translated") is None:

        result["translated"] = (
            other.get("translated", False)
        )

    return result


# =========================================================
# MERGE OLD AND NEW ARTICLES
# =========================================================

def merge_articles(
    new_articles,
    old_articles
):

    logging.info(
        "Memulai pembersihan berita lama dan baru."
    )

    # 1. NORMALISASI URL

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

    # 2. MASUKKAN BERITA BARU

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

    # 3. HAPUS BERITA KEDALUWARSA

    cutoff = datetime.now(
        timezone.utc
    ) - timedelta(
        days=MAX_AGE_DAYS
    )

    valid_articles = []

    for article in candidates:

        date = get_article_datetime(
            article
        )

        if date and date < cutoff:
            continue

        valid_articles.append(article)

    # 4. URUTKAN BERITA

    valid_articles.sort(
        key=lambda item: item.get(
            "publishedAt", ""
        ),
        reverse=True
    )

    # 5. DEDUPLIKASI ID

    id_map = {}

    for article in valid_articles:

        article_id = article.get(
            "id", ""
        )

        if not article_id:

            article_id = generate_id(
                article.get("originalUrl", "")
            )

        article["id"] = article_id

        existing = id_map.get(
            article_id
        )

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

    # 6. DEDUPLIKASI PERISTIWA

    unique_articles = []

    duplicate_count = 0

    for article in valid_articles:

        duplicate_index = None

        for index, existing in enumerate(
            unique_articles
        ):

            if is_same_news(
                article,
                existing
            ):

                duplicate_index = index
                break

        if duplicate_index is None:

            unique_articles.append(article)
            continue

        duplicate_count += 1

        existing = unique_articles[
            duplicate_index
        ]

        logging.info(
            "Duplikasi ditemukan: %s | %s",
            article.get("source", ""),
            article.get("title", "")
        )

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

    # 7. PEMBERSIHAN ID TERAKHIR

    final_map = {}

    for article in unique_articles:

        article_id = article.get(
            "id", ""
        )

        if not article_id:
            continue

        if article_id not in final_map:

            final_map[article_id] = article

    final_articles = list(
        final_map.values()
    )

    # 8. URUTKAN DAN BATASI

    final_articles.sort(
        key=lambda item: item.get(
            "publishedAt", ""
        ),
        reverse=True
    )

    final_articles = final_articles[
        :MAX_ARTICLES
    ]

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

    # -----------------------------------------------------
    # 1. LOAD CACHE TERJEMAHAN
    # -----------------------------------------------------

    load_translation_cache()

    # -----------------------------------------------------
    # 2. BACA BERITA LAMA
    # -----------------------------------------------------

    previous_articles = load_previous_articles()

    logging.info(
        "Berita lama ditemukan: %d",
        len(previous_articles)
    )

    # -----------------------------------------------------
    # 3. AMBIL BERITA RSS
    # -----------------------------------------------------

    all_articles = []

    source_stats = []

    for source in sources:

        articles, stats = fetch_source(
            source,
            categories
        )

        all_articles.extend(
            articles
        )

        source_stats.append(
            stats
        )

    successful = sum(
        1
        for item in source_stats
        if item["status"] == "success"
    )

    failed = (
        len(source_stats) - successful
    )

    # -----------------------------------------------------
    # 4. GABUNGKAN BERITA
    # -----------------------------------------------------

    final_articles = merge_articles(
        all_articles,
        previous_articles
    )

    # -----------------------------------------------------
    # 5. PULIHKAN GAMBAR
    # -----------------------------------------------------

    final_articles = refresh_missing_images(
        final_articles
    )

    # -----------------------------------------------------
    # 6. SIMPAN CACHE TERJEMAHAN
    # -----------------------------------------------------

    save_translation_cache()

    # -----------------------------------------------------
    # 7. STATUS RSS
    # -----------------------------------------------------

    if successful == 0:

        status = "error"

    elif failed > 0:

        status = "partial"

    else:

        status = "success"

    # -----------------------------------------------------
    # 8. STATISTIK GAMBAR
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
    # 9. STATISTIK TERJEMAHAN
    # -----------------------------------------------------

    translated_articles = sum(
        item.get("translated", 0)
        for item in source_stats
    )

    failed_translations = sum(
        item.get("translationFailed", 0)
        for item in source_stats
    )

    # -----------------------------------------------------
    # 10. OUTPUT JSON
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

        "translation": {

            "engine": "Argos Translate",

            "sourceLanguage": "en",

            "targetLanguage": "id",

            "translatedArticles": translated_articles,

            "failedArticles": failed_translations
        },

        "articles": final_articles
    }

    save_json(
        OUTPUT_FILE,
        result
    )

    # -----------------------------------------------------
    # 11. LOGGING
    # -----------------------------------------------------

    logging.info(
        "--------------------------------"
    )

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

    logging.info(
        "Artikel berhasil diterjemahkan: %d",
        translated_articles
    )

    logging.info(
        "Artikel gagal diterjemahkan: %d",
        failed_translations
    )

    logging.info(
        "--------------------------------"
    )

    if successful == 0:

        raise RuntimeError(
            "Semua sumber RSS gagal diakses."
        )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    main()
