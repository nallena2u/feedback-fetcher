#!/usr/bin/env python3
"""
App Store Review Scraper
Scrapes reviews from Apple App Store or Google Play Store with star rating filters
"""
import ssl
# Keep TLS certificate verification ON. macOS' bundled Python can ship without a
# usable system CA store, which is why an earlier version disabled verification
# entirely (ssl._create_unverified_context) — but that exposes every HTTPS call
# to man-in-the-middle attacks. Instead, point the default context at certifi's
# CA bundle so verification stays enabled and still works on a bare macOS/frozen
# app. Falls back to the system default (still verified) if certifi is absent.
try:
    import os
    import certifi as _certifi
    _CA_BUNDLE = _certifi.where()

    def _verified_https_context(*args, **kwargs):
        return ssl.create_default_context(cafile=_CA_BUNDLE)

    ssl._create_default_https_context = _verified_https_context
    os.environ.setdefault("SSL_CERT_FILE", _CA_BUNDLE)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _CA_BUNDLE)
except Exception:
    pass  # certifi unavailable: keep the standard, still-verifying default
import csv
import argparse
import os
from datetime import datetime
from typing import List, Dict

def scrape_apple_reviews(app_id: str, country: str = 'us', star_ratings: List[int] = None,
                         max_reviews: int = 500, after_date: datetime.date = None):
    """
    Scrape reviews from Apple App Store
    
    Args:
        app_id: Apple app ID (numeric, e.g., '310633997' for WhatsApp)
        country: Country code (e.g., 'us', 'gb', 'ca')
        star_ratings: List of star ratings to include (1-5)
        max_reviews: Maximum number of reviews to fetch
    """
    try:
        from app_store_web_scraper import AppStoreEntry, AppStoreSession
        import certifi
    except ImportError:
        print("Error: app-store-web-scraper not installed")
        print("Install with: pip install app-store-web-scraper")
        return []
    
    if star_ratings is None:
        star_ratings = [1, 2, 3, 4, 5]
    
    print(f"Fetching Apple App Store reviews for app ID: {app_id}")
    print(f"Country: {country}, Star ratings: {star_ratings}")

    try:
        numeric_app_id = int(app_id)
    except ValueError:
        print("Error: Apple app ID must be numeric (e.g., 310633997)")
        return []

    # Ensure urllib3 can find CA bundle on macOS Python installs
    if not os.environ.get("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = certifi.where()

    # App Store web feed is limited to ~500 reviews per country
    session = AppStoreSession()
    entry = AppStoreEntry(app_id=numeric_app_id, country=country.lower(), session=session)

    filtered_reviews = []
    for review in entry.reviews(limit=max_reviews):
        rating = getattr(review, 'rating', 0)
        if rating not in star_ratings:
            continue
        review_date = getattr(review, 'date', None)
        if after_date and review_date and review_date.date() < after_date:
            continue
        filtered_reviews.append({
            'store': 'Apple App Store',
            'app_id': app_id,
            'review_id': getattr(review, 'id', ''),
            'user': getattr(review, 'user_name', 'Anonymous'),
            'rating': rating,
            'title': getattr(review, 'title', ''),
            'review': getattr(review, 'review', ''),
            'date': review_date or '',
            'version': '',  # not provided by app-store-web-scraper
            'country': country
        })
    
    print(f"Found {len(filtered_reviews)} reviews matching criteria")
    return filtered_reviews


def scrape_google_reviews(app_id: str, country: str = 'us', language: str = 'en',
                          star_ratings: List[int] = None, max_reviews: int = 500,
                          after_date: datetime.date = None):
    """
    Scrape reviews from Google Play Store
    
    Args:
        app_id: Google Play package name (e.g., 'com.whatsapp')
        country: Country code (e.g., 'us', 'gb', 'ca')
        language: Language code (e.g., 'en', 'es', 'fr')
        star_ratings: List of star ratings to include (1-5)
        max_reviews: Maximum number of reviews to fetch
    """
    try:
        from google_play_scraper import reviews, Sort
    except ImportError:
        print("Error: google-play-scraper not installed")
        print("Install with: pip install google-play-scraper")
        return []
    
    if star_ratings is None:
        star_ratings = [1, 2, 3, 4, 5]
    
    print(f"Fetching Google Play Store reviews for: {app_id}")
    print(f"Country: {country}, Language: {language}, Star ratings: {star_ratings}")
    
    # google-play-scraper can filter by score *server-side* via filter_score_with,
    # but only one score per call. The old code fetched the newest `max_reviews`
    # of ALL ratings and discarded non-matching ones locally — so selecting only
    # negative reviews spent most of the budget on positives that got thrown away
    # (e.g. 500 fetched, 218 negatives kept). Instead, fetch each wanted score
    # directly so the whole budget goes to the ratings you asked for.
    def _fetch(score):
        """Page through reviews (restricted to one star `score`, or all ratings
        when score is None) until `max_reviews` are collected or the feed runs out."""
        collected = []
        token = None
        while len(collected) < max_reviews:
            batch, token = reviews(
                app_id,
                lang=language,
                country=country,
                sort=Sort.NEWEST,
                count=min(200, max_reviews - len(collected)),
                filter_score_with=score,
                continuation_token=token,
            )
            collected.extend(batch)
            if not batch or token is None:
                break
        return collected[:max_reviews]

    if set(star_ratings) >= {1, 2, 3, 4, 5}:
        # All ratings wanted: a single unfiltered pass preserves global newest order.
        result = _fetch(None)
    else:
        # Fetch each wanted rating up to the budget, then keep the newest
        # `max_reviews` across the combined set.
        result = []
        for score in sorted(star_ratings):
            result.extend(_fetch(score))
        result.sort(key=lambda r: r.get('at') or datetime.min, reverse=True)
        result = result[:max_reviews]

    # Build rows. The score/date checks below are now largely redundant
    # (filtering already happened above) but kept as a safety net.
    filtered_reviews = [
        {
            'store': 'Google Play Store',
            'app_id': app_id,
            'review_id': review.get('reviewId', ''),
            'user': review.get('userName', 'Anonymous'),
            'rating': review.get('score', 0),
            'title': '',  # Google Play doesn't have review titles
            'review': review.get('content', ''),
            'date': review.get('at', ''),
            'version': review.get('reviewCreatedVersion', ''),
            'country': country,
            'thumbs_up': review.get('thumbsUpCount', 0),
            'reply_content': review.get('replyContent', ''),
            'reply_date': review.get('repliedAt', '')
        }
        for review in result
        if review.get('score') in star_ratings
        and (not after_date or not review.get('at') or review.get('at').date() >= after_date)
    ]
    
    print(f"Found {len(filtered_reviews)} reviews matching criteria")
    return filtered_reviews


def _csv_safe(value):
    """Neutralize spreadsheet formula injection. Review text is authored by
    third parties; a cell beginning with = + - @ (or a control char) can be
    executed as a formula by Excel/Sheets. Prefix such cells with an apostrophe
    so they're treated as plain text."""
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value


def export_to_csv(reviews: List[Dict], filename: str):
    """Export reviews to CSV file"""
    if not reviews:
        print("No reviews to export")
        return

    # Get all unique keys from reviews
    fieldnames = set()
    for review in reviews:
        fieldnames.update(review.keys())
    fieldnames = sorted(fieldnames)

    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for review in reviews:
            writer.writerow({k: _csv_safe(v) for k, v in review.items()})

    print(f"\nExported {len(reviews)} reviews to {filename}")


def main():
    parser = argparse.ArgumentParser(
        description='Scrape app store reviews with star rating filters',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scrape Apple App Store (only 1 and 2 star reviews)
  python review_scraper.py --store apple --app-id 310633997 --stars 1 2
  
  # Scrape Google Play Store (all ratings)
  python review_scraper.py --store google --app-id com.whatsapp --stars 1 2 3 4 5
  
  # Scrape with custom output file
  python review_scraper.py --store google --app-id com.instagram.android --stars 1 --output bad_reviews.csv
        """
    )
    
    parser.add_argument('--store', choices=['apple', 'google'], required=True,
                       help='App store to scrape (apple or google)')
    parser.add_argument('--app-id', required=True,
                       help='App ID (numeric for Apple, package name for Google)')
    parser.add_argument('--stars', nargs='+', type=int, choices=[1, 2, 3, 4, 5],
                       default=[1, 2, 3, 4, 5],
                       help='Star ratings to include (default: all)')
    parser.add_argument('--country', default='us',
                       help='Country code (default: us)')
    parser.add_argument('--language', default='en',
                       help='Language code for Google Play (default: en)')
    parser.add_argument('--max-reviews', type=int, default=500,
                       help='Maximum number of reviews to fetch (default: 500)')
    parser.add_argument('--output', default=None,
                       help='Output CSV filename (default: auto-generated)')
    parser.add_argument('--after-date', default=None,
                       help='Only include reviews on/after this date (YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    after_date = None
    if args.after_date:
        try:
            after_date = datetime.fromisoformat(args.after_date).date()
        except ValueError:
            print("Error: --after-date must be YYYY-MM-DD")
            return

    # Scrape reviews
    if args.store == 'apple':
        reviews = scrape_apple_reviews(
            app_id=args.app_id,
            country=args.country,
            star_ratings=args.stars,
            max_reviews=args.max_reviews,
            after_date=after_date
        )
    else:  # google
        reviews = scrape_google_reviews(
            app_id=args.app_id,
            country=args.country,
            language=args.language,
            star_ratings=args.stars,
            max_reviews=args.max_reviews,
            after_date=after_date
        )
    
    # Generate output filename if not provided
    if args.output is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        stars_str = '_'.join(map(str, sorted(args.stars)))
        args.output = f"{args.store}_reviews_{args.app_id}_stars{stars_str}_{timestamp}.csv"
    
    # Export to CSV
    export_to_csv(reviews, args.output)


if __name__ == '__main__':
    main()
