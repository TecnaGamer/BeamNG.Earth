# Sketchfab asset metadata preflight

`tools/sketchfab_assets.py` searches downloadable-model metadata and inspects individual model records for licensing review. It outputs a versioned JSON report containing model identity, creator credits, license metadata, available mesh counts, provenance, and an explicit review status.

This tool does **not** download, convert, redistribute, or place models in BeamNG. A downloadable flag is not proof of permission for your intended use. Every record keeps `download_authorized`, `redistribution_authorized`, and `runtime_imported` false. A license with enough metadata for review receives `manual-review-required`; unavailable models and incomplete license records are blocked. No license is silently approved.

## Authentication and use

Set `SKETCHFAB_API_TOKEN` in the environment of the process that runs the tool using your local secret manager or a secure terminal prompt. Do not put the token in command arguments, repository files, reports, screenshots, or shared shell scripts. The tool accepts no token argument and never writes the credential to disk.

For example, PowerShell 7 can prompt without recording the token in command history:

```powershell
$env:SKETCHFAB_API_TOKEN = Read-Host 'Sketchfab API token' -MaskInput
try {
    python tools/sketchfab_assets.py search 'stone bridge' --limit 5
} finally {
    Remove-Item Env:SKETCHFAB_API_TOKEN
}
```

With the environment already set, run:

```sh
python tools/sketchfab_assets.py search "stone bridge" --limit 5
python tools/sketchfab_assets.py model MODEL_UID
python tools/sketchfab_assets.py --timeout 20 search "warehouse" --limit 10
```

Replace `MODEL_UID` with the model's 32-character hexadecimal UID. Model URLs are not accepted. Search text is limited to 120 characters and results to 1–24 records from the first response page. The tool does not crawl pages or follow pagination links. Redirect standard output to a local JSON file when a review record is needed. Exit status is 0 for a successfully generated report, even when a model is blocked for asset use; operational failures return 2 with a sanitized diagnostic on standard error.

## Review workflow

1. Search by landmark or building category. Search-result relevance does not establish a geographic match.
2. Inspect the selected UID using `model` to obtain its detail record. Search records may omit a license URL or geometry counts; omitted fields remain unknown.
3. Review the original model page, creator identity, current license, attribution requirements, modification and commercial-use restrictions, and redistribution terms for the intended package. Obtain any additional permissions needed. Metadata alone is not proof of ownership or a license grant.
4. Before a future importer is enabled, validate authenticated download entitlement, archive safety, format compatibility, scale, material/texture budgets, collision suitability, and LOD performance. Preserve source credits and the reviewed license with each accepted artifact.

No automatic download or runtime importer is included in this milestone.

## Transport and validation boundaries

Only `api.sketchfab.com` over verified HTTPS is contacted. Only the search and model-detail paths are implemented. Authorization uses an API token header; redirects, returned asset URLs, and pagination URLs are never followed. Responses are capped at 2 MiB. The default timeout is 15 seconds, configurable from 1 to 30; the socket timeout and a read-loop deadline bound stalled requests. Rate limits return a clear error with no retry loop. Error response bodies, headers, and low-level exception details are not printed. Token values echoed in metadata are redacted before normalization.

Validation: 14 mocked HTTP tests cover token forwarding to the fixed origin, redirect rejection, error redaction, response bounds, invalid inputs, deadlines, missing licenses, and CLI output. These tests run offline:

```sh
python -B -m unittest discover -s server/tests -p test_sketchfab_assets.py -v
```

Live account access, current asset availability, downloading, legal review of a particular asset, conversion, placement, and BeamNG runtime performance remain **unverified**. This milestone adds no dependency or change to the driving path.

## API references

The official [Data API authentication reference](https://docs.sketchfab.com/data-api/v3/index.html) and [Python examples](https://sketchfab.com/developers/data-api/v3/python) document API-token authentication for metadata. The [Download API](https://sketchfab.com/developers/download-api) and [download workflow](https://sketchfab.com/developers/download-api/downloading-models) describe separate authenticated acquisition requirements. These references were checked on 2026-11-02; direct developer-page fetches returned HTTP 403, so the indexed official documentation was consulted. No undocumented authentication workaround was used.
