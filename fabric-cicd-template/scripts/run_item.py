"""Post-deploy test: run a deployed Data Pipeline or Notebook and wait for it to finish.

This is the real end-to-end test. It proves the deployed code runs in the target workspace
against that environment's data and connections. It fails the stage if the run fails,
is cancelled, or exceeds the timeout.

Only switch this on where running the item is safe (usually TEST). It writes data like
any normal run.

Usage
  python run_item.py --workspace-id <guid> --item-name "PL_Main" --item-type DataPipeline --timeout-minutes 60
"""
import argparse
import sys
import time

import requests

from deploy import FABRIC_API, fabric_get, fail, get_credential

JOB_TYPES = {"DataPipeline": "Pipeline", "Notebook": "RunNotebook"}
POLL_SECONDS = 30


def find_item(workspace_id: str, name: str, item_type: str, credential) -> str:
    url = f"{FABRIC_API}/workspaces/{workspace_id}/items"
    while url:
        resp = fabric_get(url, credential)
        resp.raise_for_status()
        body = resp.json()
        for item in body.get("value", []):
            if item["type"] == item_type and item["displayName"] == name:
                return item["id"]
        url = body.get("continuationUri")
    fail(f"Post-deploy test: {item_type} '{name}' not found in workspace {workspace_id}")
    return ""


def run(workspace_id: str, name: str, item_type: str, timeout_minutes: int, credential) -> None:
    item_id = find_item(workspace_id, name, item_type, credential)
    token = credential.get_token("https://api.fabric.microsoft.com/.default").token
    start = requests.post(
        f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/jobs/instances?jobType={JOB_TYPES[item_type]}",
        headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if start.status_code != 202:
        fail(f"Post-deploy test: could not start '{name}' (HTTP {start.status_code}): {start.text[:300]}")
    status_url = start.headers["Location"]
    print(f"Started {item_type} '{name}'. Polling every {POLL_SECONDS}s, timeout {timeout_minutes} min.")

    deadline = time.time() + timeout_minutes * 60
    status = "NotStarted"
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        resp = fabric_get(status_url, credential)
        resp.raise_for_status()
        job = resp.json()
        status = job.get("status", "Unknown")
        print(f"  status: {status}")
        if status == "Completed":
            print(f"Post-deploy test passed: '{name}' completed.")
            return
        if status in ("Failed", "Cancelled", "Deduped"):
            reason = (job.get("failureReason") or {}).get("message", "no reason given")
            fail(f"Post-deploy test: '{name}' ended with status {status}: {reason}")
    fail(f"Post-deploy test: '{name}' still {status} after {timeout_minutes} minutes")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace-id", required=True)
    p.add_argument("--item-name", required=True)
    p.add_argument("--item-type", default="DataPipeline", choices=sorted(JOB_TYPES))
    p.add_argument("--timeout-minutes", type=int, default=60)
    args = p.parse_args()
    run(args.workspace_id, args.item_name, args.item_type, args.timeout_minutes, get_credential())


if __name__ == "__main__":
    main()
