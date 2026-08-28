#!/usr/bin/env python3
"""
Config validator + fixer.

A spec (config-spec/<name>.spec.json) declares the EXPECTED value and format
for each key. This tool checks each environment's JSON file against the spec
and reports deviations, and can optionally fix them.

Usage:
    python3 validate.py check                    # verify files match the spec (the test)
    python3 validate.py preview                  # show what would change (writes NOTHING)
    python3 validate.py apply                    # make the changes (undo via git if needed)

    # each command takes an optional spec path and/or a single environment:
    python3 validate.py check   config-spec/player.spec.json
    python3 validate.py preview --env test

With no command it defaults to `check` (the safe, read-only one). Default spec is engagement.
`check` exit code: 0 if no hard FAILs, 1 otherwise (usable as a CI / pre-commit gate).
`apply` preserves each value's existing JSON type (a string stays a string, a
number stays a number) and only changes keys that have an `expected` value.
("apply" also accepts the alias "write".)
"""
import json
import sys
import os
import glob

ROOT = os.path.dirname(os.path.abspath(__file__))

MISSING = object()  # sentinel: key absent from file
DELETE = object()   # sentinel: a planned change that removes a key


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_path(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def set_path(obj, dotted, value):
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def del_path(obj, dotted):
    """Remove a dotted key, then prune any parent dicts left empty by the removal."""
    parts = dotted.split(".")
    chain, cur = [], obj
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return False
        chain.append((cur, part)); cur = cur[part]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        return False
    del cur[parts[-1]]
    for parent, part in reversed(chain):          # drop now-empty parents
        if isinstance(parent[part], dict) and not parent[part]:
            del parent[part]
        else:
            break
    return True


def all_leaf_paths(obj, prefix=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.extend(all_leaf_paths(v, p))
            else:
                out.append(p)
    return out


def normalize(v):
    """Coerce a value into (kind, value) for robust comparison."""
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, (int, float)):
        return ("num", float(v))
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in ("true", "false"):
            return ("bool", s.lower() == "true")
        try:
            return ("num", float(s))
        except ValueError:
            return ("str", s)
    return ("other", v)


def values_match(expected, actual):
    ne, na = normalize(expected), normalize(actual)
    if ne[0] != na[0]:
        return False
    return ne[1] == na[1]


def strict_match(expected, actual):
    """Exact JSON equality — no coercion at all, so `"30"` != `30`, `true` != `1`,
    `" "` != `""`. Used by `audit` so any deviation from the spec is highlighted."""
    return json.dumps(expected, sort_keys=True) == json.dumps(actual, sort_keys=True)


def coerce_to_type_of(new_value, old_value):
    """Express new_value using old_value's JSON type, so we change the value
    without silently changing the type the app reads."""
    if isinstance(old_value, bool):
        return bool(normalize(new_value)[1])
    if isinstance(old_value, str):
        return str(new_value)
    if isinstance(old_value, (int, float)):
        kind, val = normalize(new_value)
        if kind == "num":
            return int(val) if float(val).is_integer() else val
    return new_value  # MISSING or unknown -> use expected as-is


def fmt(v):
    if v is MISSING:
        return "<absent>"
    if isinstance(v, str):
        return repr(v)
    return json.dumps(v)


# ---------------------------------------------------------------- validate ---

def target_for(env, rule):
    """The expected value for this env: per-env if defined, else the shared `expected`."""
    if "expectedPerEnv" in rule:
        return rule["expectedPerEnv"].get(env, MISSING)  # MISSING => not defined for this env
    return rule.get("expected")


def allowed_envs(rule):
    """The set of envs an `onlyIn` key may exist in (str or list); None if not an onlyIn key."""
    if "onlyIn" not in rule:
        return None
    v = rule["onlyIn"]
    return {v} if isinstance(v, str) else set(v)


def compare_keys(env, data, spec, strict=False):
    """Compare a config dict against the spec's expected values for `env`.
    strict=True uses exact JSON equality (no coercion) — used by `audit`."""
    matcher = strict_match if strict else values_match
    rows = []
    counts = {"PASS": 0, "FAIL": 0, "MISS": 0, "TODO": 0, "FORB": 0}

    for key, rule in spec["keys"].items():
        actual = get_path(data, key)
        if rule.get("needsSpec"):
            rows.append(("TODO", key, "(undefined)", actual)); counts["TODO"] += 1
            continue
        allowed = allowed_envs(rule)
        if allowed is not None and env not in allowed:   # forbidden in this env
            if actual is MISSING:
                rows.append(("PASS", key, "(absent here)", actual)); counts["PASS"] += 1
            else:
                rows.append(("FORB", key, "(must not exist here)", actual)); counts["FORB"] += 1
            continue
        target = target_for(env, rule)
        if "expectedPerEnv" in rule and target is MISSING:
            rows.append(("TODO", key, f"(no value for {env})", actual)); counts["TODO"] += 1
        elif actual is MISSING:
            rows.append(("MISS", key, target, actual)); counts["MISS"] += 1
        elif matcher(target, actual):
            rows.append(("PASS", key, target, actual)); counts["PASS"] += 1
        else:
            rows.append(("FAIL", key, target, actual)); counts["FAIL"] += 1

    spec_keys = set(spec["keys"].keys())
    # a leaf is "specified" if it IS a spec key or sits under one (whole-object pins)
    covered = lambda p: any(p == k or p.startswith(k + ".") for k in spec_keys)
    unspecified = [p for p in all_leaf_paths(data) if not covered(p)]
    return rows, counts, unspecified


def validate_env(env, file_rel, spec):
    return compare_keys(env, load_json(os.path.join(ROOT, file_rel)), spec)


def run_validate(spec, spec_path, only_env):
    print(f"\n==================== CONFIG: {spec['config']} ====================")
    print(f"spec: {spec_path}")
    label = {"PASS": "PASS ", "FAIL": "FAIL ", "MISS": "MISS ", "TODO": "TODO ", "FORB": "FORB "}
    total_fail = 0
    for env, file_rel in spec["files"].items():
        if only_env and env != only_env:
            continue
        rows, counts, unspecified = validate_env(env, file_rel, spec)
        total_fail += counts["FAIL"] + counts["MISS"] + counts["FORB"]
        print(f"\n--- {env.upper()}  ({file_rel}) ---")
        for status, key, expected, actual in rows:
            if status == "PASS":   # passes are fine — only surface problems
                continue
            if status == "FORB":
                print(f"  FORB  {key:42} forbidden key present — must not exist in {env}; "
                      f"actual={fmt(actual)}")
                continue
            exp = "" if expected in (None, "(undefined)") else f"expected={fmt(expected)}"
            print(f"  {label[status]} {key:42} {exp:24} actual={fmt(actual)}".rstrip())
        if unspecified:
            print(f"  ?????  keys in file with no spec entry: {', '.join(unspecified)}")
        parts = [f"{counts['PASS']} pass"]
        for code, lbl in (("FAIL", "FAIL"), ("MISS", "missing"), ("FORB", "forbidden"), ("TODO", "todo")):
            if counts[code]:
                parts.append(f"{counts[code]} {lbl}")
        print("  -> " + ", ".join(parts))

    print(f"\n==================== RESULT: "
          f"{'PASS (no hard failures)' if total_fail == 0 else f'{total_fail} HARD FAILURE(S)'} "
          f"====================\n")
    return 1 if total_fail else 0


# --------------------------------------------------------------------- fix ---

def plan_fixes(env, file_rel, spec):
    data = load_json(os.path.join(ROOT, file_rel))
    changes = []  # (key, old, new_typed)
    skipped = []
    for key, rule in spec["keys"].items():
        if rule.get("needsSpec"):
            skipped.append(key); continue
        allowed = allowed_envs(rule)
        if allowed is not None and env not in allowed:   # forbidden here -> remove if present
            actual = get_path(data, key)
            if actual is not MISSING:
                changes.append((key, actual, DELETE))
            continue
        if "expectedPerEnv" in rule:
            if env not in rule["expectedPerEnv"]:
                skipped.append(key); continue
            target = rule["expectedPerEnv"][env]
        elif "expected" in rule:
            target = rule["expected"]
        else:
            skipped.append(key)
            continue
        actual = get_path(data, key)
        if actual is not MISSING and values_match(target, actual):
            continue
        changes.append((key, actual, coerce_to_type_of(target, actual)))
    return data, changes, skipped


def run_fix(spec, only_env, write):
    verb = "APPLY" if write else "PREVIEW"
    print(f"\n==================== {verb} FIXES: {spec['config']} ====================")
    if not write:
        print("(dry-run — no files are modified; run the `apply` command to make these changes)")
    total_changes = 0
    envs_changed = 0
    for env, file_rel in spec["files"].items():
        if only_env and env != only_env:
            continue
        data, changes, skipped = plan_fixes(env, file_rel, spec)
        print(f"\n--- {env.upper()}  ({file_rel}) ---")
        if not changes:
            print("  already matches spec — nothing to change")
        for key, old, new in changes:
            if new is DELETE:
                print(f"  DEL  {key:42} {fmt(old)} -> <removed>")
            else:
                print(f"  SET  {key:42} {fmt(old)} -> {fmt(new)}")
        if skipped:
            print(f"  (skipped {len(skipped)} keys with no expected value: {', '.join(skipped)})")
        if changes:
            total_changes += len(changes)
            envs_changed += 1
            if write:
                path = os.path.join(ROOT, file_rel)
                for key, _old, new in changes:
                    if new is DELETE:
                        del_path(data, key)
                    else:
                        set_path(data, key, new)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                print(f"  -> wrote {len(changes)} change(s)  (undo with git if needed)")
    print()
    if total_changes == 0:
        print(f"==================== NOTHING TO {verb} — everything already matches the spec "
              f"====================\n")
    elif write:
        print(f"==================== APPLIED {total_changes} change(s) across {envs_changed} "
              f"env(s) — now run `check` to verify ====================\n")
    else:
        print(f"==================== {total_changes} change(s) across {envs_changed} env(s) — "
              f"run `apply` to make them ====================\n")
    return 0


# -------------------------------------------------------------------- main ---

def run_audit(spec, response, env):
    """Compare one config section of an API response against its spec (read-only)."""
    api_key = spec.get("apiKey", spec["config"])
    print(f"\n==================== AUDIT: {spec['config']}  "
          f"(env={env}, response section '{api_key}') ====================")
    if not isinstance(response.get(api_key), dict):
        print(f"  !! section '{api_key}' is missing from the response")
        return 1
    rows, counts, unspecified = compare_keys(env, response[api_key], spec, strict=True)
    label = {"FAIL": "DIFF ", "MISS": "MISS ", "TODO": "TODO "}
    for status, key, expected, actual in rows:
        if status == "PASS":
            continue
        if status == "FORB":
            print(f"  FORB  {key:42} forbidden key present in {env} response; "
                  f"response={fmt(actual)}")
            continue
        exp = "" if expected in (None, "(undefined)") else f"expected={fmt(expected)}"
        print(f"  {label[status]} {key:42} {exp:24} response={fmt(actual)}".rstrip())
    if unspecified:
        print(f"  ?????  in response but not in spec: {', '.join(unspecified)}")
    parts = [f"{counts['PASS']} match"]
    for code, lbl in (("FAIL", "differ"), ("MISS", "missing-in-response"),
                      ("FORB", "forbidden"), ("TODO", "todo")):
        if counts[code]:
            parts.append(f"{counts[code]} {lbl}")
    print("  -> " + ", ".join(parts))
    return 1 if (counts["FAIL"] or counts["MISS"] or counts["FORB"]) else 0


def resolve_specs(names, available):
    """Map config names / paths to spec paths; empty -> all available."""
    if not names:
        return list(available)
    out = []
    for a in names:
        if a.endswith(".json") or os.sep in a:
            out.append(a)                                              # explicit path
        else:
            out.append(os.path.join("config-spec", a + ".spec.json"))  # short name
    return out


def find_response_or_exit():
    """No path given -> look in the api-responses/ drop folder. Use the only
    JSON there; if there are several, list them and ask which; if none, say so."""
    folder = "api-responses"
    found = sorted(os.path.basename(p)
                   for p in glob.glob(os.path.join(ROOT, folder, "*.json")))
    if len(found) == 1:
        return os.path.join(folder, found[0])
    if not found:
        print(f"No response file found. Drop the API response JSON in {folder}/ "
              f"and run `audit --env ENV` again.")
        sys.exit(2)
    print(f"Multiple files in {folder}/ — name which one to audit:")
    for f in found:
        print(f"  python3 validate.py audit {folder}/{f} --env ENV")
    sys.exit(2)


def valid_env_or_exit(only_env, specs):
    """If an --env was given, it must be one the specs actually define; otherwise
    per-env/onlyIn keys would silently misbehave (e.g. `--env Test` vs `test`)."""
    if not only_env:
        return
    valid = set().union(*[s["files"].keys() for s in specs]) if specs else set()
    if only_env not in valid:
        print(f"Error: unknown env '{only_env}'. Valid environments: {', '.join(sorted(valid))}")
        sys.exit(2)


def load_spec_or_exit(spec_path, available):
    full = os.path.join(ROOT, spec_path)
    if not os.path.isfile(full):
        names = ", ".join(os.path.basename(p)[:-len(".spec.json")] for p in available) or "(none)"
        print(f"Error: spec not found: {spec_path}")
        print(f"Available configs: {names}")
        print("Usage: python3 validate.py [check|preview|apply|audit] [config-name ...] [--env ENV]")
        sys.exit(2)
    return load_json(full)


def main():
    args = list(sys.argv[1:])
    cmd = "check"
    if args and args[0] in ("check", "preview", "apply", "write", "audit"):
        cmd = args.pop(0)
    only_env = None
    if "--env" in args:
        i = args.index("--env"); only_env = args[i + 1].strip().lower(); del args[i:i + 2]
    available = [os.path.relpath(p, ROOT)
                 for p in sorted(glob.glob(os.path.join(ROOT, "config-spec", "*.spec.json")))]

    if cmd == "audit":
        if not only_env:
            print("audit needs --env (which environment this response came from)")
            sys.exit(2)
        # An arg that looks like a file (has a slash or ends in .json) is the
        # response path; anything else is a config name. The path is optional —
        # if omitted, we look in the api-responses/ drop folder automatically.
        explicit = [a for a in args if "/" in a or a.endswith(".json")]
        args = [a for a in args if a not in explicit]
        if explicit:
            response_path = explicit[0]
        else:
            response_path = find_response_or_exit()
        rfull = os.path.join(ROOT, response_path)
        if not os.path.isfile(rfull):
            print(f"Error: response file not found: {response_path}")
            sys.exit(2)
        print(f"Auditing response: {response_path}\n")
        response = load_json(rfull)
        aspecs = [load_spec_or_exit(sp, available) for sp in resolve_specs(args, available)]
        valid_env_or_exit(only_env, aspecs)
        rc = 0
        for spec in aspecs:
            rc |= run_audit(spec, response, only_env)
        print(f"\n==================== AUDIT RESULT: "
              f"{'all sections match the specs' if rc == 0 else 'DIFFERENCES found (see above)'} "
              f"====================\n")
        sys.exit(rc)

    spec_paths = resolve_specs(args, available)
    if not spec_paths:
        print("No spec files found in config-spec/")
        sys.exit(2)
    specs = [(sp, load_spec_or_exit(sp, available)) for sp in spec_paths]
    valid_env_or_exit(only_env, [s for _, s in specs])
    rc = 0
    for spec_path, spec in specs:
        if cmd == "preview":
            rc |= run_fix(spec, only_env, write=False)
        elif cmd in ("apply", "write"):
            rc |= run_fix(spec, only_env, write=True)
        else:
            rc |= run_validate(spec, spec_path, only_env)
    sys.exit(rc)


if __name__ == "__main__":
    main()
