"""Write a CycloneDX 1.5 SBOM for the built package.

The package has no runtime dependencies, but it *contains* third-party code (python-tds, vendored
under mssql_ocp_migrate/_vendor). Scanners that read installed-package metadata cannot see vendored
code, so the SBOM declares it explicitly for JFrog Xray / Curation, Dependency-Track and similar tools.

    python ci/make_sbom.py dist/mssql-ocp-migrate.cdx.json
"""
import datetime
import json
import sys
import tomllib
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
meta = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
name, version = meta["name"], meta["version"]

sbom = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "serialNumber": f"urn:uuid:{uuid.uuid4()}",
    "version": 1,
    "metadata": {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "component": {
            "type": "library", "bom-ref": f"pkg:pypi/{name}@{version}", "name": name, "version": version,
            "purl": f"pkg:pypi/{name}@{version}", "licenses": [{"license": {"id": "MIT"}}],
            "externalReferences": [{"type": "vcs", "url": "https://github.com/karthikbd/mssql-ocp-migrate"}],
        },
    },
    "components": [{
        "type": "library", "bom-ref": "pkg:pypi/python-tds@1.17.1", "name": "python-tds", "version": "1.17.1",
        "purl": "pkg:pypi/python-tds@1.17.1", "scope": "required",
        "licenses": [{"license": {"id": "MIT"}}],
        "description": "Vendored as mssql_ocp_migrate/_vendor/pytds (imports rewritten; tls.py re-implemented "
                       "on the Python standard library ssl module; tls_server_hostname argument added).",
        "pedigree": {"notes": "Modified copy of python-tds 1.17.1. See THIRD_PARTY_NOTICES.md."},
        "externalReferences": [{"type": "vcs", "url": "https://github.com/denisenkom/pytds"}],
    }],
    "dependencies": [
        {"ref": f"pkg:pypi/{name}@{version}", "dependsOn": ["pkg:pypi/python-tds@1.17.1"]},
        {"ref": "pkg:pypi/python-tds@1.17.1", "dependsOn": []},
    ],
}
out = Path(sys.argv[1] if len(sys.argv) > 1 else f"dist/{name}-{version}.cdx.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(sbom, indent=2), encoding="utf-8")
print(f"wrote {out}")
