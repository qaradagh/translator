import os
import tempfile

from gametrans.cache import TranslationCache


def test_memory_hit_uses_normalised_key():
    cache = TranslationCache(path=None, memory_size=16)
    cache.put("Hello there", "سلام")
    entry = cache.get("hello  THERE!!")
    assert entry is not None and entry.translation == "سلام"
    assert cache.hits == 1 and cache.misses == 0


def test_miss_counts():
    cache = TranslationCache(path=None)
    assert cache.get("nothing here") is None
    assert cache.misses == 1
    assert cache.hit_rate == 0.0


def test_lru_eviction():
    cache = TranslationCache(path=None, memory_size=3)
    for i in range(5):
        cache.put(f"line {i}", f"خط {i}")
    assert cache.size() == 3
    assert cache.get("line 0") is None      # evicted
    assert cache.get("line 4") is not None  # most recent survives


def test_empty_values_are_not_cached():
    cache = TranslationCache(path=None)
    cache.put("something", "   ")
    cache.put("", "سلام")
    assert cache.size() == 0


def test_disk_cache_persists_across_instances():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.sqlite3")

        first = TranslationCache(path=path, memory_size=8)
        first.put("Open the gate", "دروازه را باز کن", provider="gemini")
        first.close()

        second = TranslationCache(path=path, memory_size=8)
        entry = second.get("open the GATE")
        assert entry is not None
        assert entry.translation == "دروازه را باز کن"
        assert entry.provider == "gemini"
        second.close()


def test_disk_cache_is_scoped_by_target_language():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.sqlite3")

        fa = TranslationCache(path=path, target_language="Persian (Farsi)")
        fa.put("Hello", "سلام")
        fa.close()

        de = TranslationCache(path=path, target_language="German")
        assert de.get("Hello") is None
        de.close()


def test_unwritable_path_falls_back_to_memory():
    cache = TranslationCache(path="/nonexistent-dir-xyz/cache.sqlite3")
    cache.put("Hello", "سلام")
    assert cache.get("hello") is not None
