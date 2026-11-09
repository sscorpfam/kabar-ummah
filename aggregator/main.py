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


# =========================================================
# FILTER KHUSUS BERITA ISLAMI
# =========================================================

ISLAMIC_KEYWORDS = [
    # Identitas dan komunitas Islam
    "islam", "islami", "islamic", "muslim", "muslimah",
    "umat islam", "umat muslim", "kaum muslimin",
    "masyarakat muslim", "komunitas muslim",

    # Al-Quran dan hadis
    "al quran", "alquran", "quran", "qur'an",
    "mushaf", "ayat alquran", "tafsir",
    "hadis", "hadits", "sunnah", "sahih",
    "ulumul quran", "ilmu hadis",

    # Ibadah
    "shalat", "salat", "sholat", "sujud",
    "zakat", "infak", "infaq", "sedekah",
    "puasa", "shaum", "ramadan", "ramadhan",
    "haji", "umrah", "qurban", "kurban",
    "tahajud", "tarawih", "witir",
    "azan", "adzan", "iqamah",
    "doa", "dzikir", "zikir", "istighfar",
    "wudhu", "wudu", "tayamum",

    # Ulama dan tokoh Islam
    "ulama", "ustadz", "ustaz", "kyai", "kiai",
    "dai", "da'i", "mufti", "imam",
    "habib", "syekh", "syaikh",
    "ustadzah", "ustazah",

    # Nabi dan sejarah Islam
    "nabi", "rasul", "rasulullah",
    "muhammad saw", "muhammad ﷺ",
    "sahabat nabi", "tabiin", "tabi'in",
    "sirah nabawi", "sejarah islam",
    "peradaban islam", "khilafah",
    "bani umayyah", "bani abbasiyah",
    "khulafaur rasyidin",

    # Pendidikan, dakwah, dan hukum Islam
    "dakwah", "dakwah islam", "kajian islam",
    "kajian islami", "ceramah agama",
    "pesantren", "madrasah", "santri",
    "majelis taklim", "majelis ilmu",
    "fatwa", "fikih", "fiqih", "akidah",
    "aqidah", "syariah", "syariat",
    "hukum islam", "pendidikan islam",
    "ekonomi syariah", "perbankan syariah",
    "wakaf", "badan amil zakat",
