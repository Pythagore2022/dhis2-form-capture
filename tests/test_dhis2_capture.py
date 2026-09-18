"""Tests for dhis2_capture.

Each test corresponds to a failure observed in practice. Network calls are
mocked; nothing here contacts DHIS2 or a model API.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from dhis2_capture import (
    AttributeInfo,
    CaptureError,
    ConfirmationDeclined,
    DHIS2Config,
    ExtractionError,
    ExtractionResult,
    PipelineSpec,
    ProgramMetadata,
    SubmissionError,
    build_prompt,
    build_tracker_payload,
    coerce_value,
    extract_json_object,
    interpret_tracker_response,
    parse_date,
    run_pipeline,
    submit_and_verify,
    summarise_for_review,
)

TODAY = date(2026, 9, 15)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def meta() -> ProgramMetadata:
    return ProgramMetadata(
        program="IpHINAT79UW",
        program_name="Child Programme",
        tracked_entity_type="nEenWmSyUEp",
        org_unit="DiszpKrYNg8",
        org_unit_name="Ngelehun CHC",
        attributes=(
            AttributeInfo("w75KJ2mc4zz", "First name", "TEXT", True, False),
            AttributeInfo("zDhUuAYrxNC", "Last name", "TEXT", True, False),
            AttributeInfo("cejWyOfXge6", "Gender", "TEXT", False, False),
            AttributeInfo("lZGmxYbs97q", "Unique ID", "TEXT", True, True),
        ),
    )


@pytest.fixture
def spec() -> PipelineSpec:
    return PipelineSpec(
        name="malaria_case_report",
        input_type="photo",
        fields=["first_name", "last_name", "sex", "age", "facility",
                "visit_date", "test_result", "treatment_given"],
        attribute_map={"first_name": "w75KJ2mc4zz",
                       "last_name": "zDhUuAYrxNC",
                       "sex": "cejWyOfXge6"},
        data_element_map={"test_result": "DE0000001",
                          "treatment_given": "DE0000002"},
        program_stage="STAGE00001",
    )


GOOD = {"first_name": "Fatmata", "last_name": "Kargbo", "sex": "Female",
        "age": "27", "facility": "Ngelehun CHC", "visit_date": "12 March 2024",
        "test_result": "Positive", "treatment_given": "ACT"}


class FakeAnthropic:
    def __init__(self, text: str):
        self.messages = SimpleNamespace(
            create=lambda **kw: SimpleNamespace(
                content=[SimpleNamespace(text=text)]))


class FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or str(body)
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Serves queued POST and GET responses and records what was sent."""

    def __init__(self, post=None, get=None):
        self._post = post
        self._get = get or {}
        self.posted: list[dict] = []
        self.got: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.posted.append({"url": url, "json": json})
        return self._post

    def get(self, url, params=None, timeout=None):
        self.got.append({"url": url, "params": params})
        for key, resp in self._get.items():
            if key in url:
                return resp
        return FakeResponse(404, None, "not found")

    def close(self):
        pass


def _readback(n=1):
    return FakeResponse(200, {"instances": [
        {"trackedEntity": "abc123", "createdAt": "2026-09-15T14:34:36",
         "attributes": [{"displayName": "Last name", "value": "Kargbo"}]}
    ] * n})


def _png(tmp_path):
    from PIL import Image
    p = tmp_path / "form.png"
    Image.new("RGB", (40, 40), "white").save(p)
    return p


# --------------------------------------------------------------------------- #
# coerce_value — the "Last name: false" bug
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [True, False])
def test_booleans_are_refused(bad):
    """A boolean reaching a name field produced a real record reading
    Last name: "false". The import succeeded; the data was wrong."""
    with pytest.raises(ValueError, match="boolean"):
        coerce_value(bad, field_name="last_name")


@pytest.mark.parametrize("bad", [None, "", "   ", {}, [], float("nan")])
def test_empty_and_structured_values_are_refused(bad):
    with pytest.raises(ValueError):
        coerce_value(bad, field_name="x")


@pytest.mark.parametrize("value,expected", [
    ("Kargbo", "Kargbo"), ("  Kargbo  ", "Kargbo"), (27, "27"), (38.6, "38.6"),
])
def test_ordinary_values_pass_through(value, expected):
    assert coerce_value(value) == expected


def test_error_names_the_field():
    with pytest.raises(ValueError, match="last_name"):
        coerce_value(True, field_name="last_name")


# --------------------------------------------------------------------------- #
# parse_date
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("2024-03-15", "2024-03-15"), ("15 March 2024", "2024-03-15"),
    ("15 Mar 2024", "2024-03-15"), ("March 15, 2024", "2024-03-15"),
    ("15/03/2024", "2024-03-15"), ("15-03-2024", "2024-03-15"),
    ("15.03.2024", "2024-03-15"), ("  2024-03-15  ", "2024-03-15"),
])
def test_common_date_formats(raw, expected):
    assert parse_date(raw, today=TODAY) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", "32/13/2024"])
def test_unparseable_dates_raise_rather_than_defaulting(raw):
    with pytest.raises(ValueError):
        parse_date(raw, today=TODAY)


def test_future_dates_rejected():
    with pytest.raises(ValueError, match="future"):
        parse_date("2030-01-01", today=TODAY)


def test_no_hardcoded_default_ever_returned():
    """Regression: an earlier version returned 2024-01-01 on failure."""
    for bad in ("", "garbage", None, "99/99/9999"):
        with pytest.raises(ValueError):
            parse_date(bad, today=TODAY)


# --------------------------------------------------------------------------- #
# extract_json_object
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('```\n{"a": 1}\n```', {"a": 1}),
    ('Here you go:\n{"a": 1}\nHope that helps.', {"a": 1}),
    ('{"outer": {"inner": "v"}}', {"outer": {"inner": "v"}}),
])
def test_json_extraction(raw, expected):
    assert extract_json_object(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "no json here", "[1, 2, 3]"])
def test_unusable_model_output_raises(raw):
    with pytest.raises(ExtractionError):
        extract_json_object(raw)


def test_model_refusal_is_surfaced_verbatim():
    with pytest.raises(ExtractionError, match="cannot read this form"):
        extract_json_object("I cannot read this form.")


def test_prompt_forbids_booleans():
    assert "true or false" in build_prompt(["a"]).lower()


# --------------------------------------------------------------------------- #
# Spec validation
# --------------------------------------------------------------------------- #

def test_field_mapped_twice_is_rejected():
    with pytest.raises(ValueError, match="mapped twice"):
        PipelineSpec(name="b", input_type="photo", fields=["x"],
                     program_stage="S",
                     attribute_map={"x": "A"}, data_element_map={"x": "B"})


def test_data_elements_without_stage_rejected():
    with pytest.raises(ValueError, match="program_stage"):
        PipelineSpec(name="b", input_type="photo", fields=["x"],
                     data_element_map={"x": "B"})


def test_unknown_input_type_rejected():
    with pytest.raises(ValueError, match="input_type"):
        PipelineSpec(name="b", input_type="fax", fields=["x"])


def test_unmapped_fields_detected(spec):
    assert spec.unmapped_fields(GOOD) == ["age", "facility"]


def test_empty_values_are_not_reported_as_unmapped(spec):
    assert spec.unmapped_fields({"age": "", "facility": None}) == []


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #

def test_find_attribute_by_name(meta):
    assert meta.find("first name") == "w75KJ2mc4zz"
    assert meta.find("gender", "sex") == "cejWyOfXge6"
    assert meta.find("nonexistent") is None


def test_generated_attributes_are_not_required_of_the_caller(meta):
    """Unique ID is mandatory but server-generated: we must not be asked for it."""
    assert "lZGmxYbs97q" not in meta.required_uids
    assert set(meta.required_uids) == {"w75KJ2mc4zz", "zDhUuAYrxNC"}


def test_describe_flags_required_and_generated(meta):
    text = meta.describe()
    assert "REQUIRED" in text and "auto-generated" in text


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #

def test_attributes_and_data_elements_are_separated(spec, meta):
    payload = build_tracker_payload(spec, meta, GOOD, occurred_at="2024-03-12")
    te = payload["trackedEntities"][0]
    assert te["trackedEntityType"] == "nEenWmSyUEp"
    assert {a["attribute"]: a["value"] for a in te["attributes"]} == {
        "w75KJ2mc4zz": "Fatmata", "zDhUuAYrxNC": "Kargbo", "cejWyOfXge6": "Female"}
    event = te["enrollments"][0]["events"][0]
    assert {d["dataElement"]: d["value"] for d in event["dataValues"]} == {
        "DE0000001": "Positive", "DE0000002": "ACT"}


def test_payload_uses_discovered_program_not_a_global(spec, meta):
    payload = build_tracker_payload(spec, meta, GOOD, occurred_at="2024-03-12")
    assert payload["trackedEntities"][0]["enrollments"][0]["program"] == "IpHINAT79UW"


def test_missing_required_attribute_is_caught_before_submission(spec, meta):
    partial = dict(GOOD, last_name="")
    with pytest.raises(SubmissionError, match="Last name"):
        build_tracker_payload(spec, meta, partial, occurred_at="2024-03-12")


def test_boolean_in_payload_is_refused(spec, meta):
    with pytest.raises(ValueError, match="boolean"):
        build_tracker_payload(spec, meta, dict(GOOD, last_name=False),
                              occurred_at="2024-03-12")


def test_no_event_when_no_data_elements(meta):
    simple = PipelineSpec(name="s", input_type="photo",
                          fields=["first_name", "last_name"],
                          attribute_map={"first_name": "w75KJ2mc4zz",
                                         "last_name": "zDhUuAYrxNC"})
    payload = build_tracker_payload(simple, meta, GOOD, occurred_at="2024-03-12")
    assert "events" not in payload["trackedEntities"][0]["enrollments"][0]


# --------------------------------------------------------------------------- #
# Response interpretation
# --------------------------------------------------------------------------- #

def test_synchronous_import_report():
    status, stats, job, msgs = interpret_tracker_response(
        {"status": "OK", "stats": {"created": 1, "ignored": 0}})
    assert status == "OK" and stats["created"] == 1 and job is None


def test_async_job_envelope():
    status, stats, job, msgs = interpret_tracker_response(
        {"status": "OK", "response": {"id": "EOiNL4dnV8J"}})
    assert job == "EOiNL4dnV8J"


def test_nested_response_with_stats():
    status, stats, job, msgs = interpret_tracker_response(
        {"response": {"status": "OK", "stats": {"created": 2}}})
    assert stats["created"] == 2 and job is None


def test_validation_errors_are_surfaced():
    status, stats, job, msgs = interpret_tracker_response({
        "status": "ERROR", "stats": {"ignored": 1},
        "validationReport": {"errorReports": [
            {"message": "Org unit not found", "errorCode": "E1049"}]}})
    assert any("E1049" in m and "Org unit not found" in m for m in msgs)


def test_unexpected_shape_does_not_crash():
    status, _, _, _ = interpret_tracker_response({"surprise": True})
    assert status == "UNKNOWN"


# --------------------------------------------------------------------------- #
# submit_and_verify — the HTTP 200 problem
# --------------------------------------------------------------------------- #

def test_success_requires_reading_the_record_back(meta):
    session = FakeSession(
        post=FakeResponse(200, {"status": "OK", "stats": {"created": 1}}),
        get={"trackedEntities": _readback()})
    result = submit_and_verify({}, DHIS2Config.demo(), meta,
                               verify_uid="zDhUuAYrxNC", verify_value="Kargbo",
                               session=session)
    assert result.confirmed and result.created == 1


def test_http_200_without_a_readable_record_is_a_failure(meta):
    """The central lesson: 200 means the job was accepted, not that it saved."""
    session = FakeSession(
        post=FakeResponse(200, {"status": "OK", "stats": {"created": 1}}),
        get={"trackedEntities": FakeResponse(200, {"instances": []})})
    with pytest.raises(SubmissionError, match="no matching record"):
        submit_and_verify({}, DHIS2Config.demo(), meta,
                          verify_uid="zDhUuAYrxNC", verify_value="Kargbo",
                          session=session)


def test_http_error_surfaces_validation_detail(meta):
    session = FakeSession(post=FakeResponse(409, {
        "status": "ERROR",
        "validationReport": {"errorReports": [{"message": "Invalid attribute",
                                               "errorCode": "E1006"}]}}))
    with pytest.raises(SubmissionError, match="E1006"):
        submit_and_verify({}, DHIS2Config.demo(), meta,
                          verify_uid="zDhUuAYrxNC", verify_value="K",
                          session=session)


def test_non_json_response_explains_the_likely_cause(meta):
    """A retired host returns an nginx 404 page, not JSON."""
    session = FakeSession(post=FakeResponse(404, None, "<html>404 Not Found nginx</html>"))
    with pytest.raises(SubmissionError, match="server URL"):
        submit_and_verify({}, DHIS2Config.demo(), meta,
                          verify_uid="zDhUuAYrxNC", verify_value="K",
                          session=session)


def test_read_back_filters_on_a_supplied_value_not_recency(meta):
    """The public demo is writable by anyone; recency proves nothing."""
    session = FakeSession(
        post=FakeResponse(200, {"status": "OK", "stats": {"created": 1}}),
        get={"trackedEntities": _readback()})
    submit_and_verify({}, DHIS2Config.demo(), meta,
                      verify_uid="zDhUuAYrxNC", verify_value="Kargbo",
                      session=session)
    assert session.got[0]["params"]["filter"] == "zDhUuAYrxNC:eq:Kargbo"


def test_2_40_instances_key_is_understood(meta):
    session = FakeSession(
        post=FakeResponse(200, {"status": "OK", "stats": {"created": 1}}),
        get={"trackedEntities": FakeResponse(200, {"instances": [{"trackedEntity": "x"}]})})
    assert submit_and_verify({}, DHIS2Config.demo(), meta,
                             verify_uid="zDhUuAYrxNC", verify_value="K",
                             session=session).confirmed


def test_newer_trackedentities_key_is_understood(meta):
    session = FakeSession(
        post=FakeResponse(200, {"status": "OK", "stats": {"created": 1}}),
        get={"trackedEntities": FakeResponse(200,
                                             {"trackedEntities": [{"trackedEntity": "x"}]})})
    assert submit_and_verify({}, DHIS2Config.demo(), meta,
                             verify_uid="zDhUuAYrxNC", verify_value="K",
                             session=session).confirmed


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #

RESPONSE = '```json\n' + str(GOOD).replace("'", '"') + '\n```'


def test_happy_path(spec, meta, tmp_path):
    session = FakeSession(
        post=FakeResponse(200, {"status": "OK", "stats": {"created": 1}}),
        get={"trackedEntities": _readback()})
    extraction, submission = run_pipeline(
        spec, meta, _png(tmp_path), DHIS2Config.demo(),
        anthropic_client=FakeAnthropic(RESPONSE),
        verify_field="last_name", confirm=lambda s: True, session=session)

    assert submission.confirmed
    assert extraction.data["first_name"] == "Fatmata"
    enrollment = session.posted[0]["json"]["trackedEntities"][0]["enrollments"][0]
    assert enrollment["enrolledAt"] == "2024-03-12"   # "12 March 2024" normalised


def test_unmapped_fields_are_reported(spec, meta, tmp_path):
    session = FakeSession(
        post=FakeResponse(200, {"status": "OK", "stats": {"created": 1}}),
        get={"trackedEntities": _readback()})
    extraction, _ = run_pipeline(
        spec, meta, _png(tmp_path), DHIS2Config.demo(),
        anthropic_client=FakeAnthropic(RESPONSE),
        verify_field="last_name", confirm=lambda s: True, session=session)
    assert extraction.unmapped_fields == ["age", "facility"]


def test_decline_sends_nothing(spec, meta, tmp_path):
    session = FakeSession(post=FakeResponse(200, {}))
    with pytest.raises(ConfirmationDeclined):
        run_pipeline(spec, meta, _png(tmp_path), DHIS2Config.demo(),
                     anthropic_client=FakeAnthropic(RESPONSE),
                     verify_field="last_name", confirm=lambda s: False,
                     session=session)
    assert session.posted == []


def test_unreadable_date_blocks_submission(spec, meta, tmp_path):
    bad = RESPONSE.replace('"12 March 2024"', '""')
    session = FakeSession(post=FakeResponse(200, {}))
    with pytest.raises(ValueError):
        run_pipeline(spec, meta, _png(tmp_path), DHIS2Config.demo(),
                     anthropic_client=FakeAnthropic(bad),
                     verify_field="last_name", confirm=lambda s: True,
                     session=session)
    assert session.posted == []


def test_verify_field_must_be_mapped(spec, meta, tmp_path):
    with pytest.raises(CaptureError, match="verify_field"):
        run_pipeline(spec, meta, _png(tmp_path), DHIS2Config.demo(),
                     anthropic_client=FakeAnthropic(RESPONSE),
                     verify_field="facility", confirm=lambda s: True,
                     session=FakeSession(post=FakeResponse(200, {})))


def test_non_image_rejected(spec, meta, tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("not an image")
    with pytest.raises(ExtractionError, match="unsupported image type"):
        run_pipeline(spec, meta, txt, DHIS2Config.demo(),
                     anthropic_client=FakeAnthropic(RESPONSE),
                     verify_field="last_name", confirm=lambda s: True)


# --------------------------------------------------------------------------- #
# Review summary
# --------------------------------------------------------------------------- #

def test_summary_warns_about_unsaved_fields():
    text = summarise_for_review(ExtractionResult(
        pipeline="p", data={"first_name": "Fatmata", "facility": "CHC"},
        missing_fields=[], unmapped_fields=["facility"], source_file="f.png"))
    assert "WILL NOT BE SAVED" in text and "facility" in text


def test_summary_marks_missing_fields():
    text = summarise_for_review(ExtractionResult(
        pipeline="p", data={"first_name": ""}, missing_fields=["first_name"],
        unmapped_fields=[], source_file="f.png"))
    assert "MISSING" in text and "not found" in text
