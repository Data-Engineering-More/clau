"""Check the CD pipeline's own settings before anything is deployed (Build stage, CD runs).

Catches the mistakes that would otherwise deploy to the wrong place:
  - a workspace ID left as a placeholder or not a GUID
  - two environments pointing at the SAME workspace (e.g. TEST copied into PROD)
  - an approval gate switched on with no approvers set

Usage
  python check_config.py --workspace DEV=<guid> --workspace TEST=<guid> --workspace PROD=<guid> \
                         --approvers TEST="a@x.com" --approvers PROD="[Project]\\Release Approvers"
"""
import argparse
import re
import sys

GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def pairs(values: list[str], flag: str) -> dict[str, str]:
    out = {}
    for v in values or []:
        if "=" not in v:
            sys.exit(f"{flag} expects ENV=value, got '{v}'")
        env, val = v.split("=", 1)
        out[env.strip().upper()] = val.strip()
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", action="append", required=True, help="ENV=<workspace guid>")
    p.add_argument("--approvers", action="append", help="ENV=<emails or groups> for environments with a gate")
    args = p.parse_args()

    errors = []
    workspaces = pairs(args.workspace, "--workspace")
    for env, ws in workspaces.items():
        if "<" in ws or not GUID.fullmatch(ws):
            errors.append(f"{env} workspace ID '{ws}' is not set to a real GUID")
    seen: dict[str, str] = {}
    for env, ws in workspaces.items():
        key = ws.lower()
        if GUID.fullmatch(ws) and key in seen:
            errors.append(f"{env} and {seen[key]} use the same workspace ID {ws}")
        seen.setdefault(key, env)
    for env, who in pairs(args.approvers, "--approvers").items():
        if not who or "<" in who:
            errors.append(f"{env} approval gate is on but approvers are not set ('{who}')")

    for e in errors:
        print(f"##vso[task.logissue type=error]Config: {e}")
    print(f"Config check: {len(workspaces)} environment(s), {len(errors)} problem(s).")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
