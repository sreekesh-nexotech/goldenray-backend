"""Local storage backend: atomic writes, path-traversal refusal, keys."""

import re

import pytest

from media.services.storage import LocalStorage, StorageError, StorageNotFound, new_key, public_storage, storage_for, thumbnail_key


def test_new_key_layout():
    assert re.match(r"^library/\d{4}/\d{2}/[0-9a-f]{32}\.png$", new_key("", ".png"))
    assert new_key("brand/logos", ".jpg").startswith("brand/logos/")
    assert thumbnail_key("library/2026/09/abc.jpg") == "library/2026/09/abc.thumb.webp"


def test_round_trip_and_delete(tmp_path):
    storage = LocalStorage(tmp_path, "/media/public/", name="t")
    storage.save("a/b/c.bin", b"payload")
    assert storage.exists("a/b/c.bin") and storage.read("a/b/c.bin") == b"payload"
    assert oct(storage.path("a/b/c.bin").stat().st_mode & 0o777) == "0o640"
    assert storage.url("a/b/c.bin") == "/media/public/a/b/c.bin"
    assert list(tmp_path.joinpath("a", "b").iterdir()) == [tmp_path / "a" / "b" / "c.bin"]  # no temp files left
    storage.delete("a/b/c.bin")
    storage.delete("a/b/c.bin")  # idempotent
    with pytest.raises(StorageNotFound):
        storage.read("a/b/c.bin")


@pytest.mark.parametrize("key", ["", "../etc/passwd", "a/../../x", "/abs/path", "a//b", "a\\b", "./x"])
def test_keys_cannot_escape_the_root(tmp_path, key):
    with pytest.raises(StorageError):
        LocalStorage(tmp_path, None, name="t").save(key, b"x")


def test_private_storage_has_no_url(settings):
    assert storage_for("PRIVATE").url("library/2026/09/x.pdf") is None
    assert storage_for("PUBLIC").url("library/2026/09/x.pdf") == "/media/public/library/2026/09/x.pdf"


def test_unknown_public_backend(settings):
    from django.core.exceptions import ImproperlyConfigured

    settings.MEDIA_PUBLIC_BACKEND = "s3"
    with pytest.raises(ImproperlyConfigured):
        public_storage()


def test_public_media_is_served_only_while_debugging(settings):
    from flarize.urls import public_media_debug_patterns

    assert public_media_debug_patterns() == []
    settings.DEBUG = True
    [pattern] = public_media_debug_patterns()
    assert pattern.pattern.regex.match("media/public/library/2026/09/x.jpg")
    settings.MEDIA_PUBLIC_BACKEND = "bunny"
    assert public_media_debug_patterns() == []


def test_sibling_public_url():
    from media.services.storage import sibling_public_url

    assert sibling_public_url("https://cdn.test/library/2026/09/a b.jpg", "library/2026/09/a b.jpg", "library/2026/09/a b.thumb.webp") is None  # not quoted
    assert sibling_public_url("https://cdn.test/library/2026/09/a%20b.jpg", "library/2026/09/a b.jpg", "library/2026/09/a b.thumb.webp") == "https://cdn.test/library/2026/09/a%20b.thumb.webp"
    assert sibling_public_url("", "k.jpg", "k.thumb.webp") is None and sibling_public_url("https://cdn.test/k.jpg", "k.jpg", "") is None
