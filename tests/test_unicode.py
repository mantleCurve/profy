import pytest

from profy import filter_text

CLEAN_UNICODE = [
    "\U0001F469\u200d\U0001F4BB hello",  # woman technologist (ZWJ sequence)
    "\U0001F468\u200d\U0001F469\u200d\U0001F467\u200d\U0001F466 family",  # family, three ZWJs
    "\U0001F3F3\ufe0f\u200d\U0001F308 pride",  # rainbow flag: variation selector + ZWJ
    "\U0001F1FA\U0001F1F8 \U0001F1E9\U0001F1EA flags",  # regional-indicator pairs
    "\U0001F44B\U0001F3FD wave",  # skin-tone modifier
    "❤\ufe0f love",  # heart + variation selector 16
    "1\ufe0f⃣ keycap",  # keycap sequence
    "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F scotland",  # tag sequence
    "café cafe\u0301 naïve",  # precomposed and combining accents
    "שלום עולם",  # Hebrew (RTL)
    "مرحبا بالعالم",  # Arabic (RTL)
    "\u200fright-to-left mark\u200e",
    "ｈｅｌｌｏ",  # full-width "hello"
    "日本語のテキスト",  # Japanese
    "soft\u00adhyphen and no\u200bspace",
]


@pytest.mark.parametrize("text", CLEAN_UNICODE)
def test_clean_unicode_text_is_returned_untouched(english, text):
    result = english.check(text)
    assert result.is_clean
    assert result.original == text
    assert result.clean == text


@pytest.mark.parametrize(
    "text, clean",
    [
        ("\U0001F469\u200d\U0001F4BB shit \U0001F1FA\U0001F1F8", "\U0001F469\u200d\U0001F4BB **** \U0001F1FA\U0001F1F8"),
        ("❤\ufe0f fuck ❤\ufe0f", "❤\ufe0f **** ❤\ufe0f"),
        ("\u200fshit\u200f", "\u200f****\u200f"),
        ("\ufeffshit", "\ufeff****"),
        ("שלום shit", "שלום ****"),
        ("ｈｉ shit", "ｈｉ ****"),
    ],
)
def test_only_matched_spans_change_around_unicode(english, text, clean):
    result = english.check(text)
    assert result.original == text
    assert result.clean == clean


@pytest.mark.parametrize(
    "text, clean, matched",
    [
        ("f\u200bu\u200bc\u200bk", "*******", "f\u200bu\u200bc\u200bk"),
        ("f\u2063uck this", "***** this", "f\u2063uck"),
        ("sh\u200di\u200dt!", "******!", "sh\u200di\u200dt"),
        ("fu\u00adck", "*****", "fu\u00adck"),
        ("sh\ufe0fit", "*****", "sh\ufe0fit"),
        ("s\U000E0020hit", "*****", "s\U000E0020hit"),
    ],
)
def test_invisible_character_obfuscation_masks_the_whole_original_span(english, text, clean, matched):
    result = english.check(text)
    assert result.original == text
    assert result.clean == clean
    match = result.matches[0]
    assert match.text == matched
    assert text[match.position : match.position + match.length] == matched


def test_positions_index_into_the_original_text(english):
    text = "\U0001F469\u200d\U0001F4BB\u200b ok f\u200buck"
    result = english.check(text)
    match = result.matches[0]
    assert text[match.position : match.position + match.length] == "f\u200buck"
    assert result.clean == text[: match.position] + "*" * match.length


def test_normalized_languages_map_back_to_the_original_span():
    result = filter_text("\U0001F600 Scheiße!", languages="german")
    assert result.clean == "\U0001F600 *******!"
    assert result.matches[0].text == "Scheiße"


def test_callback_masks_see_the_original_span():
    result = filter_text("f\u200buck", mask=lambda word, length: f"<{len(word)}:{length}>")
    assert result.clean == "<5:5>"
