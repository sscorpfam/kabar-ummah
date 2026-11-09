import feedparser
import json
import os
import re
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
# Mencegah berita politik umum/militer murni meloloskan isu non-religius
EXCLUDE_KEYWORDS = [
    'casino', 'gambling', 'judi', 'narkoba', 'lgbt', 'hollywood', 'celebrity gossip'
]

translator = GoogleTranslator(source='auto', target='id')

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

def translate_text(text):
    if not text:
        return ""
    try:
        # Terjemahkan per klausa/teks pendek agar hasil presisi
        return translator.translate(text[:800])
    except Exception as e:
        print(f"[Peringatan] Gagal menerjemahkan: {e}")
        return text

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
            # Sumber yang kategorinya 'islami' atau 'palestina' dianggap trusted
            is_trusted = source.get('category') in ['islami', 'palestina']
            lang = source.get('language', 'id')

            for entry in feed.entries[:20]:
                title_raw = clean_html(entry.get('title', ''))
                summary_raw = clean_html(entry.get('summary', entry.get('description', '')))
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))

                # Filter kelayakan berita
                if is_islamic_content(title_raw, summary_raw, is_trusted_islamic_source=is_trusted):
                    title = title_raw
                    summary = summary_raw

                    # Terjemahkan berita internasional (EN) ke Bahasa Indonesia
                    if lang == 'en':
                        title = translate_text(title_raw)
                        summary = translate_text(summary_raw[:300])
                    else:
                        summary = summary[:300]

                    news_item = {
                        "title": title,
                        "summary": summary + ('...' if len(summary_raw) > 300 else ''),
                        "link": link,
                        "published": published,
                        "source_name": source['name'],
                        "category": source.get('category', 'islami'),
                        "is_translated": True if lang == 'en' else False
                    }
                    all_news.append(news_item)
        except Exception as e:
            print(f"[Error] Gagal mengambil dari sumber {source.get('name')}: {e}")

    # Hapus duplikat berita berdasarkan kemiripan Judul
    seen_titles = set()
    unique_news = []
    for news in all_news:
        title_key = news['title'].lower().strip()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_news.append(news)

    # Simpan hasil agregasi (Maksimal 80 berita terbaru)
    os.makedirs('data', exist_ok=True)
    with open('data/news.json', 'w', encoding='utf-8') as f:
        json.dump(unique_news[:80], f, ensure_ascii=False, indent=2)

    print(f"[Sukses] Berhasil menyimpan {len(unique_news[:80])} berita Islami ke data/news.json")

if __name__ == '__main__':
    fetch_news()
