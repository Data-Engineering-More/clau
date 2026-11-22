"""Static checks on the Fabric items in the repo. Runs in the Build stage (PR and CD).

Nothing here calls Fabric. Errors fail the build; warnings are reported but don't.

Checks
  items       every item folder has a valid .platform; no "/" in names;
              no duplicate names (per type) or duplicate logicalIds (copied folders)
  json        every .json file in the Fabric folder parses
  parameters  parameter.yml exists and parses; every rule has a value for each
              environment; regexes compile with exactly one capture group;
              no <PLACEHOLDER> left
  references  Data Pipeline activities point at notebooks/pipelines that are in the repo
  guids       GUIDs in code that parameter.yml does not map (warning: they would
              still point at the source environment after deployment)
  secrets     keys, passwords, SAS tokens or private keys committed in item files
  notebooks   Python cells in notebook-content.py compile (warning; magics skipped)

Usage
  python validate.py --repo-dir workspace [--junit results/validate.xml]
"""
import argparse
import json
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

# >>> CHANGE (optional): must match the environment keys in parameter.yml.
ENVIRONMENTS = ("DEV", "TEST", "PROD")

GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
CONTENT_FILES = ("notebook-content.py", "notebook-content.ipynb", "pipeline-content.json")
ITEM_SUFFIX = re.compile(
    r"\.(Lakehouse|Notebook|DataPipeline|SemanticModel|Report|Warehouse|Environment|"
    r"VariableLibrary|Dataflow|CopyJob|KQLDatabase|Eventhouse|Eventstream|SQLDatabase)$"
)
SECRET_PATTERNS = {
    "storage account key": re.compile(r"AccountKey\s*=\s*[A-Za-z0-9+/=]{20,}"),
    "SAS token": re.compile(r"[?&]sig=[A-Za-z0-9%+/=]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "hard-coded password": re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*[\"'][^\"'\s]{6,}[\"']"),
    "hard-coded client secret": re.compile(r"(?i)\bclient_?secret\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
}
CELL_MARKER = re.compile(r"^# (CELL|PARAMETERS CELL|MARKDOWN|METADATA) \*+\s*$")


class Results:
    def __init__(self) -> None:
        self.checks: dict[str, dict[str, list[str]]] = {}

    def _bucket(self, check: str) -> dict[str, list[str]]:
        return self.checks.setdefault(check, {"errors": [], "warnings": []})

    def start(self, check: str) -> None:
        self._bucket(check)

    def error(self, check: str, msg: str) -> None:
        self._bucket(check)["errors"].append(msg)

    def warn(self, check: str, msg: str) -> None:
        self._bucket(check)["warnings"].append(msg)

    @property
    def error_count(self) -> int:
        return sum(len(c["errors"]) for c in self.checks.values())

    def report(self) -> None:
        for check, res in self.checks.items():
            for w in res["warnings"]:
                print(f"##vso[task.logissue type=warning][{check}] {w}")
            for e in res["errors"]:
                print(f"##vso[task.logissue type=error][{check}] {e}")
            status = "FAIL" if res["errors"] else ("WARN" if res["warnings"] else "PASS")
            print(f"  {status:4}  {check:11} {len(res['errors'])} error(s), {len(res['warnings'])} warning(s)")
        warnings = sum(len(c["warnings"]) for c in self.checks.values())
        print(f"Validation: {self.error_count} error(s), {warnings} warning(s).")

    def write_junit(self, path: str) -> None:
        cases = []
        for check, res in self.checks.items():
            body = ""
            if res["errors"]:
                body += f'<failure message="{len(res["errors"])} error(s)">{escape(chr(10).join(res["errors"]))}</failure>'
            if res["warnings"]:
                body += f"<system-out>{escape(chr(10).join('WARNING: ' + w for w in res['warnings']))}</system-out>"
            cases.append(f'<testcase classname="validate" name="{check}">{body}</testcase>')
        failures = sum(1 for r in self.checks.values() if r["errors"])
        xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="Fabric item validation" '
               f'tests="{len(cases)}" failures="{failures}">{"".join(cases)}</testsuite>\n')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(xml, encoding="utf-8")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def check_items(repo: Path, r: Results) -> set[str]:
    r.start("items")
    logical_ids: dict[str, str] = {}
    names: dict[tuple[str, str], str] = {}
    folders = [d for d in repo.rglob("*") if d.is_dir() and ITEM_SUFFIX.search(d.name)]
    if not folders:
        r.error("items", f"No Fabric item folders found in '{repo}'. Is the Git folder set correctly?")
    for folder in folders:
        rel = str(folder.relative_to(repo))
        platform = folder / ".platform"
        if not platform.exists():
            r.error("items", f"{rel}: missing .platform")
            continue
        try:
            data = json.loads(read(platform))
            item_type = data["metadata"]["type"]
            name = data["metadata"]["displayName"]
            logical = data["config"]["logicalId"].lower()
        except (json.JSONDecodeError, KeyError) as exc:
            r.error("items", f"{rel}: invalid .platform ({exc})")
            continue
        if "/" in name or "\\" in name:
            r.error("items", f"{rel}: display name '{name}' contains a slash; rename the item")
        if (item_type, name) in names:
            r.error("items", f"{rel}: duplicate {item_type} name '{name}' (also {names[(item_type, name)]})")
        names[(item_type, name)] = rel
        if logical in logical_ids:
            r.error("items", f"{rel}: duplicate logicalId {logical} (also {logical_ids[logical]}). "
                             "A copied item folder? Create copies in Fabric, not by copying folders.")
        logical_ids[logical] = rel
    return set(logical_ids)


def check_json(repo: Path, r: Results) -> None:
    r.start("json")
    for path in repo.rglob("*.json"):
        try:
            json.loads(read(path))
        except json.JSONDecodeError as exc:
            r.error("json", f"{path.relative_to(repo)}: invalid JSON ({exc})")


def check_parameters(repo: Path, r: Results) -> tuple[set[str], list[re.Pattern]]:
    r.start("parameters")
    mapped: set[str] = set()
    patterns: list[re.Pattern] = []
    param_file = repo / "parameter.yml"
    if not param_file.exists():
        r.error("parameters", "parameter.yml not found in the Fabric folder")
        return mapped, patterns
    text = read(param_file)
    active = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    for placeholder in sorted(set(re.findall(r"<[A-Z_]+>", active))):
        r.error("parameters", f"placeholder {placeholder} not filled in")
    try:
        params = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        r.error("parameters", f"not valid YAML: {exc}")
        return mapped, patterns
    for section in ("find_replace", "key_value_replace"):
        for i, rule in enumerate(params.get(section) or []):
            label = f"{section}[{i}]"
            values = rule.get("replace_value") or {}
            for env in ENVIRONMENTS:
                if env not in values:
                    r.error("parameters", f"{label}: no {env} value")
            find = str(rule.get("find_value", ""))
            if str(rule.get("is_regex", "")).lower() == "true":
                try:
                    pattern = re.compile(find)
                except re.error as exc:
                    r.error("parameters", f"{label}: invalid regex ({exc})")
                    continue
                if pattern.groups != 1:
                    r.error("parameters", f"{label}: regex must have exactly one capture group (fabric-cicd rule)")
                patterns.append(pattern)
            else:
                mapped.update(g.lower() for g in GUID.findall(find))
    return mapped, patterns


def check_references(repo: Path, logical_ids: set[str], mapped: set[str], r: Results) -> None:
    r.start("references")

    def walk(node, path: Path):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("notebookId", "pipelineId") and isinstance(value, str):
                    v = value.lower()
                    if GUID.fullmatch(v) and v != ZERO_GUID and v not in logical_ids and v not in mapped:
                        r.error("references", f"{path.relative_to(repo)}: {key} {value} is not an item in this repo "
                                              "(deleted or renamed item, or one from another workspace)")
                walk(value, path)
        elif isinstance(node, list):
            for value in node:
                walk(value, path)

    for path in repo.rglob("pipeline-content.json"):
        try:
            walk(json.loads(read(path)), path)
        except json.JSONDecodeError:
            pass  # reported by the json check


def check_guids(repo: Path, logical_ids: set[str], mapped: set[str], patterns: list[re.Pattern], r: Results) -> None:
    r.start("guids")
    for path in repo.rglob("*"):
        if path.name not in CONTENT_FILES:
            continue
        text = read(path)
        for pattern in patterns:
            text = pattern.sub("", text)
        for guid in sorted(set(g.lower() for g in GUID.findall(text))):
            if guid != ZERO_GUID and guid not in logical_ids and guid not in mapped:
                r.warn("guids", f"{path.relative_to(repo)}: GUID {guid} is not mapped in parameter.yml")


def check_secrets(repo: Path, r: Results) -> None:
    r.start("secrets")
    for path in repo.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".json", ".ipynb", ".yml", ".yaml", ".sql", ".scala", ".r"):
            continue
        for lineno, line in enumerate(read(path).splitlines(), 1):
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    r.error("secrets", f"{path.relative_to(repo)}:{lineno}: possible {label}. "
                                       "Use Key Vault or a Fabric connection instead.")


def check_notebooks(repo: Path, r: Results) -> None:
    r.start("notebooks")
    for path in repo.rglob("notebook-content.py"):
        blocks: list[tuple[str, list[str]]] = []
        kind, lines = "HEADER", []
        for line in read(path).splitlines():
            m = CELL_MARKER.match(line)
            if m:
                blocks.append((kind, lines))
                kind, lines = m.group(1), []
            else:
                lines.append(line)
        blocks.append((kind, lines))
        cell_no = 0
        for i, (kind, lines) in enumerate(blocks):
            if kind not in ("CELL", "PARAMETERS CELL"):
                continue
            cell_no += 1
            meta = blocks[i + 1][1] if i + 1 < len(blocks) and blocks[i + 1][0] == "METADATA" else []
            lang = re.search(r'"language":\s*"([^"]+)"', "\n".join(meta))
            if lang and lang.group(1).lower() != "python":
                continue
            code = "\n".join("" if l.lstrip().startswith(("%", "!")) else l for l in lines)
            try:
                compile(code, f"{path.parent.name} cell {cell_no}", "exec")
            except SyntaxError as exc:
                r.warn("notebooks", f"{path.parent.name}: cell {cell_no} line {exc.lineno}: {exc.msg}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo-dir", required=True, help="Fabric Git folder")
    p.add_argument("--junit", help="write JUnit XML here (shown in the Azure DevOps Tests tab)")
    args = p.parse_args()
    repo = Path(args.repo_dir)
    if not repo.is_dir():
        sys.exit(f"'{repo}' does not exist")

    r = Results()
    logical_ids = check_items(repo, r)
    check_json(repo, r)
    mapped, patterns = check_parameters(repo, r)
    check_references(repo, logical_ids, mapped, r)
    check_guids(repo, logical_ids, mapped, patterns, r)
    check_secrets(repo, r)
    check_notebooks(repo, r)

    r.report()
    if args.junit:
        r.write_junit(args.junit)
    sys.exit(1 if r.error_count else 0)


if __name__ == "__main__":
    main()
