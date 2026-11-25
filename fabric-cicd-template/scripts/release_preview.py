"""Release preview: what this run will deploy, compared with what is already in PROD.

Shown on the run's Summary tab, so approvers can check it before approving TEST and PROD.

  CD run (main) : compares HEAD with the latest release/* tag (= what PROD has now).
                  First release (no tag yet): every item is listed as new.
  PR run        : compares the PR merge commit with its target branch (= what this PR changes).

Lists, per Fabric item: Added / Modified / Deleted (a rename shows as deleted + added);
changes to parameter.yml;
commits and linked work items (#123). Warns about deletions and commits with no work item.

Needs the full git history and tags: checkout with fetchDepth: 0 and fetchTags: true.

Usage
  python release_preview.py --fabric-folder workspace --mode release|pr [--output preview.md]
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pipeline_log as log  # noqa: E402

ITEM_SUFFIX = re.compile(r"\.(Lakehouse|Notebook|DataPipeline|SemanticModel|Report|Warehouse|Environment|"
                         r"VariableLibrary|Dataflow|CopyJob|KQLDatabase|Eventhouse|Eventstream|SQLDatabase)$")


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else ""


def item_of(path: str, folder: str) -> str | None:
    parts = Path(path).parts
    prefix = Path(folder).parts
    if tuple(parts[:len(prefix)]) != prefix:
        return None
    for part in parts[len(prefix):]:
        if ITEM_SUFFIX.search(part):
            return part
    return "parameter.yml" if parts[-1] == "parameter.yml" else None


def collect(base: str | None, head: str, folder: str):
    if base:
        diff = git("diff", "--name-status", "--find-renames", base, head, "--", folder)
        log_range = [f"{base}..{head}"]
    else:
        diff = "\n".join(f"A\t{p}" for p in git("ls-tree", "-r", "--name-only", head, "--", folder).splitlines())
        log_range = ["-n", "30", head]
    items: dict[str, set[str]] = {}
    for line in filter(None, diff.splitlines()):
        fields = line.split("\t")
        code, paths = fields[0][0], fields[1:]
        for path in paths:
            item = item_of(path, folder)
            if item:
                items.setdefault(item, set()).add(code)
        if code == "R" and len(paths) == 2:
            old, new = item_of(paths[0], folder), item_of(paths[1], folder)
            if old and new and old != new:
                items[old] = {"D"}
                items[new] = {"A"}
    changes = {}
    for item, codes in items.items():
        if codes == {"A"}:
            changes[item] = "Added"
        elif codes == {"D"}:
            changes[item] = "Deleted"
        else:
            changes[item] = "Modified"
    sep = "\x1f"
    raw = git("log", "--no-merges", f"--format=%h{sep}%an{sep}%ad{sep}%s{sep}%B\x1e", "--date=short", *log_range)
    commits = []
    for entry in filter(None, (e.strip() for e in raw.split("\x1e"))):
        h, author, date, subject, body = (entry.split(sep) + [""] * 5)[:5]
        work_items = sorted(set(re.findall(r"(?<![\w&])#(\d+)\b", body)), key=int)
        commits.append((h, date, author, subject, work_items))
    return changes, commits


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fabric-folder", default="workspace")
    p.add_argument("--mode", choices=["release", "pr"], default="release")
    p.add_argument("--tag-pattern", default="release/*")
    p.add_argument("--output", default="release-preview.md")
    args = p.parse_args()

    log.section("Release preview")
    if git("rev-parse", "--is-shallow-repository", check=False) == "true":
        log.warning("Shallow clone - the preview may be incomplete. Use 'checkout: self' with fetchDepth: 0 and fetchTags: true.")

    head = "HEAD"
    if args.mode == "pr":
        parents = git("rev-list", "--parents", "-n", "1", "HEAD").split()
        if len(parents) >= 3:
            base, head, title = parents[1], parents[2], "Changes in this pull request"
        else:
            base, title = "HEAD~1", "Changes in this pull request"
            log.warning("HEAD is not a PR merge commit; comparing with the previous commit.")
        baseline = "target branch"
    else:
        base = git("describe", "--tags", "--abbrev=0", f"--match={args.tag_pattern}", "HEAD", check=False) or None
        title = "Release preview: changes since the last PROD release"
        baseline = f"tag `{base}`" if base else "nothing (first release - everything is new)"

    changes, commits = collect(base, head, args.fabric_folder.strip("/"))
    order = {"Deleted": 0, "Added": 1, "Modified": 2}
    icons = {"Added": "➕", "Modified": "✏️", "Deleted": "🗑️"}
    rows = [[icons[change], item, change]
            for item, change in sorted(changes.items(), key=lambda kv: (order[kv[1]], kv[0]))]
    no_wi = [c for c in commits if not c[4]]
    deleted = [r[1] for r in rows if r[2] == "Deleted"]

    md = [f"## {title}", "", f"- **Compared with:** {baseline}",
          f"- **Fabric items changed:** {len(changes)} ({sum(r[2] == 'Added' for r in rows)} added, "
          f"{sum(r[2] == 'Modified' for r in rows)} modified, {len(deleted)} deleted)",
          f"- **Commits:** {len(commits)}", ""]
    if deleted:
        md += [f"> ⚠️ **{len(deleted)} item(s) will be deleted from the repo.** They stay in the target workspace "
               "unless the run uses *Remove items deleted from the repo*.", ""]
    md += ["### Fabric items", "", log.md_table(["", "Item", "Change"], rows) if rows else "_No Fabric item changes._", ""]
    md += ["### Commits", "", log.md_table(["Commit", "Date", "Author", "Message", "Work items"],
                                           [[c[0], c[1], c[2], c[3], ", ".join("#" + w for w in c[4]) or "—"] for c in commits])
           if commits else "_No commits._", ""]
    Path(args.output).write_text("\n".join(md), encoding="utf-8")

    log.info(f"Compared with: {baseline}")
    if rows:
        log.print_table(["", "Item", "Change"], rows)
    else:
        log.info("No Fabric item changes.")
    log.info(f"{len(commits)} commit(s).")
    for item in deleted:
        log.warning(f"Release preview: '{item}' is deleted in this release.")
    if no_wi:
        log.warning(f"Release preview: {len(no_wi)} commit(s) have no linked work item (#id): "
                    + ", ".join(c[0] for c in no_wi[:10]))
    log.upload_summary(args.output)


if __name__ == "__main__":
    main()
