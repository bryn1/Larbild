"""Unit tests for the lesson manuscript data model (lb-lesson, app.lesson).

Pure data + validation — no I/O, no GPU, no DB. Covers the manuscript schema
the Opus step produces: Caption rung resolution (the 3-level bildtext), Beat
parsing + the spoken-line (narration + connection) scaffold, Lesson parse +
validation (subject/area/beats/kind/level/motion), the for_level pinning that
drives the three level-versions, and the continuous narration_script.
"""
from __future__ import annotations

import pytest

from app.lesson import (
    Beat,
    Caption,
    Lesson,
    LessonError,
    LEVELS,
    LEVEL_TO_CAPTION_RUNG,
    MOTIONS,
)


# ── Caption (the bildtext, short/longer/long = enkel/utvecklad/valutvecklad) ──


def test_caption_parse_flat_string_used_at_every_rung():
    c = Caption.parse("Ljud är en våg.")
    assert c.short == c.longer == c.long == "Ljud är en våg."
    assert c.for_level("enkel") == "Ljud är en våg."
    assert c.for_level("valutvecklad") == "Ljud är en våg."


def test_caption_parse_three_rungs_map_to_levels():
    c = Caption.parse({"short": "kort", "longer": "längre", "long": "lång"})
    # short=enkel, longer=utvecklad, long=valutvecklad (the owner's mapping)
    assert c.for_level("enkel") == "kort"
    assert c.for_level("utvecklad") == "längre"
    assert c.for_level("valutvecklad") == "lång"


def test_caption_backfills_missing_rungs_so_any_level_resolves():
    # only 'short' given → longer/long fall back to it (never blank)
    c = Caption.parse({"short": "bara kort"})
    assert c.for_level("utvecklad") == "bara kort"
    assert c.for_level("valutvecklad") == "bara kort"
    assert not c.is_empty()


def test_caption_empty_is_empty():
    assert Caption.parse(None).is_empty()
    assert Caption.parse("   ").is_empty()


def test_caption_bad_type_raises():
    with pytest.raises(LessonError):
        Caption.parse(123)


def test_level_to_caption_rung_mapping_is_the_owner_spec():
    assert LEVEL_TO_CAPTION_RUNG == {
        "enkel": "short", "utvecklad": "longer", "valutvecklad": "long"
    }


# ── Beat ─────────────────────────────────────────────────────────────────────


def _beat_dict(**kw):
    base = {
        "begrepp": "Ljudvåg",
        "forklaring": "Ljud är en våg.",
        "vardagligt_exempel": "Knäpp med fingrarna.",
        "image_prompt": "en ljudvåg",
        "caption": {"short": "kort", "longer": "längre", "long": "lång"},
        "narration": "Vi börjar med ljudvågen.",
        "connection": "Den fångas upp av örat.",
        "motion": "still",
    }
    base.update(kw)
    return base


def test_beat_requires_begrepp():
    with pytest.raises(LessonError):
        Beat.parse({"begrepp": "  "}, 0)


def test_beat_parse_full():
    b = Beat.parse(_beat_dict(), 2)
    assert b.index == 2
    assert b.begrepp == "Ljudvåg"
    assert b.motion == "still"
    assert b.image_type == "illustration"  # default
    assert b.caption_for("enkel") == "kort"


def test_beat_bad_motion_raises():
    with pytest.raises(LessonError):
        Beat.parse(_beat_dict(motion="teleport"), 0)


def test_beat_motion_short_clip_allowed():
    b = Beat.parse(_beat_dict(motion="short_clip"), 0)
    assert b.motion == "short_clip"
    assert "short_clip" in MOTIONS


def test_beat_bad_duration_raises():
    with pytest.raises(LessonError):
        Beat.parse(_beat_dict(duration_s=-3), 0)
    with pytest.raises(LessonError):
        Beat.parse(_beat_dict(duration_s="abc"), 0)


def test_beat_narration_appends_connection():
    b = Beat.parse(_beat_dict(), 0)
    line = b.narration_for("enkel")
    assert "Vi börjar med ljudvågen." in line
    assert "Den fångas upp av örat." in line  # connection is spoken as the tail


def test_beat_narration_scaffolds_when_absent():
    # no explicit narration → a neutral line from begrepp + förklaring + exempel
    b = Beat.parse(_beat_dict(narration=""), 0)
    line = b.narration_for("enkel")
    assert "Ljudvåg" in line
    assert "Ljud är en våg." in line  # förklaring folded in
    assert line.strip()  # never silent


def test_beat_image_prompt_scaffold_when_absent_names_no_lora():
    b = Beat.parse(_beat_dict(image_prompt=""), 0)
    p = b.image_prompt_or_scaffold("fysik", "ljud")
    assert "fysik" in p and "ljud" in p and "Ljudvåg" in p
    assert "lora" not in p.lower()  # the SFW guard: a prompt never names a LoRA


# ── Lesson ───────────────────────────────────────────────────────────────────


def _manuscript(**kw):
    base = {
        "subject": "fysik",
        "area": "ljud",
        "level": "enkel",
        "kind": "lesson",
        "narration_voice": "sv-standard",
        "duration_target_s": 420,
        "beats": [_beat_dict(), _beat_dict(begrepp="Frekvens", narration="Frekvensen avgör tonhöjden.")],
    }
    base.update(kw)
    return base


def test_lesson_parse_full():
    les = Lesson.parse(_manuscript())
    assert les.subject == "fysik"
    assert les.area == "ljud"
    assert les.kind == "lesson"
    assert les.level == "enkel"
    assert les.begrepp_count == 2
    assert les.duration_target_s == 420


def test_lesson_requires_subject_and_area():
    with pytest.raises(LessonError):
        Lesson.parse(_manuscript(subject=""))
    with pytest.raises(LessonError):
        Lesson.parse(_manuscript(area=""))


def test_lesson_requires_nonempty_beats():
    with pytest.raises(LessonError):
        Lesson.parse(_manuscript(beats=[]))
    with pytest.raises(LessonError):
        Lesson.parse(_manuscript(beats="nope"))


def test_lesson_bad_kind_raises():
    with pytest.raises(LessonError):
        Lesson.parse(_manuscript(kind="documentary"))


def test_lesson_kind_defaults_to_lesson():
    m = _manuscript()
    del m["kind"]
    assert Lesson.parse(m).kind == "lesson"


def test_lesson_accepts_aa_spelling_of_level():
    les = Lesson.parse(_manuscript(level="välutvecklad"))
    assert les.level == "valutvecklad"  # normalised to the ASCII slug


def test_lesson_bad_level_raises():
    with pytest.raises(LessonError):
        Lesson.parse(_manuscript(level="expert"))


def test_lesson_level_optional_for_family_short():
    m = _manuscript(kind="short", level=None)
    les = Lesson.parse(m)
    assert les.kind == "short"
    assert les.level is None


def test_lesson_for_level_pins_level_and_keeps_beats():
    les = Lesson.parse(_manuscript(level="enkel"))
    val = les.for_level("valutvecklad")
    assert val.level == "valutvecklad"
    assert val.beats is les.beats  # beats reused — only the level flips
    # the SAME manuscript now resolves the 'long' caption rung at the new level
    assert val.beats[0].caption_for(val.level) == "lång"


def test_lesson_for_level_rejects_bad_level():
    les = Lesson.parse(_manuscript())
    with pytest.raises(LessonError):
        les.for_level("nope")


def test_three_levels_yield_three_distinct_captions():
    """The 3-level deliverable: one manuscript → three different bildtexter."""
    les = Lesson.parse(_manuscript())
    seen = {}
    for lvl in LEVELS:
        pinned = les.for_level(lvl)
        seen[lvl] = pinned.beats[0].caption_for(lvl)
    assert seen["enkel"] == "kort"
    assert seen["utvecklad"] == "längre"
    assert seen["valutvecklad"] == "lång"
    assert len(set(seen.values())) == 3  # genuinely distinct per level


def test_lesson_narration_script_is_continuous_and_ordered():
    les = Lesson.parse(_manuscript())
    script = les.narration_script()
    # both beats' spoken lines, in order, joined into one continuous script
    assert "Vi börjar med ljudvågen." in script
    assert "Frekvensen avgör tonhöjden." in script
    assert script.index("ljudvågen") < script.index("Frekvensen")


def test_lesson_display_title_derives_when_absent():
    les = Lesson.parse(_manuscript(title=None))
    t = les.display_title()
    assert "ljud" in t and "enkel" in t  # area + level
    short = Lesson.parse(_manuscript(kind="short", title=None, level=None))
    assert "kort" in short.display_title()


def test_lesson_as_dict_roundtrips_through_parse():
    les = Lesson.parse(_manuscript())
    again = Lesson.parse(les.as_dict())
    assert again.subject == les.subject
    assert again.begrepp_count == les.begrepp_count
    assert again.beats[0].caption_for("utvecklad") == "längre"
