import feedparser
import json
import os
import re
import time
from deep_translator import GoogleTranslator

# 1. Kata kunci Islami & Palestina (Indonesia & English)
ISLAMIC_KEYWORDS = [
    # Indonesia
    'islam', 'islami', 'muslim', 'muslimah', 'al-qur\'an', 'alquran', 'hadits', 'hadis', 
    'sunnah', 'zakat', 'infaq', 'sedekah', 'wakaf', 'shalat', 'salat', 'solat', 'puasa', 
    'ramadhan', 'ramadan', 'haji', 'umrah', 'umroh', 'masjid', 'musholla', 'ulama', 
    'ustadz', 'ustad', 'kyai', 'kiai', 'pesantren', 'fiqih', 'fiqh', 'fatwa', 'syariah', 
    'halal', 'khazanah', 'mufti', 'khutbah', 'dakwah', 'syiar', 'tahfidz', 'santri',
    'ekonomi syariah', 'bank syariah', 'palestina', 'gaza', 'al-aqsa', 'alaqsa',

    # English / International (Dunia Islam & Palestina)
    'islam', 'islamic', 'muslim', 'muslims', 'quran', 'koran', 'hadith', 'sunnah', 
    'zakat', 'palestine', 'palestinian', 'gaza', 'al-aqsa', 'aqsa', 'ramadan', 
    'hajj', 'umrah', 'mosque', 'scholars', 'sharia', 'shariah', 'halal', 
    'islamophobia', 'fatwa', 'imam', 'caliphate', 'jerusalem', 'west bank', 
    'rafah', 'jenin', 'khan younis', 'martyr', 'occupation'
]

# 2. Filter Penolak Berita Non-Islami / Hiburan / Kriminal umum
EXCLUDE_KEYWORDS = [
    'casino', 'gambling', 'judi', 'narkoba', 'lgbt', 'hollywood', 'celebrity gossip', 
    'stock market', 'premier league', 'champions league', 'bursa efek'
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
    content = f"{title} {summary}".lower()

    # Reject jika mengandung kata terlarang
    for bad_word in EXCLUDE_KEYWORDS:
        if re.search(r'\b' + re.escape(bad_word) + r'\b', content):
            return False

    # Accept jika ada kata kunci Islami/Palestina
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
        return text
    text_to_translate = text[:500]
    try:
        translator = GoogleTranslator(source='auto', target='id')
        translated = translator.translate(text_to_translate)
        time.sleep(1.0)
        return translated
    except Exception as e:
        print(f"[Peringatan] Terjemahan gagal, gunakan teks asli: {e}")
        return text_to_translate

def fetch_news():
    if not os.path.exists('aggregator/sources.json'):
        print("[Error] File aggregator/sources.json tidak ditemukan.")
        return

    with open('aggregator/sources.json', 'r', encoding='utf-8') as f:
        sources = json.load(f)

    all_news = []

    # Atur User-Agent browser agar tidak diblokir oleh server luar negeri
    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

    for source in sources:
        try:
            # Parse RSS dengan User-Agent Custom
            feed = feedparser.parse(source['url'], agent=user_agent)
            lang = source.get('language', 'id')
            
            print(f"[Info] Mengambil dari {source['name']} (Jumlah entry: {len(feed.entries)})")

            for entry in feed.entries[:15]:
                title_raw = clean_html(entry.get('title', ''))
                summary_raw = clean_html(entry.get('summary', entry.get('description', '')))
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))

                if is_strict_islamic_content(title_raw, summary_raw):
                    matched_category = detect_category(title_raw, summary_raw, source.get('category', 'Hikmah & Ibadah'))
                    
                    title = title_raw
                    summary = summary_raw[:250]

                    # Terjemahkan berita internasional (EN)
                    if lang == 'en':
                        print(f"  -> Menerjemahkan EN: {title_raw[:35]}...")
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

    # Deduplikasi Judul Berita
    seen_titles = set()
    unique_news = []
    for news in all_news:
        title_key = news['title'].lower().strip()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_news.append(news)

    # Simpan hasil akhir (Maksimal 80 berita)
    os.makedirs('data', exist_ok=True)
    with open('data/news.json', 'w', encoding='utf-8') as f:
        json.dump(unique_news[:80], f, ensure_ascii=False, indent=2)

    print(f"[Sukses] Berhasil menyimpan {len(unique_news[:80])} berita ke data/news.json")

if __name__ == '__main__':
    fetch_news()
