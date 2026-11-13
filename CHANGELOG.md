# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] - 2026-11-13

### Changed

- **Smoother updates.** Each kind of entity now tells Home Assistant how many updates it can take at once, as Home Assistant recommends. Nothing changes in how anything looks or works.

## [0.1.2] - 2026-11-11

### Added

- Debug logging for each request the sidebar sends to a tablet, and for the
  error when a tablet cannot be reached. Passwords and login tokens are not
  written to the log.

### Fixed

- The sidebar shows the whole Kiosk Satellite admin page. The embedded page
  fills the tab on a desktop and on a phone, instead of stopping after the
  logo.
- Logging in from the sidebar works with the tablet's password. The tablet's
  own login token is passed through to the tablet. It used to be removed on
  the way, the tablet answered 401, and the page stayed on the login card.
- Scripts, styles and live updates from the tablet load through Home
  Assistant, including websockets. A cookie set by the tablet stays on this
  page. If part of the page still fails, the tab says whether Home Assistant
  or the tablet refused it.

## [0.1.1] - 2026-11-10

### Added

- Pin a Kiosk Satellite's self-signed certificate the first time it connects,
  and check that fingerprint on later connections. The fingerprint is shown
  when you add or reconfigure a tablet. Verify SSL certificate stays
  available for a certificate signed by an authority Home Assistant already
  trusts.
- Open each tablet's admin page through Home Assistant, so the sidebar can
  show it when the tablet's certificate is self-signed. The page is loaded
  only for an administrator. Open in new tab uses that same proxied page.

### Changed

- Tablets that had certificate checking turned off are pinned the next time
  they answer, instead of skipping the certificate check.

### Fixed

- Adding or reconfiguring a tablet that uses Kiosk Satellite's own
  certificate no longer stops with a message to turn off Verify SSL
  certificate.
- The sidebar tab no longer stays blank when the browser will not load the
  tablet's admin page directly. If the proxied page does not load, the tab
  explains that and offers a retry and a direct link.

## [0.1.0] - 2026-11-10

### Added

- Discover Kiosk Satellite tablets from their own mDNS advertisement and from
  ESPHome devices already in Home Assistant, and confirm each one by reading
  its admin health page.
- An administrator sidebar, Kiosk Mgmt, with one tab per tablet. The tab
  embeds the remote admin page, or explains when the browser will not
  (mixed content, or a certificate the browser has not trusted yet).
- Connectivity and last seen for each tablet, and options to add a tablet by
  address, choose HTTP or HTTPS, and turn certificate checking on or off.
- Ignore or remove a tablet. An ignored tablet stays hidden when it is
  discovered again.

[0.1.3]: https://github.com/bisman-automations/ha-kiosk-mgmt/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/bisman-automations/ha-kiosk-mgmt/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/bisman-automations/ha-kiosk-mgmt/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/bisman-automations/ha-kiosk-mgmt/releases/tag/v0.1.0
