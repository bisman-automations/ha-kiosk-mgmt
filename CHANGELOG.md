# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.1]: https://github.com/bisman-automations/ha-kiosk-mgmt/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/bisman-automations/ha-kiosk-mgmt/releases/tag/v0.1.0
