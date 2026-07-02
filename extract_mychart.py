#!/usr/bin/env python3
"""Extract your MyChart data via Epic's patient-facing FHIR APIs.

Proof of concept for consumer health data access under the Cures Act:
a patient authorizes this app via their MyChart login (SMART on FHIR
standalone launch, OAuth2 authorization code + PKCE), and the app pulls
their clinical record as FHIR R4 resources.

Zero dependencies — Python 3 stdlib only.

Usage:
    # Find your health system's FHIR endpoint
    python3 extract_mychart.py --search-org "sutter"

    # Run the full extraction (opens browser for MyChart login)
    python3 extract_mychart.py --fhir-base https://.../api/FHIR/R4/

Client ID is read from the EPIC_CLIENT_ID env var, or from macOS
Keychain (service name EPIC_CLIENT_ID) as a fallback.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

ENDPOINT_DIRECTORY_URL = "https://open.epic.com/Endpoints/R4"
REDIRECT_PORT = 8765
# Epic requires an https redirect URI. The hosted page (site/) receives the
# OAuth redirect and forwards the query string to localhost:8765 client-side.
REDIRECT_URI = "https://personal-health-2h9.pages.dev/callback"
DATA_DIR = Path(__file__).parent / "data"

# USCDI resource types to pull, with any search params Epic requires.
# Observation and DocumentReference require a category filter on Epic.
RESOURCE_QUERIES = [
    ("AllergyIntolerance", {}),
    ("CarePlan", {"category": "assess-plan"}),
    ("CareTeam", {}),
    ("Condition", {}),
    ("Device", {}),
    ("DiagnosticReport", {}),
    ("DocumentReference", {"category": "clinical-note"}),
    ("Encounter", {}),
    ("Goal", {}),
    ("Immunization", {}),
    ("MedicationRequest", {}),
    ("Observation", {"category": "laboratory"}),
    ("Observation", {"category": "vital-signs"}),
    ("Observation", {"category": "social-history"}),
    ("Procedure", {}),
    # Everything else the app may be entitled to — each skips gracefully
    # (4xx) at orgs/apps where it isn't available.
    ("ServiceRequest", {}),
    ("Coverage", {}),
    ("ExplanationOfBenefit", {}),
    ("Specimen", {}),
    ("QuestionnaireResponse", {}),
    ("Appointment", {}),
    ("FamilyMemberHistory", {}),
    ("RelatedPerson", {}),
]


def http_get_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={
        "Accept": "application/fhir+json, application/json",
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_bytes(url, accept, access_token, timeout=30):
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "Authorization": f"Bearer {access_token}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


SANDBOX_FHIR_BASE = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/"


def get_client_id(sandbox=False):
    key = "EPIC_CLIENT_ID_NONPROD" if sandbox else "EPIC_CLIENT_ID"
    client_id = os.environ.get(key)
    if client_id:
        return client_id.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""),
             "-s", key, "-w"],
            capture_output=True, text=True, check=True,
        )
        if out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    sys.exit(
        f"No client ID found. Save it to Keychain with:\n"
        f'  security add-generic-password -a "$USER" -s "{key}" -w "YOUR_CLIENT_ID" -U\n'
        f"or set the {key} environment variable."
    )


def search_orgs(query):
    """Search Epic's public endpoint directory by organization name."""
    print(f"Downloading Epic endpoint directory ({ENDPOINT_DIRECTORY_URL})...")
    bundle = http_get_json(ENDPOINT_DIRECTORY_URL)
    matches = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        name = resource.get("name", "")
        address = resource.get("address", "")
        if query.lower() in name.lower():
            matches.append((name, address))
    return matches


def discover_smart_config(fhir_base):
    """Get OAuth endpoints from the server's SMART configuration."""
    base = fhir_base.rstrip("/")
    try:
        config = http_get_json(f"{base}/.well-known/smart-configuration")
        return config["authorization_endpoint"], config["token_endpoint"]
    except Exception:
        pass
    # Fallback: OAuth URIs extension in the CapabilityStatement
    metadata = http_get_json(f"{base}/metadata")
    for rest in metadata.get("rest", []):
        for ext in rest.get("security", {}).get("extension", []):
            if ext.get("url", "").endswith("oauth-uris"):
                uris = {e["url"]: e.get("valueUri") for e in ext.get("extension", [])}
                return uris["authorize"], uris["token"]
    sys.exit(f"Could not discover OAuth endpoints for {fhir_base}")


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches the OAuth redirect and stashes the authorization code.

    Ignores anything that isn't a real callback (favicon requests, bare
    visits with no query string) so a stray hit can't consume the flow.
    """
    result = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if parsed.path != "/callback" or not ("code" in params or "error" in params):
            print(f"  (ignoring stray request: {parsed.path}?{parsed.query})")
            self.send_error(404)
            return
        CallbackHandler.result = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = "code" in params
        self.wfile.write(
            b"<html><body style='font-family:sans-serif'><h2>"
            + (b"Authorized. You can close this tab and return to the terminal."
               if ok else b"Authorization failed. See terminal for details.")
            + b"</h2></body></html>"
        )

    def log_message(self, *args):
        pass


def authorize(client_id, fhir_base, authorize_url, token_url):
    """Run the SMART standalone launch: browser login -> code -> token."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid fhirUser",
        "state": state,
        "aud": fhir_base,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{authorize_url}?{urllib.parse.urlencode(auth_params)}"

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), CallbackHandler)
    server.timeout = 600

    def serve_until_callback():
        while not CallbackHandler.result:
            server.handle_request()

    thread = threading.Thread(target=serve_until_callback, daemon=True)
    thread.start()

    print("\nOpening your browser for MyChart login...")
    print(f"If it doesn't open, visit:\n\n{url}\n")
    webbrowser.open(url)
    thread.join(timeout=600)

    result = CallbackHandler.result
    if "error" in result:
        sys.exit(f"Authorization failed: {result.get('error')}: {result.get('error_description', '')}")
    code = result.get("code")
    if not code:
        sys.exit("Timed out waiting for authorization (10 min).")
    if result.get("state") != state:
        sys.exit("State mismatch in OAuth callback (possible CSRF or a stale "
                 "tab from an earlier attempt) — aborting. Re-run and use the "
                 "freshly opened browser tab.")

    token_body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        token_url, data=token_body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            token = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"Token exchange failed ({e.code}): {e.read().decode()}")
    return token


def fetch_all_pages(url, access_token):
    """Follow a FHIR search through all its pages; return resources."""
    resources = []
    while url:
        bundle = http_get_json(url, headers={"Authorization": f"Bearer {access_token}"})
        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            if resource.get("resourceType") != "OperationOutcome":
                resources.append(resource)
        url = next(
            (link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"),
            None,
        )
    return resources


def extract(client_id, fhir_base):
    fhir_base = fhir_base.rstrip("/") + "/"
    authorize_url, token_url = discover_smart_config(fhir_base)
    print(f"Authorize endpoint: {authorize_url}")
    print(f"Token endpoint:     {token_url}")

    token = authorize(client_id, fhir_base, authorize_url, token_url)
    access_token = token["access_token"]
    patient_id = token.get("patient")
    if not patient_id:
        sys.exit(f"No patient ID in token response: {json.dumps(token, indent=2)}")
    print(f"\nAuthorized. Patient FHIR ID: {patient_id}")

    DATA_DIR.mkdir(exist_ok=True)
    extracted = {}

    patient = http_get_json(
        f"{fhir_base}Patient/{patient_id}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    extracted["Patient"] = [patient]
    print(f"  Patient: {_patient_name(patient)}")

    for resource_type, extra_params in RESOURCE_QUERIES:
        params = {"patient": patient_id, **extra_params}
        label = resource_type + (f" ({extra_params['category']})" if "category" in extra_params else "")
        url = f"{fhir_base}{resource_type}?{urllib.parse.urlencode(params)}"
        try:
            resources = fetch_all_pages(url, access_token)
            extracted.setdefault(resource_type, [])
            existing_ids = {r.get("id") for r in extracted[resource_type]}
            extracted[resource_type] += [r for r in resources if r.get("id") not in existing_ids]
            print(f"  {label}: {len(resources)}")
        except urllib.error.HTTPError as e:
            print(f"  {label}: SKIPPED ({e.code} — likely not enabled for this app/org)")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = DATA_DIR / timestamp
    out_dir.mkdir(parents=True)
    for resource_type, resources in extracted.items():
        (out_dir / f"{resource_type}.json").write_text(json.dumps(resources, indent=2))

    download_attachments(out_dir, extracted, access_token)
    write_summary(out_dir, extracted, fhir_base)
    print(f"\nDone. Raw FHIR data in {out_dir}/, human-readable summary in {out_dir}/summary.md")


def download_attachments(out_dir, extracted, access_token):
    """Pull note/report bodies (Binary attachments) while the token is fresh."""
    att_dir = out_dir / "attachments"
    jobs = []
    for d in extracted.get("DocumentReference", []):
        atts = [c.get("attachment", {}) for c in d.get("content", [])]
        jobs.append(("DocumentReference", d.get("id"), atts))
    for r in extracted.get("DiagnosticReport", []):
        if r.get("presentedForm"):
            jobs.append(("DiagnosticReport", r.get("id"), r["presentedForm"]))
    if not jobs:
        return
    att_dir.mkdir(exist_ok=True)
    ext_for = {"text/html": "html", "text/rtf": "rtf", "application/pdf": "pdf",
               "text/plain": "txt", "application/xml": "xml"}
    saved = failed = 0
    for rtype, rid, atts in jobs:
        # prefer html > plain > pdf > rtf when multiple formats exist
        atts = sorted(atts, key=lambda a: ["text/html", "text/plain", "application/pdf",
                                           "text/rtf"].index(a.get("contentType"))
                      if a.get("contentType") in ("text/html", "text/plain",
                                                  "application/pdf", "text/rtf") else 9)
        for att in atts:
            url, ctype = att.get("url"), att.get("contentType", "")
            if not url:
                continue
            try:
                blob = http_get_bytes(url, ctype or "*/*", access_token)
                ext = ext_for.get(ctype, "bin")
                (att_dir / f"{rtype}_{rid}.{ext}").write_bytes(blob)
                saved += 1
                break  # one format per document is enough
            except (urllib.error.HTTPError, urllib.error.URLError):
                failed += 1
    print(f"  Attachments (note/report bodies): {saved} saved"
          + (f", {failed} unavailable" if failed else ""))


def _patient_name(patient):
    for name in patient.get("name", []):
        if name.get("use") == "official" or len(patient.get("name", [])) == 1:
            return f"{' '.join(name.get('given', []))} {name.get('family', '')}".strip()
    return patient.get("id", "unknown")


def _codeable_text(cc):
    if not cc:
        return ""
    return cc.get("text") or next(
        (c.get("display") for c in cc.get("coding", []) if c.get("display")), ""
    )


def write_summary(out_dir, extracted, fhir_base):
    lines = [f"# MyChart Extraction Summary",
             f"\nSource: {fhir_base}",
             f"Extracted: {datetime.now(timezone.utc).isoformat()}\n"]

    patient = extracted.get("Patient", [{}])[0]
    lines.append(f"**Patient:** {_patient_name(patient)} — DOB {patient.get('birthDate', '?')}\n")

    lines.append("## Resource counts\n")
    for rt, resources in sorted(extracted.items()):
        lines.append(f"- {rt}: {len(resources)}")

    def section(title, rt, fmt):
        items = extracted.get(rt, [])
        if not items:
            return
        lines.append(f"\n## {title} ({len(items)})\n")
        for r in items:
            text = fmt(r)
            if text:
                lines.append(f"- {text}")

    section("Conditions", "Condition", lambda r: _codeable_text(r.get("code")))
    section("Medications", "MedicationRequest",
            lambda r: _codeable_text(r.get("medicationCodeableConcept"))
            or r.get("medicationReference", {}).get("display", ""))
    section("Allergies", "AllergyIntolerance", lambda r: _codeable_text(r.get("code")))
    section("Immunizations", "Immunization",
            lambda r: f"{_codeable_text(r.get('vaccineCode'))} ({r.get('occurrenceDateTime', '?')[:10]})")
    section("Procedures", "Procedure",
            lambda r: f"{_codeable_text(r.get('code'))} ({r.get('performedDateTime', r.get('performedPeriod', {}).get('start', '?'))[:10]})")

    obs = extracted.get("Observation", [])
    labs = [o for o in obs if any(
        c.get("code") == "laboratory"
        for cat in o.get("category", []) for c in cat.get("coding", []))]
    if labs:
        labs.sort(key=lambda o: o.get("effectiveDateTime", ""), reverse=True)
        lines.append(f"\n## Recent labs (latest 25 of {len(labs)})\n")
        for o in labs[:25]:
            value = ""
            if "valueQuantity" in o:
                q = o["valueQuantity"]
                value = f"{q.get('value')} {q.get('unit', '')}".strip()
            elif "valueString" in o:
                value = o["valueString"]
            elif "valueCodeableConcept" in o:
                value = _codeable_text(o["valueCodeableConcept"])
            lines.append(
                f"- {o.get('effectiveDateTime', '?')[:10]} — "
                f"{_codeable_text(o.get('code'))}: {value}")

    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--search-org", metavar="NAME",
                        help="Search Epic's endpoint directory for a health system")
    parser.add_argument("--fhir-base", metavar="URL",
                        help="FHIR R4 base URL of your health system")
    parser.add_argument("--check", action="store_true",
                        help="With --fhir-base: only verify OAuth endpoint discovery, don't authorize")
    parser.add_argument("--sandbox", action="store_true",
                        help="Run against Epic's sandbox with the non-production client ID "
                             "(test patient login: fhircamila / epicepic1)")
    args = parser.parse_args()

    if args.sandbox:
        extract(get_client_id(sandbox=True), SANDBOX_FHIR_BASE)
        return

    if args.search_org:
        matches = search_orgs(args.search_org)
        if not matches:
            print(f"No organizations matching '{args.search_org}'.")
        for name, address in matches:
            print(f"  {name}\n    {address}")
        return

    if not args.fhir_base:
        parser.error("Provide --fhir-base URL (find it with --search-org) or --search-org NAME")

    if args.check:
        authorize_url, token_url = discover_smart_config(args.fhir_base.rstrip("/") + "/")
        print(f"OK — SMART discovery works for {args.fhir_base}")
        print(f"  authorize: {authorize_url}\n  token:     {token_url}")
        return

    extract(get_client_id(), args.fhir_base)


if __name__ == "__main__":
    main()
