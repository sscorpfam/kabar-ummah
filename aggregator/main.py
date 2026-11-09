import feedparser
import json
import os
import re
import time
from deep_translator import GoogleTranslator

# 1. Kata kunci Islami & Umat (Indonesia)
ISLAMIC_KEYWORDS_ID = [
    'islam', 'islami', 'muslim', 'muslimah', 'alquran', 'al-qur\'an', 'hadits', 'hadis', 
    'sunnah', 'zakat', 'infaq', 'sedekah', 'wakaf', 'shalat', 'salat', 'solat', 'puasa', 
    'ramadhan', 'ramadan', 'haji', 'umrah', 'umroh', 'masjid', 'musholla', 'ulama', 
    'ustadz', 'ustad', 'kyai', 'kiai', 'pesantren', 'fiqih', 'fiqh', 'fatwa', 'syariah', 
    'halal', 'khazanah', 'mufti', 'khutbah', 'dakwah', 'syiar', 'tahfidz', 'santri',
    'ekonomi syariah', 'bank syariah', 'palestina', 'gaza', 'al-aqsa', 'alaqsa'
]

# 2. Kata kunci Islami & Palestina (English / International)
ISLAMIC_KEYWORDS_EN = [
    'islam', 'islamic', 'muslim', 'muslims', 'quran', 'koran', 'hadith', 'sunnah', 
    'zakat', 'palestine', 'palestinian', 'gaza', 'al-aqsa', 'alaqsa', 'aqsa', 'ramadan', 
    'hajj', 'umrah', 'mosque', 'scholars', 'sharia', 'shariah', 'halal', 
    'islamophobia', 'fatwa', 'imam', 'caliphate', 'jerusalem', 'west bank', 
    'rafah', 'jenin', 'khan younis', 'ceasefire', 'martyr', 'occupation'
]

# 3. Kata Kunci Penolak Berita Non-Islami / Hiburan / Olahraga Murni
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
                pattern = r'(?<!\w)' + re.escape(keyword.lower()) + r'(?!\w)'
                if re.search(pattern, content):
                    return category_name
    return default_category

def is_strict_islamic_content(title, summary, lang='id'):
    """
    Validasi ketat berdasarkan bahasa berita (ID / EN)
    """
    content = f"{title} {summary}".lower()

    # Reject jika ada kata kunci terlarang
    for bad_word in EXCLUDE_KEYWORDS:
        pattern = r'(?<!\w)' + re.escape(bad_word) + r'(?!\w)'
        if re.search(pattern, content):
            return False

    # Pilih kamus kata kunci berdasarkan bahasa asal feed
    keywords_to_check = ISLAMIC_KEYWORDS_EN if lang == 'en' else ISLAMIC_KEYWORDS_ID

    for keyword in keywords_to_check:
        # Gunakan regex boundary safe untuk tanda hubung (-) dan petik (')
        pattern = r'(?<!\w)' + re.escape(keyword) + r'(?!\w)'
        if re.search(pattern, content):
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
    for attempt in range(2):
        try:
            translator = GoogleTranslator(source='auto', target='id')
            translated = translator.translate(text_to_translate)
            time.sleep(1.0)
            return translated
        except Exception as e:
            print(f"  [Error Translate] Menunggu retry... ({e})")
            time.sleep(1.5)
    return text_to_translate

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
            
            print(f"[Info] Memproses {source['name']} ({len(feed.entries)} berita)...")

            for entry in feed.entries[:15]:
                title_raw = clean_html(entry.get('title', ''))
                summary_raw = clean_html(entry.get('summary', entry.get('description', '')))
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))

                # Filter sesuai bahasa sumber (EN atau ID)
                if is_strict_islamic_content(title_raw, summary_raw, lang=lang):
                    matched_category = detect_category(title_raw, summary_raw, source.get('category', 'Hikmah & Ibadah'))
                    
                    title = title_raw
                    summary = summary_raw[:250]

                    # Terjemahkan jika berita bahasa Inggris
                    if lang == 'en':
                        print(f"  -> Menerjemahkan: {title_raw[:40]}...")
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
            print(f"[Error] Gagal pada sumber {source.get('name')}: {e}")

    # Hapus duplikat berita
    seen_titles = set()
    unique_news = []
    for news in all_news:
        title_key = news['title'].lower().strip()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_news.append(news)

    # Simpan ke file data/news.json
    os.makedirs('data', exist_ok=True)
    with open('data/news.json', 'w', encoding='utf-8') as f:
        json.dump(unique_news[:80], f, ensure_ascii=False, indent=2)

    print(f"[Sukses] Berhasil menyimpan {len(unique_news[:80])} berita ke data/news.json")

if __name__ == '__main__':
    fetch_news()
