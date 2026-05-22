import profy
from profy import ProfanityFilter, Severity, check_text, clean_text, filter_text


def test_version_is_exported():
    assert profy.__version__ == "0.1.0a2"


def test_straight_match_masks_text():
    result = filter_text("This is a fucking sentence")

    assert result.is_offensive
    assert result.count == 1
    assert result.unique_words == ["fucking"]
    assert result.clean == "This is a ******* sentence"


def test_obfuscated_match():
    result = filter_text("This is a f-uuck!ng sentence")

    assert result.is_offensive
    assert result.clean == "This is a ********* sentence"


def test_false_positive_words_are_not_flagged():
    for word in ["Scunthorpe", "Penistone", "assignment", "passion", "classroom"]:
        result = filter_text(word)
        assert result.is_clean, word
        assert result.clean == word


def test_invisible_characters_are_removed_before_matching():
    result = filter_text("f\u2063uck this")

    assert result.is_offensive
    assert result.clean == "**** this"


def test_allow_and_block_lists():
    assert filter_text("heck", allow=["heck"]).is_clean

    result = filter_text("ship it", block=["ship"])
    assert result.is_offensive
    assert result.clean == "**** it"


def test_severity_filter():
    assert filter_text("damn", minimum_severity=Severity.HIGH).is_clean
    assert filter_text("fuck", minimum_severity="high").is_offensive


def test_reusable_filter_and_helpers():
    shield = ProfanityFilter(mask="#")

    assert shield.clean("fuck") == "####"
    assert check_text("f**k")
    assert clean_text("shit") == "****"


def test_callback_mask_and_result_shape():
    result = filter_text("shit", mask=lambda word, length: f"[{word}:{length}]")

    assert result.clean == "[shit:4]"
    assert result.to_dict()["words"] == [
        {
            "text": "shit",
            "base": "shit",
            "severity": "high",
            "position": 0,
            "length": 4,
            "language": "english",
        }
    ]


def test_spanish_language_normalization():
    result = filter_text("maldición", languages="spanish")

    assert result.is_offensive
    assert result.clean == "*********"


def test_german_normalization_masks_original_span():
    result = filter_text("Das ist scheisse", languages="german")

    assert result.is_offensive
    assert result.clean == "Das ist ********"
    assert result.matches[0].text == "scheisse"


def test_vowel_elided_obfuscations_are_caught():
    for text in ["fck", "sht", "dmn", "what the fck", "sht happens", "oh dmn it"]:
        result = filter_text(text)
        assert result.is_offensive, text


def test_vowel_elision_does_not_flag_unrelated_short_tokens():
    for text in ["fc", "ss", "ck", "FC Barcelona", "miss the bus", "a class of students"]:
        result = filter_text(text)
        assert result.is_clean, text


def test_all_languages_uses_bundled_dictionaries():
    result = filter_text("Putain de merde", all_languages=True)

    assert result.is_offensive
    assert result.clean == "****** de *****"
