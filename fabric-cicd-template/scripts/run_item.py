"""Post-deploy test: run a deployed Data Pipeline or Notebook and wait for it to finish.

Proves the deployed code runs in the target workspace against that environment's data and
connections. Fails the stage if the run fails, is cancelled, or exceeds the timeout.
Progress is logged with timestamps; the result is added to the run's Summary tab.

Only use an item that is safe to run in that environment - it writes data like any normal run.

Usage
  python run_item.py --workspace-id <guid> --item-name "PL_Main" --item-type DataPipeline \
                     --timeout-minutes 60 [--summary-file out.md] [--environment TEST]
"""
import argparse
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import pipeline_log as log  # noqa: E402
from deploy import FABRIC_API, FABRIC_SCOPE, fabric_get, get_credential  # noqa: E402

JOB_TYPES = {"DataPipeline": "Pipeline", "Notebook": "RunNotebook"}
POLL_SECONDS = 30


def find_item(workspace_id: str, name: str, item_type: str, credential) -> str | None:
    url = f"{FABRIC_API}/workspaces/{workspace_id}/items"
    while url:
        resp = fabric_get(url, credential)
        resp.raise_for_status()
        body = resp.json()
        for item in body.get("value", []):
            if item["type"] == item_type and item["displayName"] == name:
                return item["id"]
        url = body.get("continuationUri")
    return None


def summarise(path, env, name, item_type, status, duration, detail) -> None:
    if not path:
        return
    icon = "✅" if status == "Completed" else "❌"
    text = (f"## {icon} {env} post-deploy test: {item_type} `{name}`\n\n"
            f"- **Result:** {status}\n- **Duration:** {duration}\n" + (f"- **Detail:** {detail}\n" if detail else ""))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")
    log.upload_summary(path)


def run(workspace_id, name, item_type, timeout_minutes, credential, summary_file=None, env="") -> None:
    timer = log.Timer()
    status, detail = "Not started", ""
    try:
        log.section(f"Post-deploy test: run {item_type} '{name}'")
        item_id = find_item(workspace_id, name, item_type, credential)
        if not item_id:
            detail = f"{item_type} '{name}' not found in workspace {workspace_id}"
            return
        token = credential.get_token(FABRIC_SCOPE).token
        start = requests.post(
            f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/jobs/instances?jobType={JOB_TYPES[item_type]}",
            headers={"Authorization": f"Bearer {token}"}, timeout=60)
        if start.status_code != 202:
            detail = f"could not start (HTTP {start.status_code}): {start.text[:300]}"
            return
        status_url = start.headers["Location"]
        log.info(f"Started. Polling every {POLL_SECONDS}s, timeout {timeout_minutes} min.")
        deadline = time.time() + timeout_minutes * 60
        status = "NotStarted"
        while time.time() < deadline:
            time.sleep(POLL_SECONDS)
            resp = fabric_get(status_url, credential)
            resp.raise_for_status()
            job = resp.json()
            status = job.get("status", "Unknown")
            log.info(f"status: {status:<11} elapsed: {timer.elapsed()}")
            if status == "Completed":
                return
            if status in ("Failed", "Cancelled", "Deduped"):
                detail = (job.get("failureReason") or {}).get("message", "no reason given")
                return
        status, detail = "TimedOut", f"still running after {timeout_minutes} minutes"
    finally:
        summarise(summary_file, env, name, item_type, status, timer.elapsed(), detail)
        if status == "Completed":
            log.section(f"Post-deploy test passed in {timer.elapsed()}")
        else:
            log.error(f"Post-deploy test FAILED: '{name}' {status}. {detail}")
            sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace-id", required=True)
    p.add_argument("--item-name", required=True)
    p.add_argument("--item-type", default="DataPipeline", choices=sorted(JOB_TYPES))
    p.add_argument("--timeout-minutes", type=int, default=60)
    p.add_argument("--summary-file")
    p.add_argument("--environment", default="")
    args = p.parse_args()
    run(args.workspace_id, args.item_name, args.item_type, args.timeout_minutes, get_credential(),
        args.summary_file, args.environment)


if __name__ == "__main__":
    main()
