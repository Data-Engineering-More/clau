"""Deploy the Fabric items in the repo to ONE target workspace, with step-by-step logging.

Steps (each is a section in the Azure DevOps log)
  1. Preflight   service principal can open the workspace; workspace is on a capacity
  2. Plan        lists every item and whether it will be CREATED, UPDATED or SKIPPED
  3. Publish     fabric-cicd publish with parameter.yml applied (output in a collapsible group)
  4. Clean-up    optional: remove items deleted from the repo (never lakehouses)
  5. Smoke check every repo item now exists in the workspace
  A markdown report is written to --summary-file and shown on the run's Summary tab.

Usage (pipeline or local)
  export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
  python deploy.py --workspace-id <guid> --environment TEST --repo-dir workspace [--summary-file out.md]
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

sys.path.insert(0, str(Path(__file__).parent))
import pipeline_log as log  # noqa: E402

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


class DeployError(Exception):
    pass


def as_bool(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def get_credential() -> ClientSecretCredential:
    missing = [v for v in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET") if not os.environ.get(v)]
    if missing:
        log.error(f"Missing environment variables: {', '.join(missing)}. "
                  "Check the variable group and the env: mapping in the deploy step.")
        sys.exit(1)
    return ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )


def fabric_get(url: str, credential) -> requests.Response:
    token = credential.get_token(FABRIC_SCOPE).token
    return requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)


def fail(msg: str) -> None:
    """Kept for run_item.py: log an error and stop."""
    log.error(msg)
    sys.exit(1)


def preflight(workspace_id: str, credential) -> dict:
    resp = fabric_get(f"{FABRIC_API}/workspaces/{workspace_id}", credential)
    if resp.status_code in (401, 403, 404):
        raise DeployError(
            f"Preflight: cannot open workspace {workspace_id} (HTTP {resp.status_code}). Check the ID, that the "
            "service principal is Contributor on it, and that 'Service principals can use Fabric APIs' is enabled.")
    resp.raise_for_status()
    ws = resp.json()
    if not ws.get("capacityId"):
        raise DeployError(f"Preflight: workspace '{ws.get('displayName', workspace_id)}' is not on a Fabric capacity.")
    log.info(f"Workspace '{ws.get('displayName', workspace_id)}' is reachable and on a capacity.")
    return ws


def repo_items(repo_dir: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(items to deploy, items skipped by EXCLUDE_REGEX)."""
    deploy, skipped = set(), set()
    for platform in repo_dir.rglob(".platform"):
        meta = json.loads(platform.read_text(encoding="utf-8"))["metadata"]
        if meta["type"] not in ITEM_TYPES:
            continue
        key = (meta["type"], meta["displayName"])
        (skipped if re.fullmatch(EXCLUDE_REGEX, meta["displayName"]) else deploy).add(key)
    return deploy, skipped


def workspace_items(workspace_id: str, credential) -> set[tuple[str, str]]:
    found, url = set(), f"{FABRIC_API}/workspaces/{workspace_id}/items"
    while url:
        resp = fabric_get(url, credential)
        resp.raise_for_status()
        body = resp.json()
        found.update((i["type"], i["displayName"]) for i in body.get("value", []))
        url = body.get("continuationUri")
    return found


def write_summary(path, env, workspace, rows, status, duration, problems, removed) -> None:
    if not path:
        return
    icon = "✅" if status == "Succeeded" else "❌"
    lines = [f"## {icon} {env} deployment: {status}", "",
             f"- **Workspace:** {workspace.get('displayName', '?')} (`{workspace.get('id', '?')}`)",
             f"- **Duration:** {duration}",
             f"- **Items:** {sum(1 for r in rows if r[2] == 'Create')} created, "
             f"{sum(1 for r in rows if r[2] == 'Update')} updated, "
             f"{sum(1 for r in rows if r[2] == 'Skip')} skipped, {len(removed)} removed", ""]
    if problems:
        lines += ["### Problems", ""] + [f"- {p}" for p in problems] + [""]
    lines += ["### Items", "", log.md_table(["Type", "Item", "Action", "Result"], rows), ""]
    if removed:
        lines += ["### Removed from workspace", ""] + [f"- {t} `{n}`" for t, n in sorted(removed)] + [""]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    log.upload_summary(path)


def deploy(workspace_id: str, environment: str, repo_dir: Path, credential,
           remove_deleted: bool = False, summary_file: str | None = None) -> None:
    """Preflight, plan, publish, optional clean-up, smoke check. Used by main() and dry_run.py."""
    timer = log.Timer()
    workspace: dict = {"id": workspace_id}
    rows: list[list[str]] = []
    problems: list[str] = []
    removed: set = set()
    status = "Failed"
    try:
        if not GUID.fullmatch(workspace_id):
            raise DeployError(f"--workspace-id '{workspace_id}' is not a GUID. Set the workspace ID variables in the CD pipeline.")

        log.section(f"1/5 Preflight - {environment}")
        workspace = preflight(workspace_id, credential)
        before = workspace_items(workspace_id, credential)

        log.section("2/5 Deployment plan")
        to_deploy, skipped = repo_items(repo_dir)
        if not to_deploy:
            raise DeployError(f"No {', '.join(ITEM_TYPES)} items found in {repo_dir}")
        plan = {key: ("Update" if key in before else "Create") for key in to_deploy}
        plan.update({key: "Skip" for key in skipped})
        rows = [[t, n, a, "pending"] for (t, n), a in sorted(plan.items())]
        log.print_table(["Type", "Item", "Action"], [r[:3] for r in rows])
        log.info(f"{sum(a == 'Create' for a in plan.values())} to create, "
                 f"{sum(a == 'Update' for a in plan.values())} to update, {len(skipped)} skipped (match {EXCLUDE_REGEX}).")

        log.section("3/5 Publish (fabric-cicd)")
        fabric_ws = FabricWorkspace(
            workspace_id=workspace_id,
            environment=environment,
            repository_directory=str(repo_dir),
            item_type_in_scope=ITEM_TYPES,
            token_credential=credential,
        )
        log.group(f"fabric-cicd output for {environment}")
        try:
            publish_all_items(fabric_ws, item_name_exclude_regex=EXCLUDE_REGEX)
        finally:
            log.endgroup()
        log.info(f"Publish finished after {timer.elapsed()}.")

        log.section("4/5 Remove items deleted from the repo")
        if remove_deleted:
            log.group("fabric-cicd output (clean-up)")
            try:
                # fabric-cicd never unpublishes lakehouses unless a feature flag is set, so data is safe.
                unpublish_all_orphan_items(fabric_ws)
            finally:
                log.endgroup()
        else:
            log.info("Skipped (removeDeletedItems = false).")

        log.section("5/5 Smoke check")
        after = workspace_items(workspace_id, credential)
        removed = {k for k in before - after if k[0] in ITEM_TYPES}
        for r in rows:
            key = (r[0], r[1])
            if r[2] == "Skip":
                r[3] = "not deployed"
            else:
                r[3] = "OK" if key in after else "MISSING"
                if key not in after:
                    problems.append(f"Missing after deploy: {key[0]} '{key[1]}'")
        log.print_table(["Type", "Item", "Action", "Result"], rows)
        if problems:
            raise DeployError(f"Smoke check: {len(problems)} item(s) missing after deploy")
        log.info(f"All {len(to_deploy)} item(s) present in the workspace.")
        status = "Succeeded"
    except DeployError as exc:
        problems.insert(0, str(exc))
    except Exception as exc:  # fabric-cicd / HTTP errors
        problems.insert(0, f"{type(exc).__name__}: {exc}")
    finally:
        for r in rows:
            if r[3] == "pending":
                r[3] = "not reached"
        write_summary(summary_file, environment, workspace, rows, status, timer.elapsed(), problems, removed)

    if status != "Succeeded":
        for p in problems:
            log.error(p)
        log.error(f"{environment} deployment FAILED after {timer.elapsed()}.")
        sys.exit(1)
    log.add_build_tag(f"deployed-{environment.lower()}")
    log.section(f"{environment} deployment succeeded in {timer.elapsed()}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace-id", required=True)
    p.add_argument("--environment", required=True, help="DEV / TEST / PROD - must match parameter.yml keys")
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--remove-deleted-items", default="false")
    p.add_argument("--debug", default="false")
    p.add_argument("--summary-file", help="markdown report, shown on the run's Summary tab")
    args = p.parse_args()
    if as_bool(args.debug):
        change_log_level("DEBUG")
    deploy(args.workspace_id, args.environment.upper(), Path(args.repo_dir).resolve(),
           get_credential(), as_bool(args.remove_deleted_items), args.summary_file)


if __name__ == "__main__":
    main()
