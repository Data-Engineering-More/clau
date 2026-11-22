"""Deploy the Fabric items in the repo to ONE target workspace.

Steps
  1. Preflight: the service principal can open the workspace, and the workspace is on
     a capacity. Fails fast with a clear message before anything is published.
  2. Publish every in-scope item with fabric-cicd (parameter.yml applied).
  3. Optionally remove items deleted from the repo (never lakehouses).
  4. Smoke check: every item in the repo now exists in the workspace.

Usage (pipeline or local)
  export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
  python deploy.py --workspace-id <guid> --environment TEST --repo-dir workspace
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests
from azure.identity import ClientSecretCredential
from fabric_cicd import FabricWorkspace, change_log_level, publish_all_items, unpublish_all_orphan_items

# >>> CHANGE: item types your workspace contains. fabric-cicd publishes them in dependency
# order (Lakehouse first), so notebooks can be re-pointed to the new lakehouse ID.
# Other supported types include "Environment", "SemanticModel", "Report", "Warehouse",
# "VariableLibrary". The SQL analytics endpoint is created with its lakehouse; don't list it.
ITEM_TYPES = ["Lakehouse", "Notebook", "DataPipeline"]

# >>> CHANGE (optional): items whose names match are never deployed (sandbox copies).
EXCLUDE_REGEX = r".*_dev$"

FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def as_bool(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def get_credential() -> ClientSecretCredential:
    missing = [v for v in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET") if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}. "
                 "Check the variable group and the env: mapping in the deploy step.")
    return ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )


def fabric_get(url: str, credential) -> requests.Response:
    token = credential.get_token(FABRIC_SCOPE).token
    return requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)


def fail(msg: str) -> None:
    print(f"##vso[task.logissue type=error]{msg}")
    sys.exit(1)


def preflight(workspace_id: str, credential) -> None:
    resp = fabric_get(f"{FABRIC_API}/workspaces/{workspace_id}", credential)
    if resp.status_code in (401, 403, 404):
        fail(f"Preflight: cannot open workspace {workspace_id} (HTTP {resp.status_code}). Check the ID, that the "
             "service principal is Contributor on it, and that 'Service principals can use Fabric APIs' is enabled.")
    resp.raise_for_status()
    ws = resp.json()
    if not ws.get("capacityId"):
        fail(f"Preflight: workspace '{ws.get('displayName', workspace_id)}' is not assigned to a Fabric capacity.")
    print(f"Preflight OK: workspace '{ws.get('displayName', workspace_id)}' is reachable and on a capacity.")


def repo_items(repo_dir: Path) -> set[tuple[str, str]]:
    items = set()
    for platform in repo_dir.rglob(".platform"):
        meta = json.loads(platform.read_text(encoding="utf-8"))["metadata"]
        if meta["type"] in ITEM_TYPES and not re.fullmatch(EXCLUDE_REGEX, meta["displayName"]):
            items.add((meta["type"], meta["displayName"]))
    return items


def workspace_items(workspace_id: str, credential) -> set[tuple[str, str]]:
    found, url = set(), f"{FABRIC_API}/workspaces/{workspace_id}/items"
    while url:
        resp = fabric_get(url, credential)
        resp.raise_for_status()
        body = resp.json()
        found.update((i["type"], i["displayName"]) for i in body.get("value", []))
        url = body.get("continuationUri")
    return found


def smoke_check(repo_dir: Path, workspace_id: str, credential) -> None:
    expected = repo_items(repo_dir)
    missing = sorted(expected - workspace_items(workspace_id, credential))
    print(f"Smoke check: {len(expected)} items expected, {len(expected) - len(missing)} found.")
    if missing:
        for item_type, name in missing:
            print(f"##vso[task.logissue type=error]Missing after deploy: {item_type} '{name}'")
        sys.exit(1)


def deploy(workspace_id: str, environment: str, repo_dir: Path, credential, remove_deleted: bool = False) -> None:
    """Preflight, publish, optional orphan removal, smoke check. Used by main() and dry_run.py."""
    if not GUID.fullmatch(workspace_id):
        fail(f"--workspace-id '{workspace_id}' is not a GUID. Set the workspace ID variables in the CD pipeline.")
    preflight(workspace_id, credential)
    print(f"Deploying {repo_dir} -> {environment} ({workspace_id})")
    workspace = FabricWorkspace(
        workspace_id=workspace_id,
        environment=environment,
        repository_directory=str(repo_dir),
        item_type_in_scope=ITEM_TYPES,
        token_credential=credential,
    )
    publish_all_items(workspace, item_name_exclude_regex=EXCLUDE_REGEX)
    if remove_deleted:
        # fabric-cicd never unpublishes lakehouses unless a feature flag is set, so data is safe.
        unpublish_all_orphan_items(workspace)
    smoke_check(repo_dir, workspace_id, credential)
    print("Deployment complete.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace-id", required=True)
    p.add_argument("--environment", required=True, help="DEV / TEST / PROD - must match parameter.yml keys")
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--remove-deleted-items", default="false")
    p.add_argument("--debug", default="false")
    args = p.parse_args()
    if as_bool(args.debug):
        change_log_level("DEBUG")
    deploy(args.workspace_id, args.environment.upper(), Path(args.repo_dir).resolve(),
           get_credential(), as_bool(args.remove_deleted_items))


if __name__ == "__main__":
    main()
