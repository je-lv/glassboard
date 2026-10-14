"""Source hygiene: no invisible, bidi or line-separator characters in the repository.

Glassboard renders such characters visibly for participants; its own source must
not contain them raw either (Trojan Source, CVE-2021-42574). Tests that need
them write `\\uXXXX` escapes."""
import pathlib

ROOT = pathlib.Path(__file__).parents[1]
BAD = {0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200B, 0x200C, 0x200D,
       0x200E, 0x200F, 0xFEFF, 0x2028, 0x2029, 0xFFFD, 0x00AD, 0x061C}
SUFFIXES = {".py", ".sql", ".md", ".toml", ".sh", ".in", ".cfg", ".txt", ".service", ".conf", ".json", ".yaml", ".yml"}


def test_no_invisible_or_bidi_characters_in_source():
    found = []
    for p in ROOT.rglob("*"):
        if any(part.startswith(".venv") or part in (".git", "__pycache__", ".pytest_cache", ".hypothesis") for part in p.parts):
            continue
        if not p.is_file() or (p.suffix not in SUFFIXES and p.name not in ("Caddyfile",)):
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            bad = sorted({f"U+{ord(c):04X}" for c in line if ord(c) in BAD or (ord(c) < 0x20 and c != "\t")})
            if bad:
                found.append(f"{p.relative_to(ROOT)}:{n} {bad}")
    assert not found, "\n".join(found)
