import feedparser
import json
import os
import re
import time
import html
from deep_translator import GoogleTranslator

# 1. KATA KUNCI WAJIB ISLAMI (ID & EN)
# Artikel HARUS mengandung salah satu dari kata kunci spesifik keagamaan ini
STRICT_ISLAMIC_KEYWORDS_ID = [
    'al-qur\'an', 'alquran', 'hadits', 'hadis', 'sunnah', 'tafsir', 'fiqih', 'fiqh', 
    'fatwa', 'syariah', 'sharia', 'halal', 'zakat', 'infaq', 'sedekah', 'wakaf', 
    'shalat', 'salat', 'solat', 'tahajud', 'dhuha', 'puasa', 'ramadhan', 'ramadan', 
    'khutbah', 'dakwah', 'tahfidz', 'do\'a', 'doa nabi', 'keutamaan', 'bacaan sholat',
    'masjid al-aqsa', 'masjidil haram', 'ka\'bah', 'kabah', 'ulama', 'ustadz', 'kyai', 
    'khazanah islam', 'sejarah islam', 'peradaban islam'
]

STRICT_ISLAMIC_KEYWORDS_EN = [
    'quran', 'koran', 'hadith', 'sunnah', 'tafsir', 'sharia', 'shariah', 'fatwa', 
    'zakat', 'halal', 'ramadan', 'hajj', 'umrah', 'mosque', 'al-aqsa mosque', 
    'islamic scholar', 'islamic preaching', 'prophet muhammad', 'islamic heritage',
    'islamophobia', 'islamic jurisprudence'
]

# 2. KATA KUNCI PENOLAK (POLITIK, DIPLOMASI, BIROKRASI, HIBURAN, KECELAKAAN)
# Jika mengandung kata ini, BERITA LANGSUNG DIBUANG
EXCLUDE_KEYWORDS = [
    'pemilu', 'pilkada', 'capres', 'cawapres', 'parpol', 'dpr', 'dprd', 'menteri', 
    'politikus', 'diplomasi', 'pBB', 'gencatan senjata', 'serangan udara', 'rudal', 
    'tank', 'militer', 'korupsi', 'kriminal', 'pembunuhan', 'pemerkosaan', 'judi', 
    'narkoba', 'lgbt', 'hollywood', 'celebrity gossip', 'stock market', 'bursa efek',
    'premier league', 'champions league', 'investasi', 'saham'
]

def load_categories():
    if os.path.exists('aggregator/categories.json'):
        try:
            with open('aggregator/categories.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

CATEGORIES = load_categories()

def detect_category(title, summary, default_category="Hikmah & Ibadah"):
    content = f"{title} {summary}".lower()
    for category_name, keywords in CATEGORIES.items():
        if isinstance(keywords, list):
            for keyword in keywords:
                pattern = r'(?<!\w)' + re.escape(keyword.lower()) + r'(?!\w)'
                if re.search(pattern, content):
                    return category_name
    return default_category

def is_murni_islami(title, summary, lang='id'):
    """
    LOGIKA FILTER KETAT:
    1. Tolak jika ada kata kunci politik/birokrasi/militer murni.
    2. Wajib memuat minimal 1 kata kunci spiritual/keislaman utama.
    """
    content = f"{title} {summary}".lower()

    # Reject jika memuat topik politik / non-keagamaan
    for bad_word in EXCLUDE_KEYWORDS:
        pattern = r'(?<!\w)' + re.escape(bad_word) + r'(?!\w)'
        if re.search(pattern, content):
            return False

    # Accept HANYA jika memuat kata kunci Islami murni
    keywords = STRICT_ISLAMIC_KEYWORDS_EN if lang == 'en' else STRICT_ISLAMIC_KEYWORDS_ID
    for kw in keywords:
        pattern = r'(?<!\w)' + re.escape(kw) + r'(?!\w)'
        if re.search(pattern, content):
            return True

    return False

def clean_text(text):
    if not text:
        return ""
    # Decode HTML Entities (&mdash;, &nbsp;, dll)
    text = html.unescape(text)
    clean = re.compile('<.*?>')
    text = re.sub(clean, '', text)
    return re.sub(r'\s+', ' ', text).strip()

def safe_translate(text):
    if not text:
        return text
    text_short = text[:400]
    try:
        translator = GoogleTranslator(source='auto', target='id')
        translated = translator.translate(text_short)
        time.sleep(0.6)
        return translated
    except Exception:
        return text_short

def fetch_news():
    if not os.path.exists('aggregator/sources.json'):
        print("[Error] File aggregator/sources.json tidak ditemukan.")
        return

    with open('aggregator/sources.json', 'r', encoding='utf-8') as f:
        sources = json.load(f)

    all_news = []
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

    for source in sources:
        try:
            feed = feedparser.parse(source['url'], agent=user_agent)
            lang = source.get('language', 'id')

            print(f"[Fetch] {source['name']} ({len(feed.entries)} berita)...")

            for entry in feed.entries[:25]:
                title_raw = clean_text(entry.get('title', ''))
                summary_raw = clean_text(entry.get('summary', entry.get('description', '')))
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))

                # EKSEKUSI FILTER MURNI ISLAMI
                if is_murni_islami(title_raw, summary_raw, lang=lang):
                    matched_category = detect_category(title_raw, summary_raw, source.get('category', 'Hikmah & Ibadah'))
                    
                    title = title_raw
                    summary = summary_raw[:250]
                    is_translated_success = False

                    if lang == 'en':
                        translated_title = safe_translate(title_raw)
                        translated_summary = safe_translate(summary_raw[:250]) if summary_raw else ""
                        
                        if translated_title != title_raw:
                            is_translated_success = True

                        title = translated_title
                        summary = translated_summary

                    item = {
                        "title": title,
                        "summary": summary + ('...' if len(summary_raw) > 250 else ''),
                        "link": link,
                        "published": published,
                        "source_name": source['name'],
                        "category": matched_category,
                        "is_translated": is_translated_success
                    }
                    all_news.append(item)
        except Exception as e:
            print(f"[Error] Gagal pada {source.get('name')}: {e}")

    # Deduplikasi berdasarkan judul
    seen_titles = set()
    unique_news = []
    for news in all_news:
        title_key = news['title'].lower().strip()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_news.append(news)

    # Simpan hasil agregasi murni Islami
    os.makedirs('data', exist_ok=True)
    with open('data/news.json', 'w', encoding='utf-8') as f:
        json.dump(unique_news[:80], f, ensure_ascii=False, indent=2)

    print(f"[Sukses] Tersimpan {len(unique_news[:80])} berita MURNI Islami ke data/news.json")

if __name__ == '__main__':
    fetch_news()
