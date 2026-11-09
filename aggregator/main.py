import feedparser
import json
import os
import re
import time
from deep_translator import GoogleTranslator

# 1. Kata Kunci Spesifik Islami & Perjuangan Palestina/Al-Aqsa
ISLAMIC_KEYWORDS = [
    # Ibadah, Ajaran & Syariat
    'islam', 'islami', 'muslim', 'muslimah', 'al-qur\'an', 'alquran', 'hadits', 'hadis', 
    'sunnah', 'zakat', 'infaq', 'sedekah', 'wakaf', 'shalat', 'salat', 'solat', 'puasa', 
    'ramadhan', 'ramadan', 'haji', 'umrah', 'umroh', 'masjid', 'musholla', 'ulama', 
    'ustadz', 'ustad', 'kyai', 'kiai', 'pesantren', 'fiqih', 'fiqh', 'fatwa', 'syariah', 
    'halal', 'khazanah', 'mufti', 'khutbah', 'dakwah', 'syiar', 'tahfidz', 'santri',
    'ekonomi syariah', 'bank syariah',

    # Internasional & Palestina (Harus spesifik konteks keislaman/Al-Aqsa/Kemanusiaan Umat)
    'al-aqsa', 'alaqsa', 'masjid al-aqsa', 'palestina', 'palestine', 'palestinian', 
    'islamic', 'quran', 'hadith', 'mosque', 'sharia', 'shariah', 'islamophobia', 
    'imam', 'scholars', 'hajj'
]

# 2. Filter Penolak Berita Non-Islami / Politik Murni / Kriminal Umum
EXCLUDE_KEYWORDS = [
    'casino', 'gambling', 'judi', 'narkoba', 'lgbt', 'hollywood', 'celebrity', 
    'stock market', 'wall street', 'premier league', 'champions league', 'bursa efek',
    'pemilu', 'pilkada', 'capres', 'cawapres', 'parpol', 'dpr', 'korupsi'
]

def load_categories():
    if os.path.exists('aggregator/categories.json'):
        try:
            with open('aggregator/categories.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[Peringatan] Gagal membaca categories.json: {e}")
    return {}

CATEGORIES = load_categories()

def detect_category(title, summary, default_category="Hikmah & Ibadah"):
    content = f"{title} {summary}".lower()
    for category_name, keywords in CATEGORIES.items():
        if isinstance(keywords, list):
            for keyword in keywords:
                if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', content):
                    return category_name
    return default_category

def is_strict_islamic_content(title, summary):
    """
    Validasi ketat: Hanya meloloskan berita yang benar-benar memuat konteks Islami.
    """
    content = f"{title} {summary}".lower()

    # Reject jika mengandung kata kunci pembatal (politik umum, hiburan, dll)
    for bad_word in EXCLUDE_KEYWORDS:
        if re.search(r'\b' + re.escape(bad_word) + r'\b', content):
            return False

    # Accept HANYA jika cocok dengan kata kunci Islami yang valid
    for keyword in ISLAMIC_KEYWORDS:
        if re.search(r'\b' + re.escape(keyword) + r'\b', content):
            return True

    return False

def clean_html(text):
    if not text:
        return ""
    clean = re.compile('<.*?>')
    text = re.sub(clean, '', text)
    return re.sub(r'\s+', ' ', text).strip()

def safe_translate(text):
    if not text:
        return ""
    text_to_translate = text[:500]
    for attempt in range(2):
        try:
            translator = GoogleTranslator(source='auto', target='id')
            translated = translator.translate(text_to_translate)
            time.sleep(1.2)
            return translated
        except Exception:
            time.sleep(1.5)
    return text_to_translate

def fetch_news():
    if not os.path.exists('aggregator/sources.json'):
        print("[Error] File aggregator/sources.json tidak ditemukan.")
        return

    with open('aggregator/sources.json', 'r', encoding='utf-8') as f:
        sources = json.load(f)

    all_news = []

    for source in sources:
        try:
            feed = feedparser.parse(source['url'])
            lang = source.get('language', 'id')

            for entry in feed.entries[:15]:
                title_raw = clean_html(entry.get('title', ''))
                summary_raw = clean_html(entry.get('summary', entry.get('description', '')))
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))

                # PERBAIKAN UTAMA: Semua sumber wajib lolos verifikasi ketat is_strict_islamic_content
                if is_strict_islamic_content(title_raw, summary_raw):
                    matched_category = detect_category(title_raw, summary_raw, source.get('category', 'Hikmah & Ibadah'))
                    
                    title = title_raw
                    summary = summary_raw[:250]

                    if lang == 'en':
                        print(f"[Info] Menerjemahkan berita EN: {title_raw[:40]}...")
                        title = safe_translate(title_raw)
                        if summary_raw:
                            summary = safe_translate(summary_raw[:250])

                    item = {
                        "title": title,
                        "summary": summary + ('...' if len(summary_raw) > 250 else ''),
                        "link": link,
                        "published": published,
                        "source_name": source['name'],
                        "category": matched_category,
                        "is_translated": (lang == 'en')
                    }
                    all_news.append(item)
        except Exception as e:
            print(f"[Error] Gagal mengambil dari sumber {source.get('name')}: {e}")

    # Deduplikasi judul
    seen_titles = set()
    unique_news = []
    for news in all_news:
        title_key = news['title'].lower().strip()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_news.append(news)

    # Simpan maksimal 80 berita murni Islami
    os.makedirs('data', exist_ok=True)
    with open('data/news.json', 'w', encoding='utf-8') as f:
        json.dump(unique_news[:80], f, ensure_ascii=False, indent=2)

    print(f"[Sukses] Simpan {len(unique_news[:80])} berita murni Islami ke data/news.json")

if __name__ == '__main__':
    fetch_news()
