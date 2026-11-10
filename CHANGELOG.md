# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/bisman-automations/ha-kiosk-mgmt/releases/tag/v0.1.0
