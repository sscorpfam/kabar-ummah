import feedparser
import json
import os
import re
import time
from deep_translator import GoogleTranslator

# Kata kunci Islami & Umat Islam (ID & EN) yang spesifik dan akurat
ISLAMIC_KEYWORDS = [
    # Indonesia / Melayu
    'islam', 'islami', 'muslim', 'muslimah', 'al-qur\'an', 'alquran', 'hadits', 'hadis', 
    'sunnah', 'zakat', 'infaq', 'sedekah', 'wakaf', 'shalat', 'salat', 'solat', 'puasa', 
    'ramadhan', 'ramadan', 'haji', 'umrah', 'umroh', 'masjid', 'musholla', 'ulama', 
    'ustadz', 'ustad', 'kyai', 'kiai', 'pesantren', 'fiqih', 'fiqh', 'fatwa', 'syariah', 
    'halal', 'khazanah', 'palestina', 'gaza', 'al-aqsa', 'alaqsa', 'yerusalem timur', 
    'mufti', 'khutbah', 'dakwah', 'syiar', 'tahfidz', 'santri',

    # English / International (Directly related to Islam & Palestinian Struggle)
    'islam', 'islamic', 'muslim', 'muslims', 'quran', 'koran', 'hadith', 'sunnah', 
    'zakat', 'palestine', 'palestinian', 'gaza', 'al-aqsa', 'aqsa', 'ramadan', 
    'hajj', 'umrah', 'mosque', 'scholars', 'sharia', 'shariah', 'halal', 
    'islamophobia', 'fatwa', 'imam', 'caliphate'
]

# Kata kunci pembatal (Negative Keywords)
EXCLUDE_KEYWORDS = [
    'casino', 'gambling', 'judi', 'narkoba', 'lgbt', 'hollywood', 'celebrity gossip'
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

def is_islamic_content(title, summary, is_trusted_islamic_source=False):
    if is_trusted_islamic_source:
        return True

    content = f"{title} {summary}".lower()
    if any(re.search(r'\b' + re.escape(word) + r'\b', content) for word in EXCLUDE_KEYWORDS):
        return False

    return any(re.search(r'\b' + re.escape(keyword) + r'\b', content) for keyword in ISLAMIC_KEYWORDS)

def clean_html(text):
    if not text:
        return ""
    clean = re.compile('<.*?>')
    text = re.sub(clean, '', text)
    return re.sub(r'\s+', ' ', text).strip()

def safe_translate(text):
    """
    Menerjemahkan teks satu per satu dengan jeda waktu 1.5 detik & penanganan retry
    agar terhindar dari pembatasan IP publik pada GitHub Actions.
    """
    if not text:
        return ""
    
    # Ambil 500 karakter pertama agar proses terjemahan sangat cepat
    text_to_translate = text[:500]
    
    for attempt in range(2):
        try:
            translator = GoogleTranslator(source='auto', target='id')
            translated = translator.translate(text_to_translate)
            time.sleep(1.5)  # Delay aman antar-request
            return translated
        except Exception as e:
            time.sleep(2)  # Tunggu 2 detik jika gagal sebelum coba lagi
            
    # Jika gagal 2x, kembalikan teks asli (English) agar script tidak berhenti
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
            is_trusted = source.get('category') in ['islami', 'palestina', 'Hikmah & Ibadah']
            lang = source.get('language', 'id')

            for entry in feed.entries[:15]:
                title_raw = clean_html(entry.get('title', ''))
                summary_raw = clean_html(entry.get('summary', entry.get('description', '')))
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))

                if is_islamic_content(title_raw, summary_raw, is_trusted_islamic_source=is_trusted):
                    matched_category = detect_category(title_raw, summary_raw, source.get('category', 'Hikmah & Ibadah'))
                    
                    title = title_raw
                    summary = summary_raw[:250]

                    # Terjemahkan satu per satu secara langsung jika berita internasional
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

    # Hapus duplikat berita berdasarkan kemiripan judul
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

    print(f"[Sukses] Berhasil menyimpan {len(unique_news[:80])} berita Islami ke data/news.json")

if __name__ == '__main__':
    fetch_news()
