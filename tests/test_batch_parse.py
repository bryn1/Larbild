"""Tests for the CSV/txt batch parse + per-row validation (lb-zht, P5.1).

Acceptance criteria, exercised directly against :func:`app.batch.parse_batch`
(Flask-free, no GPU/Redis/DB):
  1. a header CSV yields N validated render specs (valid rows ready to fan out);
  2. invalid rows are reported with line numbers + reasons, valid rows still parse;
  3. taxonomy validation: unknown subject / area-not-in-subject / unknown
     imageType|videoType are each rejected per-row;
  4. a client-supplied ``lora`` column is dropped (never carried into a spec);
  5. a malformed file is rejected gracefully (no exception escapes);
  6. a header-less positional ``.txt`` table parses;
  7. ``count`` expansion + its caps.
"""
from __future__ import annotations

import app.batch as batch
from app.batch import parse_batch


# ── 1. valid header CSV → N specs ───────────────────────────────────────────


def test_header_csv_parses_n_valid_rows():
    text = (
        "subject,area,imageType,prompt\n"
        "fysik,ljud,illustration,a transverse wave\n"
        "biologi,celler,foto,a plant cell\n"
        "kemi,materia,diagram,the periodic table\n"
    )
    spec = parse_batch(text)
    assert spec.valid_count == 3
    assert spec.rejected_count == 0
    # specs are shaped for enqueue_job(**spec) (image_type snake_case, kind set)
    first = spec.jobs[0]
    assert first["subject"] == "fysik"
    assert first["area"] == "ljud"
    assert first["image_type"] == "illustration"
    assert first["kind"] == "bild"
    assert first["prompt"] == "a transverse wave"
    assert first["video_type"] is None
    assert first["count"] == 1


def test_header_csv_column_reorder_and_extra_columns():
    # header order is free; an unknown extra column is ignored (forward-compat)
    text = (
        "prompt,imageType,subject,area,note\n"
        "a magnet,foto,fysik,el-magnet,ignore-me\n"
    )
    spec = parse_batch(text)
    assert spec.valid_count == 1
    assert spec.rejected_count == 0
    assert spec.jobs[0]["image_type"] == "foto"
    assert spec.jobs[0]["subject"] == "fysik"


def test_quoted_prompt_with_comma_in_header_csv():
    text = (
        'subject,area,imageType,prompt\n'
        'fysik,ljud,illustration,"a wave, in air"\n'
    )
    spec = parse_batch(text)
    assert spec.valid_count == 1
    assert spec.jobs[0]["prompt"] == "a wave, in air"


# ── 2. invalid rows reported with line numbers + reasons, valid still parse ──


def test_invalid_rows_reported_with_line_numbers_valid_still_parse():
    text = (
        "subject,area,imageType,prompt\n"  # line 1 (header)
        "fysik,ljud,illustration,good one\n"  # line 2 — valid
        "nope,ljud,foto,bad subject\n"  # line 3 — invalid subject
        "biologi,celler,foto,another good\n"  # line 4 — valid
    )
    spec = parse_batch(text)
    assert spec.valid_count == 2
    assert spec.rejected_count == 1
    err = spec.errors[0]
    assert err.line == 3  # header is line 1, so the bad data row is line 3
    assert "unknown subject" in err.error
    assert "nope" in err.error


def test_missing_prompt_rejected_with_reason():
    text = (
        "subject,area,imageType,prompt\n"
        "fysik,ljud,foto,\n"  # empty prompt
    )
    spec = parse_batch(text)
    assert spec.valid_count == 0
    assert spec.rejected_count == 1
    assert "prompt is required" in spec.errors[0].error


def test_bad_seed_rejected():
    text = (
        "subject,area,imageType,prompt,seed\n"
        "fysik,ljud,foto,x,not-a-number\n"
    )
    spec = parse_batch(text)
    assert spec.rejected_count == 1
    assert "seed must be an integer" in spec.errors[0].error


# ── 3. taxonomy validation ──────────────────────────────────────────────────


def test_unknown_subject_rejected():
    spec = parse_batch("subject,area,imageType,prompt\nbogus,ljud,foto,x\n")
    assert spec.rejected_count == 1
    assert "unknown subject" in spec.errors[0].error


def test_area_not_in_subject_rejected():
    # 'celler' is a biologi area, not a fysik area — subject-scoped check
    spec = parse_batch("subject,area,imageType,prompt\nfysik,celler,foto,x\n")
    assert spec.rejected_count == 1
    assert "unknown area" in spec.errors[0].error
    assert "celler" in spec.errors[0].error


def test_unknown_image_type_rejected():
    spec = parse_batch("subject,area,imageType,prompt\nfysik,ljud,hologram,x\n")
    assert spec.rejected_count == 1
    assert "unknown imageType" in spec.errors[0].error


def test_bild_requires_image_type():
    spec = parse_batch("subject,area,prompt\nfysik,ljud,x\n")
    assert spec.rejected_count == 1
    assert "imageType is required" in spec.errors[0].error


def test_film_row_validates_video_type():
    text = (
        "kind,subject,area,videoType,prompt\n"
        "film,biologi,celler,lektionsfilm,cell division\n"
        "film,biologi,celler,not-a-vid,bad\n"
    )
    spec = parse_batch(text)
    assert spec.valid_count == 1
    assert spec.rejected_count == 1
    assert spec.jobs[0]["kind"] == "film"
    assert spec.jobs[0]["video_type"] == "lektionsfilm"
    assert spec.jobs[0]["image_type"] is None
    assert "unknown videoType" in spec.errors[0].error


def test_film_requires_video_type():
    spec = parse_batch("kind,subject,area,prompt\nfilm,biologi,celler,x\n")
    assert spec.rejected_count == 1
    assert "videoType is required" in spec.errors[0].error


def test_invalid_kind_rejected():
    spec = parse_batch("kind,subject,area,imageType,prompt\nsong,fysik,ljud,foto,x\n")
    assert spec.rejected_count == 1
    assert "invalid kind" in spec.errors[0].error


# ── 4. client LoRA column is dropped (SFW guard) ─────────────────────────────


def test_client_lora_column_is_dropped():
    # a 'lora' column must never survive into a spec — the worker resolves it.
    text = (
        "subject,area,imageType,prompt,lora\n"
        "fysik,ljud,foto,x,off-allowlist-fake-v9\n"
    )
    spec = parse_batch(text)
    assert spec.valid_count == 1
    job = spec.jobs[0]
    assert "lora" not in job  # no top-level lora on the spec
    # and the stored provenance params blob did not capture the client lora
    assert "lora" not in job["params"]
    assert "loraName" not in job["params"]


def test_lora_name_alias_also_dropped():
    text = (
        "subject,area,imageType,prompt,loraName\n"
        "fysik,ljud,foto,x,sneaky\n"
    )
    spec = parse_batch(text)
    assert spec.valid_count == 1
    assert "loraname" not in {k.lower() for k in spec.jobs[0]["params"]}


# ── 5. malformed / edge inputs are graceful (no exception) ───────────────────


def test_empty_input_rejected_gracefully():
    for bad in ("", "   ", "\n\n"):
        spec = parse_batch(bad)
        assert spec.valid_count == 0
        assert spec.rejected_count == 1
        assert "empty batch" in spec.errors[0].error


def test_none_input_rejected_gracefully():
    spec = parse_batch(None)  # type: ignore[arg-type]
    assert spec.valid_count == 0
    assert "empty batch" in spec.errors[0].error


def test_too_many_rows_rejected_whole(monkeypatch):
    monkeypatch.setattr(batch, "MAX_ROWS", 2)
    text = "subject,area,imageType,prompt\n" + "".join(
        f"fysik,ljud,foto,row{i}\n" for i in range(5)
    )
    spec = parse_batch(text)
    assert spec.valid_count == 0
    assert spec.rejected_count == 1
    assert "too many rows" in spec.errors[0].error


def test_header_only_no_data_rows():
    spec = parse_batch("subject,area,imageType,prompt\n")
    assert spec.valid_count == 0
    assert spec.rejected_count == 1
    assert "no data rows" in spec.errors[0].error


# ── 6. header-less positional txt table ──────────────────────────────────────


def test_positional_whitespace_txt_parses():
    # no header; whitespace-delimited; '#' comments + blank lines skipped.
    text = (
        "# fill the fysik/ljud bucket\n"
        "fysik ljud illustration wave\n"
        "\n"
        "biologi celler foto plantcell\n"
    )
    spec = parse_batch(text)
    assert spec.valid_count == 2
    assert spec.rejected_count == 0
    assert spec.jobs[0]["subject"] == "fysik"
    assert spec.jobs[0]["prompt"] == "wave"


def test_positional_csv_without_header():
    # first cell is a subject slug (not a known column name) → positional mode
    text = "fysik,ljud,foto,a magnet field\nbiologi,celler,illustration,mitosis\n"
    spec = parse_batch(text)
    assert spec.valid_count == 2
    assert spec.jobs[1]["area"] == "celler"
    assert spec.jobs[1]["image_type"] == "illustration"


def test_positional_reports_line_numbers_including_comments():
    text = (
        "# header comment\n"  # line 1
        "fysik ljud foto good\n"  # line 2 valid
        "bogus ljud foto bad\n"  # line 3 invalid subject
    )
    spec = parse_batch(text)
    assert spec.valid_count == 1
    assert spec.rejected_count == 1
    assert spec.errors[0].line == 3


# ── 7. count expansion + caps ────────────────────────────────────────────────


def test_count_column_expands_in_spec():
    # count is parsed onto the spec; the fan-out (lb-y9c) repeats the enqueue.
    text = "subject,area,imageType,prompt,count\nfysik,ljud,foto,x,4\n"
    spec = parse_batch(text)
    assert spec.valid_count == 1
    assert spec.jobs[0]["count"] == 4


def test_count_zero_rejected():
    spec = parse_batch("subject,area,imageType,prompt,count\nfysik,ljud,foto,x,0\n")
    assert spec.rejected_count == 1
    assert "count must be >= 1" in spec.errors[0].error


def test_count_over_cap_rejected(monkeypatch):
    monkeypatch.setattr(batch, "MAX_ROW_COUNT", 3)
    spec = parse_batch("subject,area,imageType,prompt,count\nfysik,ljud,foto,x,9\n")
    assert spec.rejected_count == 1
    assert "exceeds per-row max" in spec.errors[0].error


def test_count_non_integer_rejected():
    spec = parse_batch("subject,area,imageType,prompt,count\nfysik,ljud,foto,x,many\n")
    assert spec.rejected_count == 1
    assert "count must be an integer" in spec.errors[0].error


def test_as_dict_shape():
    spec = parse_batch(
        "subject,area,imageType,prompt\nfysik,ljud,foto,x\nbogus,ljud,foto,y\n"
    )
    d = spec.as_dict()
    assert d["valid_count"] == 1
    assert d["rejected_count"] == 1
    assert d["errors"][0]["line"] == 3
    assert isinstance(d["jobs"], list)
