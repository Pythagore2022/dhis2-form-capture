# Failure modes

Every item below was hit during development of this pipeline. None of them
produced an error message that made the cause obvious, and several produced no
error at all — the import succeeded and the data was wrong.

They are documented here because the next person building this will meet the
same ones, and because each corresponds to a test in
[`tests/test_dhis2_capture.py`](../tests/test_dhis2_capture.py).

---

## 1. HTTP 200 does not mean the record was saved

**Symptom.** The pipeline reports success. Nothing exists in DHIS2.

`POST /api/tracker` returns 200 and a job id when DHIS2 **accepts the import
job**:

```
DHIS2 response: 200
Tracker job submitted — Job ID: EOiNL4dnV8J
```

The job then runs asynchronously and can fail validation, creating nothing. An
implementation that returns `{"status": "success"}` at this point is reporting
an outcome it has not observed.

**Fix.** Follow the job report, then query the record back:

```
GET /api/tracker/jobs/{jobId}/report
GET /api/tracker/trackedEntities?...&filter={ATTR}:eq:{VALUE}
```

`submit_and_verify()` does both and raises `SubmissionError` if no record comes
back. `SubmissionResult.confirmed` is true only after a successful read-back.

**Second trap in the same place.** DHIS2 answers this endpoint in two shapes
depending on version and whether the import ran synchronously:

```jsonc
// async job envelope
{"status": "OK", "response": {"id": "EOiNL4dnV8J"}}

// synchronous import report
{"status": "OK", "stats": {"created": 1, "ignored": 0}}
```

Indexing blindly into one crashes on the other.
`interpret_tracker_response()` handles both.

**Third trap.** The read-back must filter on a value you supplied, not on
recency. The public demo is writable by anyone; during development the most
recently created record at Ngelehun CHC turned out to belong to a stranger.
`verify_setup.py` generates a unique marker per run.

---

## 2. Booleans in string fields

**Symptom.** A record in the public demo reading:

```
First name : Chommy
Last name  : false
```

A boolean reached a name field. The import succeeded and the data is wrong —
the worst combination available, because nothing flags it.

**Fix.** `coerce_value()` rejects booleans, `None`, NaN and structured types
outright, naming the field in the error. The extraction prompt additionally
instructs the model never to return `true` or `false`, so every extracted value
is a string.

---

## 3. Fabricated dates

**Symptom.** Patient records carrying `2024-01-01`.

DHIS2 accepts `yyyy-MM-dd` only. Forms are written in every format humans use.
An early version substituted a hardcoded default when parsing failed:

```python
print(f"Warning: could not parse date '{date_str}', using default.")
return "2024-01-01"      # never do this
```

A warning printed to a console nobody reads, and a fabricated date in a health
record where nobody will ever notice it.

**Fix.** `parse_date()` raises. A failed import is visible; a wrong date is not.

**Related.** `%m/%d/%Y` is deliberately absent from the accepted formats.
`03/04/2026` is genuinely ambiguous, and silently choosing an interpretation is
the same class of error. Day-first is assumed; change it explicitly for
US-formatted forms and document the choice.

---

## 4. Fields extracted, then silently discarded

**Symptom.** A health worker photographs a malaria form, sees "submitted", and
the test result was never stored anywhere.

If a field has no entry in the attribute map it is dropped. Without an explicit
check, the pipeline still reports success.

**Fix.** `PipelineSpec.unmapped_fields()` returns every extracted field carrying
a value that has nowhere to go. These are shown in the confirmation screen under
`WILL NOT BE SAVED` and logged at warning level.

Expect the list to be long on the public demo: the Sierra Leone database has no
malaria tracker programme, so most clinical fields have no home in Child
Programme. That is correct behaviour, not a bug.

---

## 5. Copied UIDs

**Symptom.** `409 Conflict`, with a message that does not say which field is
wrong.

Every UID belongs to one database. Copying an organisation unit or tracked
entity attribute UID from another notebook is the most common cause of silent
failure in DHIS2 integrations.

**Fix.** `discover_program()` queries the server for what exists, and reads two
flags that matter:

- `mandatory` — you must supply it
- `generated` — the server creates it, and will reject a value you send

Child Programme's *Unique ID* attribute is both. Supplying it gets the record
rejected; omitting it because it is mandatory also fails. Only the combination
of flags tells you the right answer. `ProgramMetadata.required_uids` excludes
generated attributes.

---

## 6. Retired server URLs

**Symptom.** `Expecting value: line 1 column 1 (char 0)`, which looks like a
payload bug and is not.

`play.dhis2.org/40`, `play.dhis2.org/2.40.4` and several `stable-2-4x`
hostnames are retired. They serve an nginx 404 **HTML** page. Parsing that as
JSON produces a message that sends people hunting for a problem that does not
exist.

**Fix.** `_json_or_raise()` checks the response and reports the likely cause:
the server URL is wrong.

| Host | State |
|---|---|
| `play.im.dhis2.org/stable-2-40-12` | Working — pinned release, the default here |
| `play.im.dhis2.org/dev` | Working — moving SNAPSHOT build, resets nightly |
| `play.dhis2.org/40` | Retired, nginx 404 |

A pinned release is preferred over `/dev`: a moving build makes failures hard to
attribute to your own code.

---

## 7. Configuration key mismatches

**Symptom.** `KeyError: 'program'`, raised *after* the vision model call has
already been paid for.

A pipeline definition using `dhis2_program` read by code expecting `program`.

**Fix.** A single typed `PipelineSpec` with validation in `__post_init__`,
which also rejects a field mapped to both an attribute and a data element, and
data elements declared without a programme stage. Configuration errors surface
before any expensive work happens.

---

## 8. Attributes used for observations

**Symptom.** Nothing fails. The data is in the wrong place, and you find out at
analysis time.

Identity fields (name, sex, date of birth) are tracked entity attributes on the
enrollment. Clinical observations (test result, temperature, treatment) are data
elements recorded against a programme stage event.

**Fix.** `PipelineSpec` keeps `attribute_map` and `data_element_map` separate
and refuses a field appearing in both.

---

## Running the regression suite

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src pytest tests/ -q
```

72 tests. Each failure mode above has at least one test named after it —
`test_http_200_without_a_readable_record_is_a_failure`,
`test_booleans_are_refused`,
`test_no_hardcoded_default_ever_returned`, and so on.
