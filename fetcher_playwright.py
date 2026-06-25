"""
BalticRadar - autoplius listing/search-page parser (the CRAWLER core).
Extracts detail-page URLs + ad_ids + pagination from a search results page.
Works on raw HTML or rendered text because it keys on the ad URL pattern,
which is identical in both. Writes NOTHING.
"""
import re

# autoplius detail URLs end in ...-<digits>.html across all language paths.
AD_URL = re.compile(r'https://[a-z]{2}\.autoplius\.lt/(?:sludinajumi|skelbimai|ads|objavlenija)/[^\s"\')]+?-(\d+)\.html')
PAGE = re.compile(r'page_nr=(\d+)')


def parse_listing_page(text, lang="lv"):
    seen = {}
    for m in AD_URL.finditer(text):
        url, ad_id = m.group(0), m.group(1)
        if lang and f"://{lang}." not in url:
            continue  # keep one language to avoid 4x duplicates
        if ad_id not in seen:
            seen[ad_id] = url
    ads = [{"ad_id": "A" + aid, "ad_id_num": aid, "url": url} for aid, url in seen.items()]
    pages = sorted({int(p) for p in PAGE.findall(text)})
    max_page = max(pages) if pages else 1
    return {"ads": ads, "count": len(ads), "max_page_seen": max_page}


def page_url(base, n):
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}page_nr={n}"
