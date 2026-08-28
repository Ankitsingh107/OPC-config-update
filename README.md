# OPC Config Management

A tiny, dependency-free tool (`validate.py`, plain Python 3) that keeps our app
configuration **correct and in sync across the three environments** — `test`,
`stage`, and `prod`.

---

## The problem this solves

Our app is driven by configuration files. The same set of configs exists three
times — once per environment:

```
TEST ENV/         STAGE/                  PROD/
  engagement-test.json   engagement-stage.json   engagement-prod.json
  player-test.json       player-stage.json       player-prod.json
  learnActions-...        ...                      ...
```

For the most part these files should be **identical**. But two things make that
hard to keep true by hand:

1. **Drift from manual copying.** Change a value in test, then copy it to stage,
   then to prod — miss one step and the environments silently disagree.
2. **Some values are *supposed* to differ per environment.** e.g. the DRM /
   playback **license URL** points to a test server in `test` but the real Astro
   license server in `stage`/`prod`. So you *can't* just blindly make all three
   identical either.

This tool removes the guesswork: it knows what each value should be (including
the ones that legitimately differ per env), tells you when reality doesn't match,
and can fix it.

---

## The core idea: a spec is the source of truth

Instead of hand-editing the environment files, you describe what each config
*should* look like in a **spec** — one per config type, under `config-spec/`:

```
config-spec/engagement.spec.json
config-spec/player.spec.json
config-spec/learnActions.spec.json
```

```
                 config-spec/engagement.spec.json   ← you edit THIS (the truth)
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  TEST ENV/…            STAGE/…               PROD/…       ← kept in sync from it
```

You change the spec; the tool brings the three environment files into line with
it. You never edit the environment JSON by hand. (If someone does, the tool
catches it — that's the safety net.)

---

## Quick start

Everything runs through `validate.py`. The safe, read-only command is `check`:

```bash
python3 validate.py check          # check EVERY config in every environment
```

```
==================== CONFIG: engagement ====================
--- TEST  (TEST ENV/engagement-test.json) ---
  -> 18 pass
--- STAGE  (STAGE/engagement-stage.json) ---
  -> 18 pass
--- PROD  (PROD/engagement-prod.json) ---
  -> 18 pass
==================== RESULT: PASS (no hard failures) ====================
```

`PASS` means every environment matches its spec. If something were off, you'd see
the exact key, what's expected, and what's actually there:

```
--- PROD  (PROD/engagement-prod.json) ---
  FAIL  continueWatching.exitThreshold   expected=97   actual='0.97'
  -> 17 pass, 1 FAIL
```

---

## How a spec describes a config

A spec file has three parts: the config name, where its files live, and the keys:

```json
{
  "config": "engagement",
  "files": {
    "test":  "TEST ENV/engagement-test.json",
    "stage": "STAGE/engagement-stage.json",
    "prod":  "PROD/engagement-prod.json"
  },
  "keys": {
    "continueWatching.exitThreshold": { "expected": 97, "format": "percentage" }
  }
}
```

Each key is a **dotted path** into the JSON (`continueWatching.exitThreshold`
means `{ "continueWatching": { "exitThreshold": … } }`), and every key follows
**one of just two rules**:

### Rule 1 — `expected`: same value in every environment

Most keys. The value must be identical in test, stage, and prod.

```json
"continueWatching.exitThreshold": { "expected": 97 }
```

> "exitThreshold must be `97` everywhere."

### Rule 2 — `expectedPerEnv`: a different value per environment

For the handful of values that are *meant* to differ — like the license URL.

```json
"drmCertificateInfo.fairPlayLicenseUrl": {
  "expectedPerEnv": {
    "test":  "https://irdetoexperience.test.ott.irdeto.com/.../getckc",
    "stage": "https://license.ctrp.astro.com.my/.../getckc",
    "prod":  "https://license.ctrp.astro.com.my/.../getckc"
  }
}
```

> "the license is the Irdeto test server in test, and the Astro server in
> stage/prod — and each must stay exactly that."

### Rule 3 — `onlyIn`: a key that may exist in some environments and is *forbidden* in the rest

For keys that belong in certain environments only — e.g. a debug key that should
exist in `test` but must never reach `stage`/`prod`.

```json
"playerDebugView": { "onlyIn": "test", "expected": { "enableDebugView": true } }
```

> "this key is allowed in `test` (and must match the `expected` value there); in
> every other environment it must **not exist** at all."

In the environments it's *not* allowed in, `check` **fails** if the key is
present, and `apply` **removes** it. Use `"onlyIn": []` (allowed nowhere) to
forbid a key everywhere — that's how you retire an unwanted key: `apply` deletes
it from every environment.

That's the whole model: **same everywhere (`expected`)**, **per env
(`expectedPerEnv`)**, or **allowed only in some envs (`onlyIn`)**.

*(`format` — e.g. `"percentage"`, `"url"`, `"boolean"` — is an optional human
note. The tool does not enforce it; it's just there so the spec reads clearly.)*

---

## Everyday task: changing a config value

Say you want `exitThreshold` to be `95` instead of `97`, everywhere. The flow is
always the same five steps:

```
check  →  edit the spec  →  preview  →  apply  →  check
```

**1. Check you're starting clean:**
```bash
python3 validate.py check engagement      # -> all pass
```

**2. Edit the spec** (`config-spec/engagement.spec.json`):
```json
"continueWatching.exitThreshold": { "expected": 95 }     // was 97
```

**3. Preview** — see exactly what *would* change, in every env. Writes nothing:
```bash
python3 validate.py preview engagement
```
```
--- TEST  (TEST ENV/engagement-test.json) ---
  SET  continueWatching.exitThreshold   '97' -> '95'
--- STAGE  ... ---
  SET  continueWatching.exitThreshold   '97' -> '95'
--- PROD  ... ---
  SET  continueWatching.exitThreshold   '97' -> '95'
==================== 3 change(s) across 3 env(s) — run `apply` to make them ====================
```

**4. Apply** — only after you're happy with the preview. Backs up each file first:
```bash
python3 validate.py apply engagement
```

**5. Check again** — confirm it landed and nothing else moved:
```bash
python3 validate.py check engagement      # -> all pass
```

**Adding a new key** works the same way — add it to the spec and `apply`; it gets
created (nested paths included) in all three files:
```json
"refresh.newSetting": { "expected": 30 }
```

---

## Auditing a live API response

The environment files are what we *intend*. Sometimes you want to check what the
**API actually returns** (it serves these configs to the app). Drop the full API
response into the `api-responses/` folder and run `audit` — you don't name the
file, it just picks up what's in there:

```bash
python3 validate.py audit --env prod
```

(If you keep several files in the folder, it'll list them and you name which:
`python3 validate.py audit api-responses/prod-response.json --env prod`.)

It pulls each config's section out of the combined response and compares it to
that config's spec, for the environment you name:

```
==================== AUDIT: player  (env=prod, response section 'player') ====================
  DIFF  drmCertificateInfo.certificateUrl   expected='...getcertificate?...'   response='...getcertficate?...'
  DIFF  player.bufferTime.hd.vod            expected='30'                      response='270'
  -> 16 match, 5 differ
```

`audit` is **read-only** and **exactly strict** — it never assumes two things are
equal unless they're byte-identical (so `"30"` ≠ `30`, `300` ≠ `"PT300S"`). The
point of an audit is to surface *every* difference, including the one above where
the response has a typo'd URL (`getcertficate`).

`--env` is required because per-env values (like the license) differ by
environment, so the tool needs to know which env the response came from.

---

## Command reference

| Command | What it does | Writes? |
|---|---|---|
| `check [configs…]` | Verify env files match their spec. Exits non-zero if anything's off. | No |
| `preview [configs…]` | Show what `apply` would change. | No |
| `apply [configs…]` | Bring env files into line with the spec (undo via git). | **Yes** |
| `audit [configs…] --env E` | Compare the API response in `api-responses/` against the specs (strict). | No |

Common to all:

- **Target specific configs** by name: `check engagement`, or
  `check engagement learnActions`. With **no name**, every config is included.
- **One environment only:** add `--env test|stage|prod`.
- Running `python3 validate.py` with no command defaults to `check`.

---

## Safety net

- **`preview` before `apply`** — always see the diff first; `apply` never runs on
  its own.
- **Git is the undo** — the config files are tracked in git, so every `apply` is
  visible in `git diff` and reversible with `git checkout -- <file>` (or `git
  restore`). Review the diff, commit when happy. (The tool used to write
  `.backups/` copies; that's been removed now that git history covers it.)
- **`check` is your gate** — run it before any release (it exits non-zero on
  failure, so it works in CI):
  ```bash
  python3 validate.py check || exit 1
  ```

---

## Requirements

Python 3. No packages to install.
```bash
python3 validate.py check
```
```
