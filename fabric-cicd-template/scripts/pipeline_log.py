"""Logging helpers that render nicely in the Azure DevOps run log.

Outside Azure DevOps the same calls print plain timestamped lines.
  section()  -> green section header
  group()    -> collapsible block (close with endgroup())
  error()/warning() -> annotations shown on the run summary
  upload_summary()  -> markdown file shown on the run's Summary tab
  add_build_tag()   -> tag on the run, filterable in the runs list
"""
import os
import time
from datetime import datetime, timezone

IN_PIPELINE = os.environ.get("TF_BUILD", "").lower() == "true"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")


def info(msg: str) -> None:
    print(f"{_ts()}  {msg}", flush=True)


def section(msg: str) -> None:
    print(f"##[section]{_ts()}  {msg}" if IN_PIPELINE else f"\n=== {_ts()}  {msg} ===", flush=True)


def group(title: str) -> None:
    print(f"##[group]{title}" if IN_PIPELINE else f"--- {title} ---", flush=True)


def endgroup() -> None:
    print("##[endgroup]" if IN_PIPELINE else "--- end ---", flush=True)


def warning(msg: str) -> None:
    print(f"##vso[task.logissue type=warning]{msg}", flush=True)


def error(msg: str) -> None:
    print(f"##vso[task.logissue type=error]{msg}", flush=True)


def upload_summary(path: str) -> None:
    if IN_PIPELINE:
        print(f"##vso[task.uploadsummary]{os.path.abspath(path)}", flush=True)


def add_build_tag(tag: str) -> None:
    if IN_PIPELINE:
        print(f"##vso[build.addbuildtag]{tag}", flush=True)


class Timer:
    def __init__(self) -> None:
        self.start = time.time()

    def elapsed(self) -> str:
        secs = int(time.time() - self.start)
        return f"{secs // 60}m {secs % 60:02d}s"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    def cell(v) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers), flush=True)
    print(fmt.format(*("-" * w for w in widths)), flush=True)
    for r in rows:
        print(fmt.format(*(str(c) for c in r)), flush=True)
