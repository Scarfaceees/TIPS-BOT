"""Trvaly archiv rozboru a skenu: co se v panelu prepisuje (posledni sken, rozbor zapasu), se sem navic ulozi
a nikdy nemaze. Zapis jen pri zmene obsahu (stejny sken dvakrat = jeden zaznam), data komprimovana (zlib).

Pouziva ho hlavni databaze (storage.Ledger.archive) i databaze Profilu 4 (profil4.store.Store.archive)."""

from __future__ import annotations

import hashlib
import zlib


def pack(text: str) -> tuple[bytes, str]:
    raw = text.encode("utf-8")
    return zlib.compress(raw, 6), hashlib.sha1(raw).hexdigest()


def unpack(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")
