<p align="center">
  <img src="custom_components/kiosk_mgmt/brand/icon.png" alt="Kiosk Mgmt" width="128">
</p>

# Kiosk Mgmt

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-blue.svg)](https://www.home-assistant.io/)
[![GitHub Release](https://img.shields.io/github/v/release/bisman-automations/ha-kiosk-mgmt)](https://github.com/bisman-automations/ha-kiosk-mgmt/releases)
[![Test](https://github.com/bisman-automations/ha-kiosk-mgmt/actions/workflows/test.yml/badge.svg)](https://github.com/bisman-automations/ha-kiosk-mgmt/actions/workflows/test.yml)
[![Validate](https://github.com/bisman-automations/ha-kiosk-mgmt/actions/workflows/validate.yml/badge.svg)](https://github.com/bisman-automations/ha-kiosk-mgmt/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/bisman-automations/ha-kiosk-mgmt)](LICENSE)

Kiosk Mgmt finds every [Kiosk Satellite](https://kiosksatellite.com/) tablet on
your network and opens its remote admin page from a sidebar panel.

## Requirements

- Home Assistant 2026.10 or newer.
- Kiosk Satellite on the tablet, with Remote Administration turned on and a
  password set. The tablet and Home Assistant need to be on the same network.

## Installation

Kiosk Mgmt is not in the default catalog yet. Add this repository, then install it.

### Marketplace (Home Assistant 2026.11 and newer)

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=bisman-automations&repository=ha-kiosk-mgmt&category=integration)

1. Select the button above, or go to **Settings > Marketplace**, choose **Add
   from link**, enter `bisman-automations/ha-kiosk-mgmt` under **GitHub link**, and
   select **Add**. Adding a repository from a link needs a GitHub connection;
   the Marketplace asks you to connect one if you haven't.
2. Install **Kiosk Mgmt**. If a repair asks you to restart Home Assistant, restart it.

New versions show up under **Settings > Updates**. Already installed Kiosk Mgmt with
HACS? There's nothing to do: Home Assistant moves it into the Marketplace the
first time it starts on 2026.11.

### HACS (Home Assistant 2026.10 and older)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bisman-automations&repository=ha-kiosk-mgmt&category=integration)

Kiosk Mgmt does not appear in search until it is in the default catalog. Add it as a custom repository, then download it:

1. In HACS open the menu, choose **Custom repositories**, and add `https://github.com/bisman-automations/ha-kiosk-mgmt` as an **Integration**.
2. Download **Kiosk Mgmt** and restart Home Assistant.

### Custom repository

Kiosk Mgmt is not in the default catalog yet. The repository to add, in the
Marketplace or in HACS, is:

`https://github.com/bisman-automations/ha-kiosk-mgmt`

Add that as an integration repository, then install **Kiosk Mgmt** from the
list. The Marketplace button and the HACS button above open the same
repository.

### Manual

1. Download `kiosk_mgmt.zip` from the [latest release](https://github.com/bisman-automations/ha-kiosk-mgmt/releases/latest).
2. Unzip it into `config/custom_components/kiosk_mgmt/` and restart Home Assistant.

## Setup

[![Open your Home Assistant instance and start setting up Kiosk Mgmt.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=kiosk_mgmt)

Select the button above, or go to **Settings > Devices & services > Add
integration** and search for **Kiosk Mgmt**.

Home Assistant also offers to set it up when it hears a Kiosk Satellite on the
network. One entry covers every tablet. Further tablets are added on their
own.

If a tablet is not discovered, open the integration's options and choose
**Add a kiosk**. Type the tablet's address. Leave **Protocol** on
**Detect automatically** unless you know it: that tries HTTPS first, then
HTTP, on port 2324 (or whatever you set under Remote Administration). Leave
**Verify SSL certificate** off for the certificate Kiosk Satellite creates
for itself. Kiosk Mgmt shows that certificate's SHA-256 fingerprint and
remembers it. Later connections are allowed only when the tablet presents
the same certificate. Turn **Verify SSL certificate** on only for a
certificate signed by an authority Home Assistant already trusts.

If the tablet's certificate changes, for example after reinstalling Kiosk
Satellite, Home Assistant raises a repair named for the tablet. Open the
repair, compare the new fingerprint, and accept it only if you expected the
change. Reconfiguring the tablet offers the same choice.

### Sidebar

**Kiosk Mgmt** is in the sidebar for administrators. Each tablet is a tab
with its name, IP address, model, whether it is online, and when it was last
seen, plus **Open in new tab**.

The admin page is loaded through Home Assistant, not straight from the
tablet. That is what lets the tab show a self-signed certificate, and a
plain HTTP tablet, while Home Assistant itself is open over HTTPS. Only an
administrator can open the proxied page. **Open in new tab** uses the same
proxied address.

If the page does not load, the tab says so instead of leaving a blank area.
**Retry** asks again. **Open tablet directly** is the tablet's own address,
which may ask the browser to trust the certificate.

### Entities

Each tablet gets a device, a **Connectivity** sensor, and a **Last seen**
sensor. When Kiosk Satellite is also set up through ESPHome, the two devices
share the tablet's MAC address and Home Assistant shows them as one device.

### Ignoring a tablet

In the integration's options, choose **Manage kiosks**. **Ignore** hides a
tablet and keeps it hidden if it is discovered again. **Show again** brings
it back. **Remove** forgets it, so a later discovery can add it again.

## Troubleshooting

- **Nothing is discovered.** On the tablet, turn on Remote Administration and
  set a password. **Find other kiosks** is what announces the tablet to other
  Kiosk Satellite apps. With that off, the tablet can still be found from its
  ESPHome advertisement, or you can add it by address.
- **The tab says it can't load the admin page.** Choose **Retry**. If that
  still fails, use **Open tablet directly**. A certificate warning in that
  tab is the browser's own check; Kiosk Mgmt's pin is separate.
- **Certificate changed.** A repair named for the tablet means the
  certificate no longer matches the one Kiosk Mgmt pinned. Accept the new
  fingerprint from the repair, or from the tablet's reconfigure screen, only
  if you expected it to change.
- **Certificate is not trusted.** A repair with that wording means **Verify
  SSL certificate** is on and the certificate is not signed by an authority
  Home Assistant trusts. Leave **Verify SSL certificate** off to pin the
  tablet's own certificate, or install a certificate Home Assistant trusts.
- **Diagnostics:** on the integration's page, open the menu and choose
  **Download diagnostics**. Attach the file to an issue (check it first for
  anything you'd rather not share).
- **Debug logs:** on the integration's page choose **Enable debug logging**,
  reproduce the problem, then **Disable debug logging** to download the log.
- Report problems at [Issues](https://github.com/bisman-automations/ha-kiosk-mgmt/issues).

## Development

```
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```

The sidebar panel is built with `npm run build` from `frontend/panel.js`.
`node --test tests/*.mjs` checks the frame rules.

CI also tests the oldest supported Home Assistant (`requirements_test_min.txt`)
and runs hassfest and the HACS checks. Releases are drafted automatically from
`CHANGELOG.md` on every push to `main` and published by hand. See
[CLAUDE.md](CLAUDE.md) and [CONTRIBUTING](https://github.com/bisman-automations/.github/blob/main/CONTRIBUTING.md).

## License

[MIT](LICENSE)
