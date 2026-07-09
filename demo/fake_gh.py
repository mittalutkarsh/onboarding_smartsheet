#!/usr/bin/env python3
"""A fake `gh` CLI for the self-contained demo (NOT for production).

Emulates just the three subcommands the automation uses, backed by a JSON state
file at ``$DEMO_GH_STATE``:

    gh pr list  --repo R --head B --state all --json number,url,state
    gh pr create --repo R --base X --head B --title T --body Y   (prints PR URL)
    gh pr view  <number> --repo R --json number,url,state,mergeable

State shape: {"counter": int, "branches": {<head>: {number,url,state,mergeable}}}
"""
import json
import os
import sys

STATE = os.environ["DEMO_GH_STATE"]


def load():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"counter": 100, "branches": {}}


def save(d):
    with open(STATE, "w") as f:
        json.dump(d, f)


def argval(args, name):
    return args[args.index(name) + 1] if name in args else None


def main():
    a = sys.argv[1:]
    if a[:2] == ["pr", "list"]:
        head = argval(a, "--head")
        pr = load()["branches"].get(head)
        out = [{"number": pr["number"], "url": pr["url"], "state": pr["state"]}] if pr else []
        print(json.dumps(out))
        return 0
    if a[:2] == ["pr", "create"]:
        head, base, repo = argval(a, "--head"), argval(a, "--base"), argval(a, "--repo")
        d = load()
        if head in d["branches"]:  # idempotent: reuse
            print(d["branches"][head]["url"])
            return 0
        d["counter"] += 1
        num = d["counter"]
        url = f"https://github.com/{repo}/pull/{num}"
        d["branches"][head] = {
            "number": num, "url": url, "state": "OPEN",
            "mergeable": "MERGEABLE", "base": base, "repo": repo,
        }
        save(d)
        print(url)
        return 0
    if a[:2] == ["pr", "view"]:
        num = int(a[2])
        for pr in load()["branches"].values():
            if pr["number"] == num:
                print(json.dumps({
                    "number": num, "url": pr["url"],
                    "state": pr["state"], "mergeable": pr["mergeable"],
                }))
                return 0
        sys.stderr.write(f"fake_gh: PR #{num} not found\n")
        return 1
    sys.stderr.write(f"fake_gh: unsupported args {a}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
