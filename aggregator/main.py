    )

    valid_articles = []

    for article in merged.values():

        try:
            date = datetime.fromisoformat(
                article["publishedAt"].replace("Z", "+00:00")
            )

            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)

            if date < cutoff:
                continue

        except Exception:
            pass

        valid_articles.append(article)

    valid_articles.sort(
        key=lambda item: item.get("publishedAt", ""),
        reverse=True
    )

    return valid_articles[:MAX_ARTICLES]


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
        raise RuntimeError("Daftar RSS kosong.")

    if not categories:
        raise RuntimeError("Kategori berita kosong.")

    previous_articles = load_previous_articles()

    all_articles = []
    source_stats = []

    for source in sources:

        articles, stats = fetch_source(
            source,
            categories
        )

        all_articles.extend(articles)
        source_stats.append(stats)

    successful = sum(
        1 for item in source_stats
        if item["status"] == "success"
    )

    failed = len(source_stats) - successful

    final_articles = merge_articles(
        all_articles,
        previous_articles
    )

    if successful == 0:
        status = "error"
    elif failed > 0:
        status = "partial"
    else:
        status = "success"

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
        "articles": final_articles
    }

    save_json(
        OUTPUT_FILE,
        result
    )

    logging.info("--------------------------------")
    logging.info("Status: %s", status)
    logging.info("Sumber berhasil: %d/%d", successful, len(sources))
    logging.info("Berita baru lolos filter: %d", len(all_articles))
    logging.info("Total berita tersimpan: %d", len(final_articles))
    logging.info("--------------------------------")

    # Jika semua sumber gagal, workflow dibuat gagal
    # agar masalah terlihat di GitHub Actions.
    if successful == 0:
        raise RuntimeError(
            "Semua sumber RSS gagal diakses."
        )


if __name__ == "__main__":
    main()
