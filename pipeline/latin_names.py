"""Latin-script names for places whose own name is written in another script (Thai, Chinese, Arabic...).

The app shows the Latin-script name on the main line and the place's own name underneath. This file finds that name,
best source first:

    1. name:en in OpenStreetMap (kept as the tag; handled by the app, nothing is stored here)
    2. int_name, or a name:xx-Latn tag, in OpenStreetMap
    3. Overture's English name, then any other Latin-script name it holds
    4. a spelling-out made by machine (Thai with pythainlp; Chinese, Japanese and the rest with small libraries)

A machine spelling is marked "_latin_m" so the app can treat it as a guess. The libraries are optional: without
them step 4 simply finds nothing.
"""
import re
import unicodedata

# Words that carry no identity when two spellings of a name are compared (English words and the Thai "jay" marker).
# The caller adds its own (the diet words).
GENERIC = {"restaurant", "cafe", "coffee", "bar", "food", "foods", "shop", "kitchen",
           "house", "the", "and", "che", "jay", "jeh", "ran", "raan", "ahan", "arhan", "aharn", "ahaan", "ahar", "pizza"}

_THAI = re.compile(r"[฀-๿]+")
_KANA = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[㐀-䶿一-鿿]+")


def is_latin(text):
    """True when the text has letters and every letter is Latin script (accents are fine)."""
    letters = [c for c in text or "" if c.isalpha()]
    return bool(letters) and all(unicodedata.name(c, "").startswith("LATIN") for c in letters)


def _title(text):
    return " ".join(w[:1].upper() + w[1:] for w in text.split())


def machine_spelling(name):
    """The name spelled out in Latin letters by machine, or None when it can't be (or is already Latin)."""
    if not name or is_latin(name):
        return None
    try:
        if _KANA.search(name):
            import pykakasi
            out = " ".join(w["hepburn"] for w in pykakasi.kakasi().convert(name))
        else:
            out = _HAN.sub(lambda m: " " + _han(m.group()) + " ", name)
            out = _THAI.sub(lambda m: " " + _thai(m.group()) + " ", out)
            from anyascii import anyascii   # Korean, Cyrillic, Greek, Arabic, Hindi and any other script
            out = anyascii(out)
    except ImportError:
        return None
    out = re.sub(r"\s+", " ", out).strip(" -")
    return _title(out) if is_latin(out) else None


def _thai(text):
    from pythainlp import word_tokenize
    from pythainlp.transliterate import romanize   # the Royal Thai General System, built into pythainlp
    # Thai has no spaces between words, so split first: "Ran Ming Choen", not "Ranmingchoen".
    return " ".join(romanize(word, engine="royin") for word in word_tokenize(text))


def _han(text):
    from pypinyin import lazy_pinyin
    return " ".join(lazy_pinyin(text))


def osm_latin(tags):
    """A Latin-script name from OpenStreetMap other than name:en: int_name, then name:xx-Latn."""
    for key in ["int_name"] + sorted(k for k in tags if re.fullmatch(r"name:[A-Za-z_]+-Latn", k)):
        if is_latin(tags.get(key, "")):
            return tags[key]
    return None


def overture_latin(common):
    """A Latin-script name from Overture's names.common (a map of language to name): English first, then the others."""
    if isinstance(common, dict) and "key" in common and "value" in common:   # DuckDB hands maps over as two lists
        common = dict(zip(common["key"], common["value"]))
    common = common or {}
    for lang in ["en"] + sorted(k for k in common if k != "en"):
        if is_latin(common.get(lang) or ""):
            return common[lang]
    return None


def latin_tags(name, tags, overture_common=None):
    """The tags to add so the app can show a Latin name: {"_latin": ..., "_latin_m": "1" if made by machine}, or {}.

    Nothing is added when the name is already Latin or OpenStreetMap's own name:en covers it."""
    if is_latin(name) or is_latin(tags.get("name:en", "")):
        return {}
    found = osm_latin(tags) or overture_latin(overture_common)
    if found:
        return {"_latin": found}
    spelled = machine_spelling(name)
    return {"_latin": spelled, "_latin_m": "1"} if spelled else {}


def latin_forms(name, tags, overture_common=None):
    """Every Latin-script form of a place's name, for comparing places: the name itself, name:en, and the rest."""
    forms = [name, tags.get("name:en"), tags.get("_latin"), osm_latin(tags), overture_latin(overture_common)]
    if not is_latin(name):
        forms.append(machine_spelling(name))
    return [f for f in dict.fromkeys(forms) if f and is_latin(f)]


def _sound_key(form, ignore=()):
    """A name squashed to letters that survive different romanisation habits (Ming Zhen / Ming Chen, Kwan / Khuan)."""
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", unicodedata.normalize("NFD", form).encode("ascii", "ignore").decode().lower()).split()
             if w not in GENERIC and w not in ignore]
    key = "".join(words)
    for a, b in (("zh", "j"), ("ch", "j"), ("kh", "k"), ("ph", "p"), ("th", "t"), ("ng", "n"), ("oe", "e"), ("ou", "o"), ("oo", "u"), ("y", "i"), ("h", "")):
        key = key.replace(a, b)
    return key


def same_sound(forms_a, forms_b, ignore=(), minimum=5):
    """True when some Latin form of one name and some Latin form of the other share a long stretch of the same
    sounds that makes up most of the shorter one (words in ignore don't count; one name may carry a branch name: "Ming Kwan" / "Ming Kwan Suan Dok")."""
    from difflib import SequenceMatcher
    for a in forms_a:
        for b in forms_b:
            ka, kb = _sound_key(a, ignore), _sound_key(b, ignore)
            if min(len(ka), len(kb)) < minimum:
                continue
            m = SequenceMatcher(None, ka, kb, autojunk=False).find_longest_match(0, len(ka), 0, len(kb))
            if m.size >= minimum and m.size >= 0.8 * min(len(ka), len(kb)):
                return True
    return False
