#!/usr/bin/env python3
"""Pre-flight check.

Run this before first use, and again whenever you point the pipeline at a
different DHIS2 server. It verifies, in order:

  1. DHIS2 is reachable and the credentials work
  2. The programme and organisation unit exist, and which attributes are
     required or auto-generated
  3. The model API key is valid and the configured model answers
  4. A full round trip: submit a synthetic record, follow the import job, and
     read the record back

Nothing here uses real patient data.

    python src/verify_setup.py                # public demo server
    python src/verify_setup.py --env          # DHIS2_* environment variables
    python src/verify_setup.py --skip-write   # read-only checks
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

import requests

from dhis2_capture import (
    DEFAULT_MODEL,
    CaptureError,
    DHIS2Config,
    PipelineSpec,
    build_session,
    build_tracker_payload,
    discover_program,
    submit_and_verify,
)

OK, NO, HM = "  [pass]", "  [FAIL]", "  [warn]"

PROGRAM_UID = "IpHINAT79UW"       # Child Programme on the Sierra Leone demo
ORG_UNIT_NAME = "Ngelehun CHC"


def check_connection(config: DHIS2Config) -> bool:
    print("\n1. DHIS2 connection")
    session = build_session(config)
    try:
        r = session.get(f"{config.base_url}/api/system/info", timeout=30)
    except requests.RequestException as exc:
        print(f"{NO} cannot reach {config.base_url}: {exc}")
        return False

    if r.status_code == 401:
        print(f"{NO} authentication rejected — check username and password")
        return False
    if r.status_code >= 400:
        print(f"{NO} HTTP {r.status_code} from /api/system/info")
        return False
    try:
        info = r.json()
    except ValueError:
        print(f"{NO} /api/system/info did not return JSON. The server URL is "
              f"probably wrong — retired DHIS2 demo hosts serve an nginx 404 page.")
        return False

    print(f"{OK} {config.base_url}")
    print(f"{OK} version {info.get('version')}")
    me = session.get(f"{config.base_url}/api/me", timeout=30)
    if me.status_code < 400:
        print(f"{OK} authenticated as {me.json().get('username')}")
    session.close()
    return True


def check_metadata(config: DHIS2Config):
    print("\n2. Programme metadata")
    try:
        meta = discover_program(config, program_uid=PROGRAM_UID,
                                org_unit_name=ORG_UNIT_NAME)
    except CaptureError as exc:
        print(f"{NO} {exc}")
        return None

    print(f"{OK} {meta.describe()}")
    required = [a.name for a in meta.attributes if a.mandatory and not a.generated]
    generated = [a.name for a in meta.attributes if a.generated]
    print(f"{OK} you must supply: {', '.join(required) or 'nothing'}")
    if generated:
        print(f"{OK} server generates: {', '.join(generated)}")
    return meta


def check_model(model: str) -> bool:
    print("\n3. Model API")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print(f"{NO} ANTHROPIC_API_KEY is not set")
        return False
    try:
        import anthropic
    except ImportError:
        print(f"{NO} the anthropic package is not installed")
        return False

    client = anthropic.Anthropic(api_key=key)
    try:
        msg = client.messages.create(model=model, max_tokens=16, messages=[
            {"role": "user", "content": "Reply with the single word: ready"}])
    except Exception as exc:                       # noqa: BLE001
        print(f"{NO} {type(exc).__name__}: {exc}")
        print("  List valid model ids with:")
        print("  curl https://api.anthropic.com/v1/models "
              "-H \"x-api-key: $ANTHROPIC_API_KEY\" -H 'anthropic-version: 2023-06-01'")
        return False
    print(f"{OK} {model} responded: {msg.content[0].text.strip()!r}")
    return True


def check_round_trip(config: DHIS2Config, meta) -> bool:
    print("\n4. Round trip with a synthetic record")

    # A unique surname so the read-back proves THIS record exists. The public
    # demo is writable by anyone; filtering on recency proves nothing.
    marker = "Probe" + uuid.uuid4().hex[:8].upper()

    spec = PipelineSpec(
        name="verification_probe", input_type="photo",
        fields=["first_name", "last_name", "sex", "visit_date"],
        attribute_map={
            "first_name": meta.find("first name") or "",
            "last_name": meta.find("last name") or "",
            "sex": meta.find("gender", "sex") or "",
        })
    data = {"first_name": "Verification", "last_name": marker, "sex": "Male"}

    try:
        payload = build_tracker_payload(spec, meta, data, occurred_at="2024-01-15")
        result = submit_and_verify(payload, config, meta,
                                   verify_uid=spec.attribute_map["last_name"],
                                   verify_value=marker)
    except CaptureError as exc:
        print(f"{NO} {exc}")
        return False

    print(f"{OK} status={result.status} created={result.created} "
          f"ignored={result.ignored}")
    print(f"{OK} read back {len(result.verified_records)} record(s) "
          f"with surname {marker}")
    for rec in result.verified_records[:1]:
        print(f"       trackedEntity {rec.get('trackedEntity')} "
              f"created {rec.get('createdAt', '')}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", action="store_true",
                    help="use DHIS2_* environment variables instead of the demo")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--skip-write", action="store_true")
    args = ap.parse_args()

    config = DHIS2Config.from_env() if args.env else DHIS2Config.demo()
    if not args.env:
        print("Using the PUBLIC DHIS2 demo server. Synthetic data only.")

    results = [check_connection(config)]
    meta = check_metadata(config) if results[0] else None
    results.append(meta is not None)
    results.append(check_model(args.model))
    if meta and not args.skip_write:
        results.append(check_round_trip(config, meta))

    print()
    if all(results):
        print("All checks passed. The pipeline is ready to run.")
        return 0
    print("Some checks failed. Fix the items marked [FAIL] before using the pipeline.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
