# GPS Info

A Tildagon badge app (menu name **"GPS Info"**) that shows live GPS status and a
**sky map** of the satellites currently in view, designed for the round display.
Built on top of the GPS Hexpansion from
[emf-speedometer](https://github.com/mbooth101/emf-speedometer) /
[TechCabin GPS Hexpansion](https://github.com/TechCabin/EMFBadge-Hexpansions-GPS).

## Features

* **Sky map view** — polar plot of satellites in view. Centre = directly
  overhead (90° elevation), outer ring = horizon (0°). North is up, azimuth
  increases clockwise. Dots are coloured by signal strength (C/N0):
  grey = not tracked, red < 20 dB, amber < 30 dB, green ≥ 30 dB. Tracked
  satellites get a white ring. Rings mark 0°/30°/60° elevation.
* **Data view** — fix type (2D/3D), satellites used/in-view, latitude,
  longitude, altitude, speed and course.
* **Connection status** — clear screens for "OS v2.0.0 required", "GPS
  Hexpansion not found" and "waiting for GPS".
* **LED status ring** — the 12 edge LEDs show connection status by **colour**
  and satellite count by **number lit**:
  * all off = no hexpansion / OS too old
  * red, pulsing = acquiring, no fix yet
  * amber = 2D fix
  * green = 3D fix
  * number of lit LEDs = satellites in view

## Controls

* **Left / Right**: switch between the sky map and the data readout.
* **F (Cancel)**: return to the Tildagon main menu.

## Requirements

* **Tildagon OS v2.0.0+** (uses hexpansion app discovery).
* **GPS Hexpansion firmware v3+** (in this repo's `EEPROM/` folder) for the
  satellite and altitude data. The stock/speedometer firmware only provides
  position, so with it the sky map will be empty and the app shows an
  "update GPS firmware" hint.

> **Why a separate firmware?** The GPS Hexpansion's EEPROM (M24C16) is only
> **2 KB**, far too small to hold a full NMEA parser. So this firmware stays
> tiny (~1.4 KB compiled): it parses only RMC (position/speed/bearing, keeping
> the speedometer working) and **buffers the raw NMEA sentences**. The badge app
> reads `gps.sentences` and parses GGA/GSV/GSA itself, on the badge flash where
> there is plenty of room.

## 1. Flash the GPS Hexpansion firmware

Insert the GPS Hexpansion into **port 2**, then:

```powershell
# one-time: install the cross compiler
pip install mpy-cross

cd "C:\Users\brian\OneDrive\Documents\Hardware\Projects\emf-gps-skymap"
python -m mpy_cross EEPROM\gps.py            # produces EEPROM\gps.mpy

# Find the badge's COM port with `mpremote devs` (it changes with OS version;
# look for the 303a/16d0 device). Substitute it below for COM13.
mpremote connect COM13 mount EEPROM + run EEPROM/prepare_eeprom.py + cp EEPROM/gps.mpy :/hexpansion/app.mpy
```

The GPS Hexpansion can be moved to any port after flashing; port 2 is only
hard-coded in `prepare_eeprom.py` for the flashing step.

> The extended firmware is backwards compatible — the `GPSEvent(position,
> speed, bearing)` signature is unchanged, so the speedometer app keeps working.

## 2. Install the app

install the app from the app library or follow the instructions below

The launcher loads each app as `apps.<folder>.app` and reads its menu name from
a `metadata.json`, so the app must be installed as a **folder** (a bare `.mpy`
file will not load):

```powershell
cd "C:\Users\brian\OneDrive\Documents\Hardware\Projects\emf-gps-skymap"
python -m mpy_cross app.py

mpremote connect COM13 fs mkdir :/apps/gps_info ^
  + fs cp app.mpy       :/apps/gps_info/app.mpy ^
  + fs cp metadata.json :/apps/gps_info/metadata.json ^
  + fs cp tildagon.toml :/apps/gps_info/tildagon.toml
```

`metadata.json` is `{"name": "GPS Info", "version": "1", "hidden": false}` — the
`name` field is what shows in the badge menu.

## Credits & references

* [emf-speedometer](https://github.com/mbooth101/emf-speedometer) by Mat Booth —
  the original GPS Hexpansion firmware and app structure this builds on.
* [TechCabin GPS Hexpansion](https://github.com/TechCabin/EMFBadge-Hexpansions-GPS)
  — the GPS Hexpansion hardware/firmware concept.
* [emfcamp/badge-2024-software](https://github.com/emfcamp/badge-2024-software)
  — Tildagon OS (app framework, hexpansion API, `ctx` drawing).
* [Tildagon documentation](https://tildagon.badge.emfcamp.org/) — badge, app and
  flashing docs.
* NMEA 0183 sentences used: RMC (position/speed/course), GGA (fix quality,
  satellites used, altitude), GSA (2D/3D fix), GSV (satellites in view —
  PRN/elevation/azimuth/SNR).

## License

MIT

