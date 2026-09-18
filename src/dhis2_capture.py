"""
AI-assisted form capture for DHIS2.

Reads a photographed paper health form with a vision model and submits the
confirmed result to DHIS2 through the tracker API.

Every design decision below was forced by a failure observed in practice. They
are documented where they occur rather than in a separate design note, because
the next person to touch this code needs the reason at the point of change:

1.  HTTP 200 from /api/tracker means the import job was ACCEPTED, not that a
    record exists. `submit_and_verify` polls the job report and reads the record
    back. Nothing here reports success on a status code alone.
2.  Attribute values are coerced explicitly. A boolean leaking into a name field
    produced a real record reading `Last name: "false"` on the public demo. The
    import succeeded; the data was wrong.
3.  An unparseable date raises. An earlier version substituted a hardcoded
    default, which puts a fabricated date into a patient record where nobody
    will ever notice.
4.  Extracted fields with no DHIS2 mapping are reported, never silently dropped.
5.  Metadata UIDs are discovered from the server, not hardcoded, and required or
    auto-generated attributes are detected before submission.

Licence: MIT
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__all__ = [
    "DHIS2Config", "ProgramMetadata", "PipelineSpec",
    "ExtractionResult", "SubmissionResult",
    "CaptureError", "ExtractionError", "SubmissionError", "ConfirmationDeclined",
    "parse_date", "coerce_value", "extract_json_object",
    "build_tracker_payload", "summarise_for_review", "interpret_tracker_response",
    "discover_program", "build_session", "submit_and_verify", "run_pipeline",
]

log = logging.getLogger("dhis2_capture")

# Model IDs change. Verify with GET https://api.anthropic.com/v1/models.
DEFAULT_MODEL = "claude-sonnet-4-5"

HTTP_TIMEOUT = 30
MAX_IMAGE_BYTES = 5 * 1024 * 1024
JOB_POLL_ATTEMPTS = 6
JOB_POLL_INTERVAL = 2.0

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class CaptureError(Exception):
    """Base class for errors this module raises deliberately."""


class ExtractionError(CaptureError):
    """The model did not return usable structured data."""


class SubmissionError(CaptureError):
    """DHIS2 rejected the record, or the outcome could not be established."""


class ConfirmationDeclined(CaptureError):
    """The operator reviewed the extraction and chose not to submit."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DHIS2Config:
    """Connection details for one DHIS2 instance."""

    base_url: str
    username: str
    password: str

    @classmethod
    def from_env(cls) -> "DHIS2Config":
        try:
            return cls(
                base_url=os.environ["DHIS2_BASE_URL"].rstrip("/"),
                username=os.environ["DHIS2_USERNAME"],
                password=os.environ["DHIS2_PASSWORD"],
            )
        except KeyError as exc:
            raise CaptureError(f"missing environment variable: {exc.args[0]}") from exc

    @classmethod
    def demo(cls) -> "DHIS2Config":
        """The public DHIS2 Sierra Leone demo. Synthetic data only.

        A pinned release is used rather than /dev. The dev instance tracks a
        moving SNAPSHOT build and resets nightly, which makes failures hard to
        attribute. Retired hosts such as play.dhis2.org/40 return an nginx 404
        that is not JSON, so the failure surfaces as a parse error.
        """
        return cls(
            base_url="https://play.im.dhis2.org/stable-2-40-12",
            username="admin",
            password="district",
        )


@dataclass(frozen=True)
class AttributeInfo:
    uid: str
    name: str
    value_type: str
    mandatory: bool
    generated: bool


@dataclass(frozen=True)
class ProgramMetadata:
    """What a programme on a specific server actually accepts.

    Built by `discover_program`. Never hardcode this: UIDs are specific to one
    database, and DHIS2 rejects unknown UIDs with messages that do not name the
    offending field.
    """

    program: str
    program_name: str
    tracked_entity_type: str
    org_unit: str
    org_unit_name: str
    attributes: tuple[AttributeInfo, ...]

    def find(self, *needles: str) -> str | None:
        """First attribute whose name contains any of `needles` (case-insensitive)."""
        for attr in self.attributes:
            lowered = attr.name.lower()
            if any(n.lower() in lowered for n in needles):
                return attr.uid
        return None

    @property
    def required_uids(self) -> tuple[str, ...]:
        """Mandatory attributes that the server will not generate for you."""
        return tuple(a.uid for a in self.attributes if a.mandatory and not a.generated)

    def describe(self) -> str:
        lines = [f"{self.program_name} ({self.program}) at "
                 f"{self.org_unit_name} ({self.org_unit})"]
        for a in self.attributes:
            flags = []
            if a.mandatory:
                flags.append("REQUIRED")
            if a.generated:
                flags.append("auto-generated")
            lines.append(f"  {a.uid}  {a.name:<30} {a.value_type:<12} "
                         f"{' '.join(flags)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class PipelineSpec:
    """One form type: what to extract and where each field belongs in DHIS2."""

    name: str
    input_type: str                       # "photo" or "audio"
    fields: Sequence[str]
    attribute_map: Mapping[str, str] = field(default_factory=dict)
    data_element_map: Mapping[str, str] = field(default_factory=dict)
    program_stage: str | None = None
    date_fields: Sequence[str] = ("visit_date", "date", "event_date")
    audio_language: str = "en"

    def __post_init__(self) -> None:
        if self.input_type not in {"photo", "audio"}:
            raise ValueError(f"input_type must be photo or audio, got {self.input_type!r}")
        if self.data_element_map and not self.program_stage:
            raise ValueError(f"{self.name!r} maps data elements but has no program_stage")
        clash = set(self.attribute_map) & set(self.data_element_map)
        if clash:
            raise ValueError(f"{self.name!r}: fields mapped twice: {sorted(clash)}")

    def unmapped_fields(self, extracted: Mapping[str, Any]) -> list[str]:
        known = set(self.attribute_map) | set(self.data_element_map) | set(self.date_fields)
        return sorted(k for k, v in extracted.items()
                      if v not in (None, "", []) and k not in known)


@dataclass
class ExtractionResult:
    pipeline: str
    data: dict[str, Any]
    missing_fields: list[str]
    unmapped_fields: list[str]
    source_file: str
    transcript: str | None = None


@dataclass
class SubmissionResult:
    status: str
    created: int
    updated: int
    ignored: int
    job_id: str | None
    messages: list[str]
    verified_records: list[dict[str, Any]]
    raw: dict[str, Any]

    @property
    def confirmed(self) -> bool:
        """True only when a record was read back out of DHIS2.

        Deliberately stricter than the import status. A job can report OK and
        still leave nothing you can query.
        """
        return bool(self.verified_records)


# --------------------------------------------------------------------------- #
# Value handling
# --------------------------------------------------------------------------- #

_DATE_FORMATS = (
    "%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
)


def parse_date(value: Any, *, today: date | None = None) -> str:
    """Normalise a date to the ISO form DHIS2 requires, or raise.

    %m/%d/%Y is deliberately absent. "03/04/2026" is ambiguous and silently
    choosing an interpretation is the class of error this module exists to
    prevent. Day-first is assumed; make it explicit in your fork if your forms
    are US-formatted.
    """
    if value is None or not str(value).strip():
        raise ValueError("date is empty — cannot submit a record without a date")

    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if parsed > (today or date.today()):
            raise ValueError(f"date {parsed.isoformat()} is in the future")
        return parsed.isoformat()

    raise ValueError(f"could not interpret date {value!r}; expected one of: "
                     + ", ".join(_DATE_FORMATS))


def coerce_value(value: Any, *, field_name: str = "") -> str:
    """Convert an extracted value to the string DHIS2 expects, or raise.

    Booleans are rejected outright. A boolean reaching a name field produced a
    real record on the public demo reading `Last name: "false"` — the import
    succeeded and the data was wrong, which is the worst combination available.
    If a genuine boolean belongs in a field, map it to an explicit code first.
    """
    where = f" for field {field_name!r}" if field_name else ""

    if isinstance(value, bool):
        raise ValueError(
            f"refusing to submit boolean {value!r}{where}; map it to an explicit "
            f"value such as 'true'/'false' or a DHIS2 option code first"
        )
    if value is None:
        raise ValueError(f"refusing to submit None{where}")
    if isinstance(value, (dict, list)):
        raise ValueError(f"refusing to submit {type(value).__name__}{where}")
    if isinstance(value, float) and value != value:          # NaN
        raise ValueError(f"refusing to submit NaN{where}")

    text = str(value).strip()
    if not text:
        raise ValueError(f"refusing to submit an empty value{where}")
    return text


def extract_json_object(raw: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Handles bare JSON, fenced blocks and prose around an object. Raises with the
    offending text rather than a bare JSONDecodeError, so the operator can see
    what the model actually said — a refusal, for instance.
    """
    if not raw or not raw.strip():
        raise ExtractionError("model returned an empty response")

    candidates = [raw.strip()]
    first, last = raw.find("{"), raw.rfind("}")
    if first != -1 and last > first:
        candidates.append(raw[first:last + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ExtractionError("model response was not a JSON object. Raw response:\n"
                          + raw[:2000])


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #

def build_tracker_payload(
    spec: PipelineSpec,
    meta: ProgramMetadata,
    data: Mapping[str, Any],
    *,
    occurred_at: str,
) -> dict[str, Any]:
    """Assemble the tracker import body.

    Attributes carry identity; data elements carry observations. Putting an
    observation in an attribute is a modelling error that surfaces at analysis
    time, long after it is cheap to fix.
    """
    attributes = [
        {"attribute": uid, "value": coerce_value(data[name], field_name=name)}
        for name, uid in spec.attribute_map.items()
        if data.get(name) not in (None, "", [])
    ]

    supplied = {a["attribute"] for a in attributes}
    missing_required = [uid for uid in meta.required_uids if uid not in supplied]
    if missing_required:
        names = {a.uid: a.name for a in meta.attributes}
        raise SubmissionError(
            "required attributes missing from the payload: "
            + ", ".join(f"{names.get(u, u)} ({u})" for u in missing_required)
        )

    enrollment: dict[str, Any] = {
        "orgUnit": meta.org_unit,
        "program": meta.program,
        "enrolledAt": occurred_at,
        "occurredAt": occurred_at,
        "status": "ACTIVE",
    }

    data_values = [
        {"dataElement": uid, "value": coerce_value(data[name], field_name=name)}
        for name, uid in spec.data_element_map.items()
        if data.get(name) not in (None, "", [])
    ]
    if data_values:
        enrollment["events"] = [{
            "programStage": spec.program_stage,
            "program": meta.program,
            "orgUnit": meta.org_unit,
            "occurredAt": occurred_at,
            "status": "COMPLETED",
            "dataValues": data_values,
        }]

    return {"trackedEntities": [{
        "trackedEntityType": meta.tracked_entity_type,
        "orgUnit": meta.org_unit,
        "attributes": attributes,
        "enrollments": [enrollment],
    }]}


def summarise_for_review(result: ExtractionResult) -> str:
    lines = [f"Extracted from {result.source_file} ({result.pipeline}):", ""]
    for name, value in result.data.items():
        lines.append(f"  {name:<24} {value if value not in (None, '', []) else '— not found —'}")
    if result.missing_fields:
        lines += ["", f"  MISSING: {', '.join(result.missing_fields)}"]
    if result.unmapped_fields:
        lines += ["", "  WILL NOT BE SAVED (no DHIS2 mapping): "
                      + ", ".join(result.unmapped_fields)]
    return "\n".join(lines)


def interpret_tracker_response(body: Mapping[str, Any]) -> tuple[str, dict, str | None, list[str]]:
    """Read a tracker response into (status, stats, job_id, messages).

    DHIS2 answers in two shapes depending on version and whether the import ran
    synchronously: an async job envelope ({"response": {"id": ...}}) or a
    synchronous import report carrying status and stats. Handle both; indexing
    blindly into one crashes on the other.
    """
    inner = body.get("response")

    if isinstance(inner, Mapping) and "id" in inner and "stats" not in inner:
        return (str(body.get("status", "PENDING")).upper(), {},
                str(inner["id"]), ["import queued; poll the job report"])

    report = inner if isinstance(inner, Mapping) else body
    stats = dict(report.get("stats") or {})
    status = str(report.get("status") or body.get("status") or "UNKNOWN").upper()

    messages: list[str] = []
    validation = report.get("validationReport") or {}
    for key in ("errorReports", "warningReports"):
        for item in validation.get(key) or []:
            code = item.get("errorCode", "")
            messages.append(f"{key[:-7]} {code}: {item.get('message', item)}".strip())

    return status, stats, None, messages


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def build_session(config: DHIS2Config) -> requests.Session:
    session = requests.Session()
    session.auth = (config.username, config.password)
    session.headers.update({"Content-Type": "application/json",
                            "Accept": "application/json"})
    retry = Retry(total=3, backoff_factor=1.0,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET", "POST"}),
                  raise_on_status=False)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def _json_or_raise(response: requests.Response, what: str) -> Any:
    """Parse a response body, with a clear message when it is not JSON.

    A retired DHIS2 host returns an nginx 404 HTML page. Parsing that blind
    produces "Expecting value: line 1 column 1", which sends people looking for
    a payload bug that does not exist.
    """
    try:
        return response.json()
    except ValueError as exc:
        snippet = response.text[:200].replace("\n", " ")
        raise SubmissionError(
            f"{what}: expected JSON, got HTTP {response.status_code} "
            f"({response.headers.get('Content-Type', 'unknown type')}). "
            f"Is the server URL correct? Body starts: {snippet}"
        ) from exc


def discover_program(
    config: DHIS2Config,
    *,
    program_uid: str,
    org_unit_name: str,
    session: requests.Session | None = None,
) -> ProgramMetadata:
    """Ask the server what this programme accepts, instead of assuming."""
    owned = session is None
    session = session or build_session(config)
    try:
        r = session.get(f"{config.base_url}/api/organisationUnits",
                        params={"filter": f"name:eq:{org_unit_name}",
                                "fields": "id,name"},
                        timeout=HTTP_TIMEOUT)
        units = _json_or_raise(r, "organisation unit lookup").get("organisationUnits") or []
        if not units:
            raise SubmissionError(f"no organisation unit named {org_unit_name!r}")

        r = session.get(
            f"{config.base_url}/api/programs/{program_uid}",
            params={"fields": "id,name,trackedEntityType[id],"
                              "programTrackedEntityAttributes[mandatory,"
                              "trackedEntityAttribute[id,name,valueType,generated]]"},
            timeout=HTTP_TIMEOUT)
        if r.status_code == 404:
            raise SubmissionError(f"programme {program_uid} not found on this server")
        prog = _json_or_raise(r, "programme lookup")
    finally:
        if owned:
            session.close()

    attrs = tuple(
        AttributeInfo(
            uid=p["trackedEntityAttribute"]["id"],
            name=p["trackedEntityAttribute"]["name"],
            value_type=p["trackedEntityAttribute"].get("valueType", ""),
            mandatory=bool(p.get("mandatory")),
            generated=bool(p["trackedEntityAttribute"].get("generated")),
        )
        for p in prog.get("programTrackedEntityAttributes", [])
    )

    return ProgramMetadata(
        program=prog["id"],
        program_name=prog.get("name", program_uid),
        tracked_entity_type=prog["trackedEntityType"]["id"],
        org_unit=units[0]["id"],
        org_unit_name=units[0]["name"],
        attributes=attrs,
    )


def _read_back(
    session: requests.Session,
    config: DHIS2Config,
    meta: ProgramMetadata,
    *,
    filter_uid: str,
    filter_value: str,
) -> list[dict[str, Any]]:
    """Query the record back. This is the only proof the record exists.

    Filter on a value you supplied rather than on recency. The public demo is
    writable by anyone, so "the most recent record" is not necessarily yours.
    """
    r = session.get(
        f"{config.base_url}/api/tracker/trackedEntities",
        params={
            "orgUnit": meta.org_unit,
            "program": meta.program,
            "ouMode": "SELECTED",
            "filter": f"{filter_uid}:eq:{filter_value}",
            "order": "createdAt:desc",
            "pageSize": 5,
            "fields": "trackedEntity,createdAt,attributes[attribute,displayName,value]",
        },
        timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        log.warning("read-back query returned HTTP %s", r.status_code)
        return []
    body = _json_or_raise(r, "read-back query")
    # 2.40 returns "instances"; later versions return "trackedEntities".
    return body.get("instances") or body.get("trackedEntities") or []


def submit_and_verify(
    payload: Mapping[str, Any],
    config: DHIS2Config,
    meta: ProgramMetadata,
    *,
    verify_uid: str,
    verify_value: str,
    session: requests.Session | None = None,
) -> SubmissionResult:
    """Submit, follow the job to completion, then read the record back.

    The read-back is the point. HTTP 200 means DHIS2 accepted the import job;
    a job can still fail validation and create nothing, and code that reports
    success on the status code is lying to whoever photographed the form.
    """
    owned = session is None
    session = session or build_session(config)
    try:
        response = session.post(f"{config.base_url}/api/tracker",
                                json=payload, timeout=HTTP_TIMEOUT * 2)
        body = _json_or_raise(response, "tracker import")
        if response.status_code >= 400:
            status, stats, job_id, messages = interpret_tracker_response(body)
            raise SubmissionError(
                f"HTTP {response.status_code} from /api/tracker. "
                + ("; ".join(messages) or json.dumps(body)[:600])
            )

        status, stats, job_id, messages = interpret_tracker_response(body)

        if job_id:
            for _ in range(JOB_POLL_ATTEMPTS):
                time.sleep(JOB_POLL_INTERVAL)
                r = session.get(f"{config.base_url}/api/tracker/jobs/{job_id}/report",
                                timeout=HTTP_TIMEOUT)
                if r.status_code >= 400:
                    continue
                report = _json_or_raise(r, "job report")
                status, stats, _, messages = interpret_tracker_response(report)
                if status not in {"PENDING", "RUNNING", "UNKNOWN"}:
                    body = report
                    break

        records = _read_back(session, config, meta,
                             filter_uid=verify_uid, filter_value=verify_value)
    finally:
        if owned:
            session.close()

    result = SubmissionResult(
        status=status,
        created=int(stats.get("created", 0)),
        updated=int(stats.get("updated", 0)),
        ignored=int(stats.get("ignored", 0)),
        job_id=job_id,
        messages=messages,
        verified_records=records,
        raw=dict(body),
    )

    if not result.confirmed:
        raise SubmissionError(
            f"import reported status={result.status} created={result.created} "
            f"ignored={result.ignored}, but no matching record could be read "
            f"back from DHIS2. " + ("; ".join(messages) or "no detail returned")
        )
    return result


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

def _encode_image(path: str | Path) -> tuple[str, str]:
    p = Path(path)
    if not p.is_file():
        raise ExtractionError(f"file not found: {p}")
    size = p.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ExtractionError(f"{p.name} is {size / 1e6:.1f} MB; the API limit is "
                              f"{MAX_IMAGE_BYTES / 1e6:.0f} MB. Resize first.")
    media_type, _ = mimetypes.guess_type(p.name)
    if media_type not in SUPPORTED_IMAGE_TYPES:
        raise ExtractionError(f"unsupported image type {media_type!r} for {p.name}")
    return base64.standard_b64encode(p.read_bytes()).decode("ascii"), media_type


def build_prompt(fields: Sequence[str]) -> str:
    schema = ",\n".join(f'  "{name}": ""' for name in fields)
    return (
        "You are transcribing a health facility form for data entry.\n\n"
        f"Return ONLY a JSON object with exactly these keys:\n{{\n{schema}\n}}\n\n"
        "Rules:\n"
        "- Transcribe only what is legibly written or ticked. Do not infer, "
        "complete, or correct values.\n"
        "- For checkboxes, return the label of the ticked option as a string.\n"
        "- Never return true or false. Every value is a string.\n"
        "- If a field is absent, illegible, or you are unsure, return an empty "
        "string. An empty string is always better than a guess.\n"
        "- Preserve dates exactly as written on the form.\n"
        "- No commentary, no explanation, no markdown fences."
    )


def extract_from_photo(image_path, spec, *, client, model=DEFAULT_MODEL) -> dict[str, Any]:
    image_b64, media_type = _encode_image(image_path)
    message = client.messages.create(
        model=model, max_tokens=1024,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": build_prompt(spec.fields)},
        ]}])
    return extract_json_object(message.content[0].text)


def extract_from_audio(audio_path, spec, *, client, transcriber,
                       model=DEFAULT_MODEL) -> tuple[dict[str, Any], str]:
    transcript = transcriber(audio_path, spec.audio_language)
    if not transcript.strip():
        raise ExtractionError("transcription returned no text")
    message = client.messages.create(
        model=model, max_tokens=1024,
        messages=[{"role": "user", "content":
                   f"A health worker dictated this note:\n\n{transcript}\n\n"
                   + build_prompt(spec.fields)}])
    return extract_json_object(message.content[0].text), transcript


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _default_confirm(summary: str) -> bool:
    print(summary)
    return input("\nSubmit this record to DHIS2? [y/N]: ").strip().lower() in {"y", "yes"}


def run_pipeline(
    spec: PipelineSpec,
    meta: ProgramMetadata,
    input_file: str | Path,
    config: DHIS2Config,
    *,
    anthropic_client: Any,
    verify_field: str,
    transcriber: Callable[[str | Path, str], str] | None = None,
    model: str = DEFAULT_MODEL,
    confirm: Callable[[str], bool] = _default_confirm,
    session: requests.Session | None = None,
) -> tuple[ExtractionResult, SubmissionResult]:
    """Extract, review, submit, verify.

    `confirm` is a deliberate seam: a notebook passes an input() prompt, a chat
    bot passes a callback awaiting a button press, a test passes a lambda. What
    none of them may do is skip it.

    `verify_field` names the extracted field used to read the record back. It
    must be in `spec.attribute_map`.
    """
    log.info("running %s on %s", spec.name, input_file)

    transcript = None
    if spec.input_type == "photo":
        raw_data = extract_from_photo(input_file, spec,
                                      client=anthropic_client, model=model)
    else:
        if transcriber is None:
            raise CaptureError(f"{spec.name!r} takes audio but no transcriber was given")
        raw_data, transcript = extract_from_audio(
            input_file, spec, client=anthropic_client,
            transcriber=transcriber, model=model)

    data = {name: raw_data.get(name, "") for name in spec.fields}

    result = ExtractionResult(
        pipeline=spec.name,
        data=data,
        missing_fields=[f for f in spec.fields if data.get(f) in (None, "", [])],
        unmapped_fields=spec.unmapped_fields(data),
        source_file=str(input_file),
        transcript=transcript,
    )
    if result.unmapped_fields:
        log.warning("extracted but NOT mapped to DHIS2, will not be saved: %s",
                    ", ".join(result.unmapped_fields))

    occurred_at = parse_date(next((data[f] for f in spec.date_fields if data.get(f)), None))

    if verify_field not in spec.attribute_map:
        raise CaptureError(f"verify_field {verify_field!r} is not in attribute_map")
    if not data.get(verify_field):
        raise CaptureError(f"verify_field {verify_field!r} was not extracted; "
                           f"cannot confirm the record afterwards")

    if not confirm(summarise_for_review(result)):
        raise ConfirmationDeclined("operator declined submission")

    payload = build_tracker_payload(spec, meta, data, occurred_at=occurred_at)
    submission = submit_and_verify(
        payload, config, meta,
        verify_uid=spec.attribute_map[verify_field],
        verify_value=coerce_value(data[verify_field], field_name=verify_field),
        session=session)

    log.info("confirmed in DHIS2: %s record(s)", len(submission.verified_records))
    return result, submission
