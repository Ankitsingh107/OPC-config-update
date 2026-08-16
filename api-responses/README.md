# api-responses/

Drop a full API config response here (the combined JSON with top-level
`engagement` / `player` / `learnActions` / `configuration` sections), then audit
it against the specs — you don't name the file, `audit` picks up whatever's here:

    python3 validate.py audit --env <test|stage|prod>

**Recommended:** save it as `response.json` and just overwrite it each time —
one known file, nothing piles up. The name is only used to tell files apart, so
if you'd rather keep responses around to compare later, name them by env
(`prod-response.json`, `stage-response.json`) and `audit` will let you pick.

`audit` is **read-only** — for each config it pulls that section out of the
response and compares it to the spec's expected values for the given environment,
reporting any differences (`DIFF`), keys the API didn't return (`MISS`), and keys
in the response that aren't in the spec. It never changes anything.

Audit a subset too:  `python3 validate.py audit engagement player --env prod`

If you keep several files in this folder, `audit` lists them and you name which
one: `python3 validate.py audit api-responses/<file>.json --env prod`
