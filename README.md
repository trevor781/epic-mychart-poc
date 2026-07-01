# Epic MyChart Data Extraction PoC

Proof of concept: extract a patient's full clinical record from any Epic
health system via the free, public patient-access FHIR APIs — the same
mechanism a consumer health app would use after a user opts in with
their MyChart login (SMART on FHIR standalone launch, OAuth2 + PKCE).

Zero dependencies. Python 3 stdlib only.

## One-time setup: register an app with Epic

This is the only step that requires a human (account creation).

1. Go to <https://fhir.epic.com> → **Sign Up** (free, instant).
2. **Build Apps** → **Create** a new app:
   - **Application Audience:** Patients
   - **Client type:** Public (no client secret; we use PKCE)
   - **Endpoint (redirect) URI:** `https://localhost:8765/callback` — Epic
     requires https for production apps. The script serves the callback
     over https with a self-signed cert; your browser will show a
     one-time certificate warning after login (Advanced → Proceed).
   - **APIs:** select only **USCDI R4 FHIR APIs**, read-only — at minimum:
     Patient.Read, AllergyIntolerance.Search, CarePlan.Search,
     CareTeam.Search, Condition.Search, Device.Search,
     DiagnosticReport.Search, DocumentReference.Search, Encounter.Search,
     Goal.Search, Immunization.Search, MedicationRequest.Search,
     Observation.Search (Labs, Vitals, Social History), Procedure.Search
   - Fill in the documentation/terms URLs it asks for (a GitHub repo URL works).
3. Mark the app **Ready for Production** and accept the API terms.
4. Copy the **production Client ID** (not the non-prod one) and save it
   to Keychain:

   ```sh
   security add-generic-password -a "$USER" -s "EPIC_CLIENT_ID" -w "YOUR_CLIENT_ID" -U
   ```

**Why these choices matter:** apps that are patient-facing, read-only,
USCDI-only, and production-ready qualify for Epic's *Automatic Client
Record Distribution* — the client ID is pushed to participating Epic
health systems within ~48 hours, no per-hospital approval needed.
Including even one non-USCDI or write API disqualifies the app from
auto-distribution.

## Usage

```sh
# 1. Find your health system's FHIR endpoint
python3 extract_mychart.py --search-org "sutter"

# 2. (Optional) verify the org's OAuth endpoints are discoverable
python3 extract_mychart.py --fhir-base <URL> --check

# 3. Extract — opens your browser for MyChart login + consent
python3 extract_mychart.py --fhir-base <URL>
```

Output lands in `data/<timestamp>/`: raw FHIR JSON per resource type,
plus `summary.md` with conditions, meds, allergies, immunizations,
procedures, and recent labs. `data/` is gitignored — it contains PHI.

## How it works (the consumer-app-relevant part)

1. `--search-org` queries Epic's public endpoint directory
   (<https://open.epic.com/Endpoints/R4>) — every production Epic org's
   FHIR base URL.
2. The org's `/.well-known/smart-configuration` yields its OAuth
   authorize/token endpoints.
3. The script starts a localhost callback server, opens the browser to
   the authorize URL (with PKCE challenge and `aud` = FHIR base), and
   the user logs into MyChart and consents.
4. The auth code is exchanged for an access token, which includes the
   patient's FHIR ID.
5. All USCDI resource types are pulled with pagination and dumped to
   disk.

A production consumer app does exactly this, plus: refresh tokens for
ongoing access, and registration review timelines per health system for
anything beyond the auto-distributed read-only USCDI set.
