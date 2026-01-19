#!/usr/bin/env python3
"""
Cache Migration Script - Migrate JSON cache files to SQLite database
Run this once to migrate existing cache data
"""

import json
from pathlib import Path
from datetime import datetime, timedelta
from cache_manager import CacheManager
from trade_journal import initialize_enhanced_database

# Cache file mappings with TTL in minutes
CACHE_FILES = {
    'schwab_token.json': {'ttl': 60*24*30, 'data_type': 'auth'},  # 30 days
    'open_trade_quotes_cache.json': {'ttl': 5, 'data_type': 'quotes'},
    'support_resistance_cache.json': {'ttl': 60*24*7, 'data_type': 'support_resistance'},  # 7 days
    'opp_analysis_cache.json': {'ttl': 60, 'data_type': 'grok'},
    'grok_market_sentiment.json': {'ttl': 60, 'data_type': 'grok'},
    'grok_sentiment_cache.json': {'ttl': 60, 'data_type': 'grok'},
    'simple_scanner_cache.json': {'ttl': 60*24, 'data_type': 'scanner'},  # 24 hours
    'leaps_cache.json': {'ttl': 60*24, 'data_type': 'scanner'},
    '0dte_spreads_cache.json': {'ttl': 60*24, 'data_type': 'scanner'},
}


def migrate_cache_files(dry_run: bool = False) -> dict:
    """
    Migrate JSON cache files to database

    Args:
        dry_run: If True, only report what would be migrated

    Returns:
        Dictionary with migration stats
    """
    stats = {
        'total_files': 0,
        'migrated_files': 0,
        'total_entries': 0,
        'errors': []
    }

    print("🔄 Starting cache migration to database...")

    if dry_run:
        print("   [DRY RUN MODE - No changes will be made]")

    # Initialize database schema
    if not dry_run:
        print("\n1. Initializing enhanced database schema...")
        initialize_enhanced_database()

    # Initialize cache manager
    cache = CacheManager()

    print("\n2. Migrating cache files...")

    for filename, config in CACHE_FILES.items():
        stats['total_files'] += 1
        file_path = Path(filename)

        if not file_path.exists():
            print(f"   ⏭️  Skipping {filename} (not found)")
            continue

        try:
            print(f"   📄 Processing {filename}...")

            with open(file_path, 'r') as f:
                data = json.load(f)

            # Determine how to migrate based on structure
            entries_migrated = 0

            if isinstance(data, dict):
                # If it has a 'cache_time' field, it's a time-stamped cache
                if 'cache_time' in data:
                    cache_key = filename.replace('.json', '')

                    if not dry_run:
                        cache.set(
                            key=cache_key,
                            value=data,
                            ttl_minutes=config['ttl'],
                            data_type=config['data_type']
                        )

                    entries_migrated = 1
                    print(f"      ✓ Migrated as single entry: {cache_key}")

                # Multiple keyed entries
                else:
                    for key, value in data.items():
                        cache_key = f"{filename.replace('.json', '')}_{key}"

                        if not dry_run:
                            cache.set(
                                key=cache_key,
                                value=value,
                                ttl_minutes=config['ttl'],
                                data_type=config['data_type']
                            )

                        entries_migrated += 1

                    print(f"      ✓ Migrated {entries_migrated} entries")

            elif isinstance(data, list):
                # List of items
                cache_key = filename.replace('.json', '')

                if not dry_run:
                    cache.set(
                        key=cache_key,
                        value=data,
                        ttl_minutes=config['ttl'],
                        data_type=config['data_type']
                    )

                entries_migrated = 1
                print(f"      ✓ Migrated list with {len(data)} items")

            stats['migrated_files'] += 1
            stats['total_entries'] += entries_migrated

        except Exception as e:
            error_msg = f"Error migrating {filename}: {e}"
            print(f"      ❌ {error_msg}")
            stats['errors'].append(error_msg)

    print("\n3. Migration Summary:")
    print(f"   Total files processed: {stats['total_files']}")
    print(f"   Successfully migrated: {stats['migrated_files']}")
    print(f"   Total cache entries: {stats['total_entries']}")

    if stats['errors']:
        print(f"   ⚠️  Errors: {len(stats['errors'])}")
        for error in stats['errors']:
            print(f"      - {error}")

    if not dry_run:
        # Get cache stats
        cache_stats = cache.get_stats()
        print("\n4. Database Cache Stats:")
        print(f"   Total entries: {cache_stats['total_entries']}")
        print(f"   Active entries: {cache_stats['active_entries']}")
        print(f"   By type: {cache_stats['by_type']}")

    return stats


def backup_cache_files():
    """Backup existing cache files before deletion"""
    backup_dir = Path("cache_backup")
    backup_dir.mkdir(exist_ok=True)

    print(f"\n📦 Backing up cache files to {backup_dir}/...")
    backed_up = 0

    for filename in CACHE_FILES.keys():
        file_path = Path(filename)
        if file_path.exists():
            backup_path = backup_dir / filename
            backup_path.write_bytes(file_path.read_bytes())
            backed_up += 1
            print(f"   ✓ Backed up {filename}")

    print(f"   Backed up {backed_up} files")
    return backed_up


def remove_old_cache_files(confirm: bool = False):
    """
    Remove old JSON cache files after migration

    Args:
        confirm: Must be True to actually delete files
    """
    if not confirm:
        print("\n⚠️  Skipping file deletion (confirmation required)")
        print("   Run with confirm=True to delete old cache files")
        return 0

    print("\n🗑️  Removing old cache files...")
    removed = 0

    for filename in CACHE_FILES.keys():
        file_path = Path(filename)
        if file_path.exists():
            try:
                file_path.unlink()
                removed += 1
                print(f"   ✓ Removed {filename}")
            except Exception as e:
                print(f"   ❌ Error removing {filename}: {e}")

    print(f"   Removed {removed} cache files")
    return removed


def main():
    """Main migration workflow"""
    print("=" * 60)
    print("CACHE MIGRATION TO DATABASE")
    print("=" * 60)

    # Step 1: Dry run to see what will be migrated
    print("\n" + "=" * 60)
    print("STEP 1: DRY RUN (Preview)")
    print("=" * 60)
    migrate_cache_files(dry_run=True)

    # Step 2: Ask for confirmation
    print("\n" + "=" * 60)
    print("STEP 2: Confirmation")
    print("=" * 60)
    response = input("\nProceed with migration? (yes/no): ").strip().lower()

    if response != 'yes':
        print("❌ Migration cancelled")
        return

    # Step 3: Backup files
    print("\n" + "=" * 60)
    print("STEP 3: Backup")
    print("=" * 60)
    backup_cache_files()

    # Step 4: Perform migration
    print("\n" + "=" * 60)
    print("STEP 4: Migration")
    print("=" * 60)
    stats = migrate_cache_files(dry_run=False)

    # Step 5: Verify
    print("\n" + "=" * 60)
    print("STEP 5: Verification")
    print("=" * 60)
    cache = CacheManager()
    cache_stats = cache.get_stats()

    if cache_stats['active_entries'] > 0:
        print(f"✅ Migration successful! {cache_stats['active_entries']} active cache entries")
    else:
        print("⚠️  Warning: No active cache entries found")

    # Step 6: Ask about cleanup
    print("\n" + "=" * 60)
    print("STEP 6: Cleanup (Optional)")
    print("=" * 60)
    print("Old cache files have been backed up to cache_backup/")
    response = input("Remove old JSON cache files? (yes/no): ").strip().lower()

    if response == 'yes':
        remove_old_cache_files(confirm=True)
    else:
        print("   Keeping old cache files (you can delete them manually later)")

    print("\n" + "=" * 60)
    print("✅ MIGRATION COMPLETE!")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Update your code to use cache_manager instead of JSON files")
    print("2. Test that all functionality works with database cache")
    print("3. Delete cache_backup/ folder when confident everything works")


if __name__ == "__main__":
    main()
