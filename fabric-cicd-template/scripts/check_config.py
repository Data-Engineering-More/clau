"""Check the CD pipeline's own settings before anything is deployed (Build stage, CD runs).

Catches the mistakes that would otherwise deploy to the wrong place:
  - a workspace ID left as a placeholder or not a GUID
  - two environments pointing at the SAME workspace (e.g. TEST copied into PROD)
  - an approval gate switched on with no approvers set
  - PROD switched on while TEST is off (PROD always deploys after TEST)
Only environments passed with --enabled are checked; the others may still be placeholders.

Usage
  python check_config.py --workspace DEV=<guid> --workspace TEST=<guid> --workspace PROD=<guid> \
                         --enabled DEV --enabled TEST \
                         --approvers TEST="a@x.com" --approvers PROD="[Project]\\Release Approvers"
"""
import argparse
import re
import sys

GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
ORDER = ["DEV", "TEST", "PROD"]


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
    p.add_argument("--enabled", action="append", help="environment that will be deployed (default: all given)")
    p.add_argument("--approvers", action="append", help="ENV=<emails or groups> for environments with a gate")
    args = p.parse_args()

    errors = []
    workspaces = pairs(args.workspace, "--workspace")
    enabled = [e.strip().upper() for e in args.enabled] if args.enabled else list(workspaces)
    disabled = [e for e in workspaces if e not in enabled]

    for env in enabled:
        if env not in workspaces:
            errors.append(f"{env} is enabled but has no workspace ID")
    if "PROD" in enabled and "TEST" not in enabled:
        errors.append("PROD is enabled but TEST is not - set enableTest: 'true' (PROD always deploys after TEST)")

    live = {env: workspaces[env] for env in enabled if env in workspaces}
    for env, ws in live.items():
        if "<" in ws or not GUID.fullmatch(ws):
            errors.append(f"{env} workspace ID '{ws}' is not set to a real GUID")
    seen: dict[str, str] = {}
    for env, ws in live.items():
        if GUID.fullmatch(ws):
            if ws.lower() in seen:
                errors.append(f"{env} and {seen[ws.lower()]} use the same workspace ID {ws}")
            seen.setdefault(ws.lower(), env)
    for env, who in pairs(args.approvers, "--approvers").items():
        if env in enabled and (not who or "<" in who):
            errors.append(f"{env} approval gate is on but approvers are not set ('{who}')")

    for e in errors:
        print(f"##vso[task.logissue type=error]Config: {e}")
    order = sorted(enabled, key=lambda e: ORDER.index(e) if e in ORDER else 99)
    print(f"Environments deployed by this run: {' -> '.join(order) or 'none'}")
    if disabled:
        print(f"Switched off (not checked, not deployed): {', '.join(disabled)}")
    print(f"Config check: {len(errors)} problem(s).")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
