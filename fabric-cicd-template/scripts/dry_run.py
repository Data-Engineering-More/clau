"""Offline dry-run deployment. Runs in the Build stage on every PR and CD run.

Deploys the repo to an in-memory FAKE Fabric workspace for each target environment, through
the real deploy.py and the real fabric-cicd code (only HTTP is faked). It then inspects what
would have been published. No credentials, no network, nothing touches Fabric.

Fails if, for any environment:
  - fabric-cicd rejects parameter.yml or an item definition
  - a published item still contains a SOURCE ID - a literal GUID from parameter.yml, a value
    captured by one of its regex rules, or --source-workspace-id
  - a notebook's default lakehouse does not point at a lakehouse in the target workspace
  - an item from the repo was not published

Usage
  python dry_run.py --repo-dir workspace [--environments TEST PROD] [--junit results/dry-run.xml]
"""
import argparse
import base64
import copy
import io
import json
import logging
import re
import sys
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import deploy  # noqa: E402

GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


# ----------------------------------------------------------------------------- fake Fabric
class FakeFabric:
    """Just enough of the Fabric REST API for fabric-cicd publish + smoke check."""

    def __init__(self, workspace_id: str):
        self.ws = workspace_id
        self.items: dict[str, dict] = {}
        self.defs: dict[str, dict] = {}
        self.unhandled: list[str] = []

    @staticmethod
    def _resp(status, body=None, url=""):
        r = requests.Response()
        r.status_code, r.url = status, url
        r._content = json.dumps(body).encode() if body is not None else b""
        r.headers.update({"Content-Type": "application/json"})
        r.reason = "OK" if status < 400 else "Error"
        return r

    def request(self, method, url, **kw):
        method, body = method.upper(), kw.get("json")
        path = re.sub(r"^https://api\.(fabric\.microsoft|powerbi)\.com", "", url.split("?")[0])
        ok = lambda b=None, s=200: self._resp(s, b, url)  # noqa: E731
        if path == "/v1/workspaces" and method == "GET":
            return ok({"value": [{"id": self.ws, "displayName": "dry-run", "type": "Workspace"}]})
        m = re.match(rf"^/v1/workspaces/{self.ws}(.*)$", path)
        rest = m.group(1) if m else None
        if rest == "" and method == "GET":
            return ok({"id": self.ws, "displayName": "dry-run", "capacityId": "dry-run-capacity"})
        if rest == "/items" and method == "GET":
            return ok({"value": list(self.items.values())})
        if rest == "/folders" and method == "GET":
            return ok({"value": []})
        if rest == "/items" and method == "POST":
            iid = str(uuid.uuid4())
            self.items[iid] = {"id": iid, "type": body["type"], "displayName": body["displayName"],
                               "description": body.get("description", ""), "workspaceId": self.ws}
            self.defs[iid] = copy.deepcopy(body.get("definition"))
            return ok(self.items[iid], 201)
        if rest:
            mm = re.match(r"^/items/([^/]+)/updateDefinition$", rest)
            if mm and method == "POST":
                self.defs[mm.group(1)] = copy.deepcopy(body.get("definition"))
                return ok({})
            mm = re.match(r"^/items/([^/]+)/getDefinition$", rest)
            if mm and method == "POST":
                return ok({"definition": self.defs.get(mm.group(1)) or {"parts": []}})
            mm = re.match(r"^/items/([^/]+)$", rest)
            if mm and mm.group(1) in self.items:
                if method == "GET":
                    return ok(self.items[mm.group(1)])
                if method == "PATCH":
                    self.items[mm.group(1)].update({k: v for k, v in (body or {}).items() if k in ("displayName", "description")})
                    return ok(self.items[mm.group(1)])
                if method == "DELETE":
                    self.items.pop(mm.group(1))
                    return ok({})
            if re.match(r"^/items/[^/]+/shortcuts$", rest) and method == "GET":
                return ok({"value": []})
            mm = re.match(r"^/lakehouses/([^/]+)$", rest)
            if mm and mm.group(1) in self.items and method == "GET":
                return ok({**self.items[mm.group(1)], "properties": {"sqlEndpointProperties": {
                    "connectionString": f"{self.ws}.dry-run.fabric", "id": str(uuid.uuid4()), "provisioningStatus": "Success"}}})
        self.unhandled.append(f"{method} {path}")
        return self._resp(404, {"errorCode": "NotSupportedByDryRun", "message": f"{method} {path}"}, url)

    def published(self) -> dict[tuple[str, str], dict[str, str]]:
        out = {}
        for iid, item in self.items.items():
            parts = (self.defs.get(iid) or {}).get("parts", [])
            out[(item["type"], item["displayName"])] = {
                p["path"]: base64.b64decode(p["payload"]).decode("utf-8", "replace") for p in parts}
        return out


class FakeCredential:
    @staticmethod
    def _b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    def get_token(self, *_a, **_k):
        token = f"{self._b64({'alg': 'none'})}.{self._b64({'idtyp': 'app', 'appid': 'dry-run', 'oid': 'dry-run'})}.sig"
        return mock.Mock(token=token, expires_on=9999999999)


# ----------------------------------------------------------------------------- checks
def source_ids(repo: Path, extra: list[str]) -> set[str]:
    """IDs that must NOT survive into a TEST/PROD deployment."""
    ids = {g.lower() for g in extra if GUID.fullmatch(g)}
    params = yaml.safe_load((repo / "parameter.yml").read_text(encoding="utf-8")) or {}
    regexes = []
    for rule in params.get("find_replace") or []:
        find = str(rule.get("find_value", ""))
        if str(rule.get("is_regex", "")).lower() == "true":
            regexes.append(re.compile(find))
        else:
            ids.update(g.lower() for g in GUID.findall(find))
    for path in repo.rglob("*"):
        if path.is_file() and path.name != "parameter.yml" and path.suffix in (".py", ".json", ".ipynb"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for rx in regexes:
                ids.update(m.group(1).lower() for m in rx.finditer(text) if GUID.fullmatch(m.group(1)))
    return ids


def dry_run(repo: Path, env: str, forbidden: set[str]) -> list[str]:
    ws = str(uuid.uuid4())
    fake = FakeFabric(ws)
    problems: list[str] = []
    log = io.StringIO()
    with mock.patch.object(requests.Session, "request", fake.request), mock.patch("time.sleep"), redirect_stdout(log):
        try:
            deploy.deploy(ws, env, repo, FakeCredential())
        except SystemExit as exc:
            if exc.code not in (0, None):
                problems.append(f"deploy stopped: {exc.code if isinstance(exc.code, str) else 'see log below'}")
        except Exception as exc:  # fabric-cicd raises on bad parameter.yml / definitions
            problems.append(f"fabric-cicd error: {type(exc).__name__}: {exc}")
    if fake.unhandled:
        problems.append(f"calls the dry run does not simulate (new item type or fabric-cicd version?): {sorted(set(fake.unhandled))}")

    published = fake.published()
    lakehouse_ids = {i["id"] for i in fake.items.values() if i["type"] == "Lakehouse"}
    for (item_type, name), parts in published.items():
        for part, text in parts.items():
            leaked = sorted(g for g in forbidden if g in text.lower())
            for g in leaked:
                problems.append(f"{item_type} '{name}' ({part}) still contains source ID {g}")
            if item_type == "Notebook" and lakehouse_ids:
                m = re.search(r'"default_lakehouse":\s*"([^"]+)"', text)
                if m and m.group(1).lower() not in lakehouse_ids and m.group(1).lower() not in forbidden:
                    problems.append(f"Notebook '{name}' default lakehouse {m.group(1)} is not a lakehouse in the target")
    if problems:
        problems.append("--- deploy log (last 40 lines) ---\n" + "\n".join(log.getvalue().splitlines()[-40:]))
    else:
        print(f"  {env}: {len(published)} item(s) published to fake workspace, no source IDs left.")
    return problems


def write_junit(path: str, results: dict[str, list[str]]) -> None:
    cases = "".join(
        f'<testcase classname="dry_run" name="{env}">'
        + (f'<failure message="{len(p)} problem(s)">{escape(chr(10).join(p))}</failure>' if p else "")
        + "</testcase>" for env, p in results.items())
    fails = sum(1 for p in results.values() if p)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="Offline dry-run deployment" '
                          f'tests="{len(results)}" failures="{fails}">{cases}</testsuite>\n', encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--environments", nargs="+", default=["TEST", "PROD"])
    p.add_argument("--source-workspace-id", action="append", default=[],
                   help="extra ID(s) that must not appear after deployment, e.g. the DEV workspace")
    p.add_argument("--junit")
    args = p.parse_args()
    repo = Path(args.repo_dir).resolve()
    logging.getLogger("fabric_cicd").setLevel(logging.WARNING)

    forbidden = source_ids(repo, args.source_workspace_id)
    print(f"Dry run: {len(forbidden)} source ID(s) must not survive; environments: {' '.join(args.environments)}")
    results = {env: dry_run(repo, env.upper(), forbidden) for env in args.environments}
    for env, problems in results.items():
        for prob in problems:
            print(f"##vso[task.logissue type=error]Dry run {env}: {prob}")
    if args.junit:
        write_junit(args.junit, results)
    failed = [e for e, prob in results.items() if prob]
    print(f"Dry run: {len(results) - len(failed)}/{len(results)} environment(s) passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
