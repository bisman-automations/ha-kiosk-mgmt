# Kiosk Mgmt: notes for Claude and other coding agents

Home Assistant custom integration (`kiosk_mgmt`), installed through the
Marketplace on Home Assistant 2026.11 and newer and through HACS on 2026.10.
It finds Kiosk Satellite tablets on the LAN and opens each one's remote
admin page from an admin-only sidebar panel. Read this before changing
anything.

## Layout

- `custom_components/kiosk_mgmt/`: the integration.
  - `__init__.py`: setup, unload, admin-only services.
  - `config_flow.py`: the single config entry, zeroconf confirm, manual host,
    options, and reconfigure.
  - `client.py`: probes `GET /api/health` on the admin port.
  - `tls.py`: leaf SHA-256 pinning and the aiohttp fingerprint check.
  - `proxy.py`: admin-only reverse proxy used by the sidebar iframe.
  - `repairs.py`: accept a changed certificate from the repair.
  - `discovery.py`: zeroconf and device-registry matching.
  - `coordinator.py`: polling, device registry, repairs.
  - `binary_sensor.py` and `sensor.py`: connectivity and last seen.
  - `panel.py`: admin sidebar panel and the static panel script.
  - `websocket_api.py`: admin-only live kiosk list.
  - `diagnostics.py`: config-entry diagnostics.
  - `frontend/kiosk-mgmt-panel.js`: the built Lit panel (source in
    `frontend/` at the repo root; `npm run build` refreshes it).
- `frontend/`: panel source. `logic.mjs` is the part the Node tests import.
- `tests/`: pytest, plus `tests/panel.test.mjs`.
- `README.md` is user documentation; `CHANGELOG.md` is the release history.

## Checks

The shared checks below. Also `node --test tests/*.mjs` for the panel
rules, and `npm run build` after changing `frontend/`. There is no mypy
job. Hassfest is `python -m script.hassfest --integration-path
custom_components/kiosk_mgmt` from a Home Assistant core checkout of the
version under test (hassfest is not installed with the test harness).

## Conventions

Discovery follows the Kiosk Satellite app, not guesses:

- Fleet mDNS is `_kiosk-satellite._tcp` (`FleetDiscovery.kt`). TXT keys are
  `id`, `name`, `version`, `port`, and optionally `host`, `tls`, `dns`,
  `admin`. The instance is `ks-<id>`. This is announced only while Find
  other kiosks is on. `id` is the fleet id or the Android id.
- ESPHome mDNS is `_esphomelib._tcp` (`MdnsAnnouncer.kt`). TXT includes
  `mac`, `project_name`, `project_version`, `friendly_name`, `board=android`,
  and `platform=ESP32`. The port in that advertisement is the ESPHome API
  port (6053), not the admin port. `project_name` is always
  `kiosk_satellite.bluetooth_proxy` (`BluetoothProxyRuntime.kt`).
- Home Assistant's ESPHome integration splits that project name into
  manufacturer `kiosk_satellite` and model `bluetooth_proxy` on the device
  registry. The device name is the friendly name, so it often does not
  start with `ks-`. The node name does: `ks-<slug>` on a fresh install
  (`node_name.dart`), or `kiosk-satellite-<suffix>` when no node name is set.
- The admin server listens on port 2324 unless changed
  (`definitions.dart`). `GET /api/health` is the unauthenticated identity
  document (`remote_manager.dart`). It has no MAC. The MAC Home Assistant
  already has is the ESPHome one: synthetic from the Android id, or the
  real Wi-Fi MAC when the tablet is set to report it.
- ESPHome's Visit link is `http://<host>:<webserver port>` and the app
  sends that port only for plain HTTP (`bt_proxy_manager.dart`
  `_webserverPort`). HTTPS admin has no Visit link, so the address comes
  from the fleet advertisement or from a host typed in the flow.
- This integration never reads or writes ESPHome `runtime_data` or another
  integration's config entry data. Device registry entries are read-only
  input. Our own device is registered with the same MAC connection so Home
  Assistant merges it with the ESPHome device.

One config entry holds every kiosk under `options["kiosks"]`. The stable
id prefers the fleet `id`, then the MAC, then an id minted for a manual
host. A later advertisement with the same host attaches the fleet id and
MAC to that record. Ignored kiosks stay in options so discovery does not
add them back.

Scheme `auto` tries HTTPS, then HTTP, and remembers which one answered.
`verify_ssl` off (the default) pins the leaf certificate: the first
successful HTTPS handshake records its SHA-256, and later connections check
that fingerprint with aiohttp's `Fingerprint` instead of a certificate
authority. The config flow shows the fingerprint before storing it.
`verify_ssl` on uses Home Assistant's verifying session, for a certificate
an authority already signed. A stored pin that no longer matches raises a
repair and does not update the pin until the user accepts the new
certificate. Entries from 1.1 (`verify_ssl` false, no fingerprint) are
migrated to 1.2 and pinned on the next successful probe.

The panel does not embed the tablet directly. The iframe loads
`/api/kiosk_mgmt/proxy/{entry_id}/{kiosk_id}/` with a short-lived signed
path (`async_sign_path`). That response sets an HttpOnly cookie so the
embedded app's later requests, including POST and websockets, are
authorized. The proxy uses the pinned connection, rewrites redirects and
absolute URLs onto the prefix, and strips `X-Frame-Options` and CSP
`frame-ancestors`. If the frame does not load, the tab shows a message, a
retry, and a direct link to the tablet. Open in new tab uses the proxy too.

No secrets are stored. Entity state is connectivity and last seen.

## Open decisions

- `/api/health` has no MAC, so a host typed by hand is matched to a later
  advertisement by host until a fleet id or MAC shows up.
- A kiosk with Find other kiosks off and ESPHome off is invisible until
  someone adds it by host.

<!-- BEGIN bisman-automations shared rules -->
<!-- This block is the same in every bisman-automations repo. Edit it in
     bisman-automations/.github (templates/CLAUDE.shared.md) and copy it to
     every repo; the Validate workflow's "standards" job checks it matches. -->

## Shared rules (every bisman-automations repo)

### Releases

- The version lives in `custom_components/<domain>/manifest.json`. Anything
  else that repeats it is called out in the repo-specific notes below and is
  bumped in the same commit.
- Versions follow [Semantic Versioning](https://semver.org): patch for fixes,
  translations, docs and CI; minor for new features, options or entities;
  major when something users rely on is removed or renamed (entities,
  services, events, options) or setup has to be redone.
- Check what is released before choosing a version:
  `git ls-remote --tags origin | awk -F/ '{print $3}' | sort -V | tail -1`
  (`sort -V`: plain sorting puts `v1.10.0` before `v1.9.0`). Never add
  changes to a released version. If the manifest version has no tag yet,
  add to its CHANGELOG section instead of bumping again.
- Every push to `main` ships as a release. In that same commit, named
  `Release X.Y.Z`, bump the manifest version and write the notes directly
  under a new `## [X.Y.Z] - YYYY-MM-DD` heading in `CHANGELOG.md` (Keep a
  Changelog: Added, Changed, Deprecated, Removed, Fixed, Security), with
  that version's compare link at the bottom. There is no `## [Unreleased]`
  section and no `[Unreleased]:` compare link. New work is not collected
  under a holding heading and moved later; the release commit is where the
  heading and the entries are written. The standards check fails if a
  `## [Unreleased]` heading is present.
- Ask Donavan before every push. Push fast-forward only: never force-push,
  rebase or amend anything already pushed, or rewrite history in any other
  way. Rewriting history orphans the release tags (it broke drafting in
  ha-button-actions).
- Pushing to `main` runs **Draft release**, which creates or refreshes a
  draft `vX.Y.Z` with that version's CHANGELOG section as the notes, the
  commits since the last release and `<domain>.zip`. **Donavan publishes
  the draft himself**, and publishing creates the tag. Never create or push
  tags, publish releases, or edit published releases.
- Publishing runs **Release check**: the tag must match the manifest version
  and the CHANGELOG, and be on `main`. If it fails, fix it with a new patch
  release, not by moving the tag.

### Commits

- Author commits as `Donavan Becker <donavan.becker@icloud.com>` (set
  `git config user.name` / `user.email` in the clone). HACS's
  default-repository check needs the submitter to have a fair share of the
  commits. Credit an AI assistant with a `Co-Authored-By:` trailer if you
  like, never as the author.
- Subject lines are plain, imperative and say what changed for the user
  ("Show Pico remotes as unavailable when Lutron isn't running"). No
  `fix:`/`feat:` prefixes. Release commits are "Release X.Y.Z".
- Commit and push only when asked.

### CI (shared workflows)

- `.github/workflows/` holds four thin callers of the reusable workflows in
  [bisman-automations/.github](https://github.com/bisman-automations/.github):
  `test.yml` (lint, frontend, pytest on the oldest supported and the newest
  Home Assistant, plus the upcoming one as information only), `validate.yml`
  (hassfest, HACS, standards), `draft-release.yml` and `release-check.yml`.
  Don't add jobs to the callers. Use the shared workflow's inputs, or change
  the shared workflow so every repo gets the improvement.
- `requirements_test.txt` leaves `pytest-homeassistant-custom-component`
  unpinned (newest Home Assistant) and pins ruff (and mypy where used).
  `requirements_test_min.txt` pins the harness release for the minimum in
  `hacs.json`: the newest harness release of that month. CI fails if they
  drift apart.
- Raising the minimum Home Assistant means changing, together: `hacs.json`,
  `requirements_test_min.txt`, `python-min` in `test.yml` (3.13 up to
  2026.2, 3.14 from 2026.3), the README's Home Assistant badge, and a
  CHANGELOG entry.
- Home Assistant 2026.3 and newer need Python 3.14 (3.14.2+).
- The published hassfest rejects `categories` in `manifest.json` and Pillow
  in `requirements`. Leave both out.
- Dependabot (`.github/dependabot.yml`) proposes action and pinned-tool
  updates weekly. It ignores the test harness on purpose.
- Test and Validate also run every Thursday, the day after Home Assistant's
  betas come out. The **next** test job (newest pre-release harness) is
  information only: when it fails, fix the cause before that release ships.

### Before saying work is done

Run from the repo root, in a Python 3.14 venv with `requirements_test.txt`
installed:

```
ruff check .
ruff format --check .
pytest
```

Also run `node --test tests/*.mjs` when the repo has Node tests, mypy when
the repo-specific notes list it, and the tests in a second venv with
`requirements_test_min.txt` when a change could behave differently on the
oldest supported Home Assistant. The standards check (also in CI) is
`python <path to bisman-automations/.github>/scripts/check_repo.py .`.

### Code and docs

- Support every Home Assistant from the `hacs.json` minimum to the newest
  release. Check for deprecations in the newest release and the beta.
- Keep entity IDs, unique IDs, service names and event names stable. When
  one has to change, migrate it and say so under Changed in the CHANGELOG.
- `strings.json` and `translations/en.json` hold the same keys. Home
  Assistant reads only `translations/`, so change both in the same commit.
- Never put personal details in the repo (README, CHANGELOG, code, tests,
  screenshots): no names, addresses, account or device identifiers, or real
  entity IDs. Use placeholders such as `light.living_room`.
- `README.md` is user documentation with the standard header (badges, HACS
  and "add integration" My buttons) and sections Installation, Setup, ...,
  Troubleshooting, Development, License. Use plain language and show names,
  not entity IDs, wherever the UI does.
<!-- END bisman-automations shared rules -->
