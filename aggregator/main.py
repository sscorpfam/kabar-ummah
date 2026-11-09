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

# Kata kunci pembatal (Negative Keywords): Jika mengandung kata ini, berita langsung DITOLAK
EXCLUDE_KEYWORDS = [
    'casino', 'gambling', 'judi', 'narkoba', 'lgbt', 'hollywood', 'celebrity gossip'
]

# Memuat categories.json jika tersedia
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
    """Mendeteksi kategori berita berdasarkan kata kunci di categories.json"""
    content = f"{title} {summary}".lower()
    for category_name, keywords in CATEGORIES.items():
        if isinstance(keywords, list):
            for keyword in keywords:
                if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', content):
                    return category_name
    return default_category

def is_islamic_content(title, summary, is_trusted_islamic_source=False):
    """
    Memeriksa apakah konten berita benar-benar Islami.
    Jika sumbernya sudah 100% portal khusus Islam, otomatis return True.
    """
    if is_trusted_islamic_source:
        return True

    content = f"{title} {summary}".lower()

    # Cek apakah ada kata kunci terlarang/non-islami
    if any(re.search(r'\b' + re.escape(word) + r'\b', content) for word in EXCLUDE_KEYWORDS):
        return False

    # Validasi kata kunci Islami dengan word boundary (\b)
    return any(re.search(r'\b' + re.escape(keyword) + r'\b', content) for keyword in ISLAMIC_KEYWORDS)

def clean_html(text):
    if not text:
        return ""
    clean = re.compile('<.*?>')
    text = re.sub(clean, '', text)
    return re.sub(r'\s+', ' ', text).strip()

def safe_translate_batch(texts):
    """
    Menerjemahkan kumpulan teks (batch) sekaligus agar terhindar dari rate-limit 5 req/sec.
    """
    if not texts:
        return []
    
    translator = GoogleTranslator(source='auto', target='id')
    try:
        translated = translator.translate_batch(texts)
        time.sleep(1) # Jeda aman 1 detik
        return translated
    except Exception as e:
        print(f"[Peringatan] Batch translation gagal: {e}. Menggunakan teks asli.")
        return texts

def fetch_news():
    if not os.path.exists('aggregator/sources.json'):
        print("[Error] File aggregator/sources.json tidak ditemukan.")
        return

    with open('aggregator/sources.json', 'r', encoding='utf-8') as f:
        sources = json.load(f)

    raw_items = []
    texts_to_translate = []
    translate_indices = []

    for source in sources:
        try:
            feed = feedparser.parse(source['url'])
            is_trusted = source.get('category') in ['islami', 'palestina', 'Hikmah & Ibadah']
            lang = source.get('language', 'id')

            for entry in feed.entries[:20]:
                title_raw = clean_html(entry.get('title', ''))
                summary_raw = clean_html(entry.get('summary', entry.get('description', '')))
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))

                # Filter kelayakan berita Islami
                if is_islamic_content(title_raw, summary_raw, is_trusted_islamic_source=is_trusted):
                    matched_category = detect_category(title_raw, summary_raw, source.get('category', 'Hikmah & Ibadah'))
                    
                    item = {
                        "title": title_raw,
                        "summary": summary_raw[:300],
                        "link": link,
                        "published": published,
                        "source_name": source['name'],
                        "category": matched_category,
                        "is_translated": (lang == 'en')
                    }
                    
                    # Jika bahasa Inggris, kumpulkan untuk diterjemahkan via batch
                    if lang == 'en':
                        translate_indices.append((len(raw_items), 'title'))
                        texts_to_translate.append(title_raw)
                        
                        if summary_raw:
                            translate_indices.append((len(raw_items), 'summary'))
                            texts_to_translate.append(summary_raw[:300])

                    raw_items.append(item)
        except Exception as e:
            print(f"[Error] Gagal mengambil dari sumber {source.get('name')}: {e}")

    # Eksekusi Penerjemahan Massal (Batch)
    if texts_to_translate:
        print(f"[Info] Memproses terjemahan {len(texts_to_translate)} elemen teks...")
        translated_texts = safe_translate_batch(texts_to_translate)
        
        for (item_idx, field), translated_text in zip(translate_indices, translated_texts):
            raw_items[item_idx][field] = translated_text

    # Hapus duplikat berita berdasarkan kemiripan judul
    seen_titles = set()
    unique_news = []
    for news in raw_items:
        title_key = news['title'].lower().strip()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            # Format akhiran summary
            if news['summary'] and not news['summary'].endswith('...'):
                news['summary'] += '...'
            unique_news.append(news)

    # Simpan hasil agregasi (Maksimal 80 berita terbaru)
    os.makedirs('data', exist_ok=True)
    with open('data/news.json', 'w', encoding='utf-8') as f:
        json.dump(unique_news[:80], f, ensure_ascii=False, indent=2)

    print(f"[Sukses] Berhasil menyimpan {len(unique_news[:80])} berita Islami ke data/news.json")

if __name__ == '__main__':
    fetch_news()
