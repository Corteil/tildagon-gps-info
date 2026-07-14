"""
GPS Sky Map for the Tildagon badge.

Shows GPS connection/fix status, position data, the satellite count and a polar
sky map of the satellites in view. Designed for the round display.

Requires:
  * Tildagon OS v2.0.0+      (hexpansion app discovery)
  * GPS Hexpansion firmware v3+ for satellite/altitude data (sky map). With
    older firmware the app still shows position and a "update firmware" hint.

License: MIT
"""
import app
import math
import ota

from events.input import Buttons, BUTTON_TYPES
from tildagonos import tildagonos
from system.eventbus import eventbus
from system.hexpansion.events import HexpansionMountedEvent
from system.patterndisplay.events import PatternDisable, PatternEnable
from system.scheduler.events import RequestForegroundPushEvent, RequestForegroundPopEvent

# GPS hexpansion identity (matches the GPS firmware EEPROM header)
GPS_VID = 0x7CAB
GPS_PID = 0xBEAC

# Round display geometry: 240x240, origin at the centre, visible radius 120
HORIZON_R = 104     # radius of the horizon (elevation 0) ring
CARD_R = 110        # radius for the N/E/S/W cardinal labels

VIEW_SKY = 0
VIEW_DATA = 1

FIX_LABELS = {1: "No Fix", 2: "2D Fix", 3: "3D Fix"}

# --- Easter egg: the satellites become rubber ducks near the EMF ponds ---
DUCK = "⇩"                 # the badge font's duck glyph (app_components.tokens "duck")
EMF_POND = (52.03927, -2.38026)  # EMF ponds @ Eastnor Castle Deer Park (can recalibrate on site)
DUCK_RANGE_M = 100              # ducks appear within this distance of the pond
DUCK_YELLOW = (1.0, 0.82, 0.0)
DUCK_UNLOCK_PRESSES = 10        # Down-presses on the sky view to reveal the setting


def _get(key, default=None):
    try:
        import settings
        v = settings.get(key)
        return default if v is None else v
    except Exception:
        return default


def _set(key, value):
    try:
        import settings
        settings.set(key, value)
        settings.save()
    except Exception:
        pass


def get_app_by_vid_pid_shim(vid, pid):
    try:
        # Only available on Tildagon OS v2 or newer
        from system.hexpansion.util import get_app_by_vid_pid
        return get_app_by_vid_pid(vid, pid)
    except (ImportError, AttributeError):
        # ImportError: pre-v2 OS. AttributeError: hexpansion manager not ready.
        return None


def snr_colour(snr):
    """Traffic-light colour by signal strength (C/N0 in dB)."""
    if snr <= 0:
        return (0.45, 0.45, 0.45)
    if snr < 20:
        return (0.93, 0.14, 0.0)
    if snr < 30:
        return (1.0, 0.75, 0.0)
    return (0.0, 0.85, 0.3)


def sky_xy(azimuth, elevation, r_max=HORIZON_R):
    """Map azimuth/elevation to x,y on the round screen (North up, clockwise)."""
    el = 0 if elevation < 0 else (90 if elevation > 90 else elevation)
    r = r_max * (90 - el) / 90
    a = math.radians(azimuth)
    return (r * math.sin(a), -r * math.cos(a))


class GPSSkyMap(app.App):

    def __init__(self):
        self.button_states = Buttons(self)
        self.view = VIEW_SKY

        # Latest snapshot of GPS data (refreshed each frame)
        self.position = None
        self.altitude = 0.0
        self.speed = 0.0
        self.bearing = 0.0
        self.num_sats = 0
        self.fix_type = 1
        self.sats = []
        self.sky_data = False   # True when firmware exposes raw NMEA sentences

        # LED status-ring state (colour = fix status, count = sats in view)
        self._led_state = None
        self._led_phase = 0

        # Easter egg state
        self._down_count = 0
        self._toast = ""
        self._toast_ms = 0

        self.gps = None
        self._find_gps_module()

        # Subscribe to events
        eventbus.on_async(RequestForegroundPushEvent, self._resume, self)
        eventbus.on_async(RequestForegroundPopEvent, self._pause, self)
        eventbus.on_async(HexpansionMountedEvent, self._mounted, self)
        try:
            from system.hexpansion.events import HexpansionUnmountedEvent
            eventbus.on_async(HexpansionUnmountedEvent, self._unmounted, self)
        except ImportError:
            from system.hexpansion.events import HexpansionRemovalEvent
            eventbus.on_async(HexpansionRemovalEvent, self._unmounted, self)

        # Take over the LEDs for the satellite indicator
        eventbus.emit(PatternDisable())

    def _find_gps_module(self):
        self.gps = get_app_by_vid_pid_shim(GPS_VID, GPS_PID)

    # --- lifecycle --------------------------------------------------------

    async def _resume(self, _: RequestForegroundPushEvent):
        eventbus.emit(PatternDisable())
        self._led_state = None

    async def _pause(self, _: RequestForegroundPopEvent):
        self._leds_off()
        eventbus.emit(PatternEnable())

    async def _mounted(self, _):
        if not self.gps:
            self._find_gps_module()

    async def _unmounted(self, e):
        if self.gps and getattr(e, "port", None) == self.gps.config.port:
            self.gps = None

    # --- update -----------------------------------------------------------

    def update(self, delta):
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self._leds_off()
            self.minimise()
            return

        # Toggle between the sky map and the data readout
        if self.button_states.get(BUTTON_TYPES["RIGHT"]):
            self.view = (self.view + 1) % 2
            self.button_states.clear()
        if self.button_states.get(BUTTON_TYPES["LEFT"]):
            self.view = (self.view - 1) % 2
            self.button_states.clear()

        # Hidden duck controls (sky view only)
        if self.view == VIEW_SKY:
            if self.button_states.get(BUTTON_TYPES["DOWN"]):
                self._duck_down()
                self.button_states.clear()
            if self.button_states.get(BUTTON_TYPES["UP"]):
                if self._duck_unlocked():
                    self._cycle_ducks(-1)
                self.button_states.clear()
            if self.button_states.get(BUTTON_TYPES["CONFIRM"]):
                if self._duck_unlocked() and self.position:
                    _set("gpsinfo_pond_lat", self.position[0])
                    _set("gpsinfo_pond_lon", self.position[1])
                    self._toast_show("Pond set " + DUCK)
                self.button_states.clear()
        if self._toast_ms > 0:
            self._toast_ms -= delta

        # Snapshot GPS data. Position/speed/bearing come from the firmware; the
        # satellite/altitude/fix data is parsed here from the raw NMEA sentences
        # the firmware buffers (keeps the hexpansion firmware tiny).
        if self.gps:
            self.position = self.gps.position
            self.speed = getattr(self.gps, "speed", 0.0)
            self.bearing = getattr(self.gps, "bearing", 0.0)
            sentences = getattr(self.gps, "sentences", None)
            self.sky_data = sentences is not None
            if self.sky_data:
                self._parse_nmea(sentences)
            else:
                self.sats = []
                self.num_sats = 0
                self.altitude = 0.0
                self.fix_type = 1
        else:
            self.position = None
            self.sats = []

        self._update_leds(delta)

    def _parse_nmea(self, sentences):
        """Parse GGA (count/altitude), GSA (2D/3D) and GSV (satellites) from the
        raw, checksum-stripped sentences buffered by the firmware."""
        sats = {}       # "talker:prn" -> sat dict
        gsv = {}        # talker -> {prn: sat dict} accumulator for current cycle
        num_sats = 0
        altitude = 0.0
        fix_type = 1
        for line in sentences:
            p = line.split(',')
            if len(p[0]) < 6:
                continue
            talker = p[0][1:3]
            stype = p[0][3:6]
            try:
                if stype == "GGA":
                    if p[7]:
                        num_sats = int(p[7])
                    if p[9]:
                        altitude = float(p[9])
                elif stype == "GSA":
                    if p[2]:
                        fix_type = int(p[2])
                elif stype == "GSV":
                    total = int(p[1])
                    num = int(p[2])
                    if num == 1:
                        gsv[talker] = {}
                    acc = gsv.get(talker)
                    if acc is None:
                        continue
                    for i in range(4, len(p) - 3, 4):
                        if not p[i]:
                            continue
                        prn = int(p[i])
                        acc[prn] = {
                            "prn": prn,
                            "elevation": int(p[i + 1]) if p[i + 1] else 0,
                            "azimuth": int(p[i + 2]) if p[i + 2] else 0,
                            "snr": int(p[i + 3]) if p[i + 3] else 0,
                        }
                    if num >= total:
                        prefix = talker + ":"
                        for k in [k for k in sats if k.startswith(prefix)]:
                            del sats[k]
                        for prn, s in acc.items():
                            sats[prefix + str(prn)] = s
            except (ValueError, IndexError):
                continue

        self.num_sats = num_sats
        self.altitude = altitude
        self.fix_type = fix_type
        out = []
        for s in sats.values():
            s2 = dict(s)
            s2["used"] = s2["snr"] > 0
            out.append(s2)
        self.sats = out

    # Status -> base colour: 0 off, 1 acquiring (red), 2 2D (amber), 3 3D (green)
    LED_COLOURS = {0: (0, 0, 0), 1: (70, 0, 0), 2: (80, 50, 0), 3: (0, 80, 0)}

    def _update_leds(self, delta):
        # Connection status drives the colour
        if not self.gps or ota.get_version().startswith("v1."):
            status = 0
        elif self.fix_type >= 3 and self.position:
            status = 3
        elif self.fix_type == 2 or self.position:
            status = 2
        else:
            status = 1

        # Number of lit LEDs = satellites in view (whole ring pulses if none yet)
        count = min(12, len(self.sats))
        if status == 1 and count == 0:
            count = 12

        if status == 1:
            # Pulse while acquiring a fix, to stand out
            self._led_phase = (self._led_phase + delta) % 1200
            b = 0.15 + 0.85 * (0.5 * (1 + math.sin(2 * math.pi * self._led_phase / 1200)))
            self._write_leds(status, count, b)
        else:
            key = (status, count)
            if key != self._led_state:
                self._led_state = key
                self._write_leds(status, count, 1.0)

    def _write_leds(self, status, count, brightness):
        base = GPSSkyMap.LED_COLOURS[status]
        col = (int(base[0] * brightness), int(base[1] * brightness), int(base[2] * brightness))
        try:
            for i in range(12):
                tildagonos.leds[i + 1] = col if i < count else (0, 0, 0)
            tildagonos.leds.write()
        except Exception:
            pass

    def _leds_off(self):
        try:
            for i in range(12):
                tildagonos.leds[i + 1] = (0, 0, 0)
            tildagonos.leds.write()
        except Exception:
            pass
        self._led_state = None

    # --- easter egg -------------------------------------------------------

    def _duck_unlocked(self):
        return bool(_get("gpsinfo_duck_unlocked", False))

    def _toast_show(self, msg):
        self._toast = msg
        self._toast_ms = 1600

    def _duck_down(self):
        if self._duck_unlocked():
            self._cycle_ducks(1)
            return
        self._down_count += 1
        if self._down_count >= DUCK_UNLOCK_PRESSES:
            _set("gpsinfo_duck_unlocked", True)
            self._down_count = 0
            self._toast_show("Ducks unlocked! " + DUCK)

    def _cycle_ducks(self, d):
        modes = ("auto", "on", "off")
        cur = _get("gpsinfo_ducks", "auto")
        i = modes.index(cur) if cur in modes else 0
        mode = modes[(i + d) % len(modes)]
        _set("gpsinfo_ducks", mode)
        self._toast_show("Ducks: " + mode + " " + DUCK)

    def _near_pond(self):
        if not self.position:
            return False
        plat = _get("gpsinfo_pond_lat", EMF_POND[0])
        plon = _get("gpsinfo_pond_lon", EMF_POND[1])
        dlat = (self.position[0] - plat) * 111320.0
        dlon = (self.position[1] - plon) * 111320.0 * math.cos(math.radians(self.position[0]))
        return (dlat * dlat + dlon * dlon) < (DUCK_RANGE_M * DUCK_RANGE_M)

    def _ducks_active(self):
        mode = _get("gpsinfo_ducks", "auto")
        if mode == "off":
            return False
        if mode == "on":
            return True
        return self._near_pond()          # auto: ducks near the EMF ponds

    # --- draw -------------------------------------------------------------

    def draw(self, ctx):
        ctx.rgb(0.05, 0.06, 0.10).rectangle(-120, -120, 240, 240).fill()

        # Status screens take priority over the data views
        if ota.get_version().startswith("v1."):
            self._draw_status(ctx, "Tildagon OS", "v2.0.0 required", (0.0, 0.75, 0.29))
            return
        if not self.gps:
            self._draw_status(ctx, "GPS Hexpansion", "not found", (0.93, 0.14, 0.0))
            return
        if not self.position and not self.sats:
            self._draw_status(ctx, "Acquiring", "waiting for GPS...", (1.0, 0.75, 0.0))
            return

        if self.view == VIEW_SKY:
            self._draw_skymap(ctx)
        else:
            self._draw_data(ctx)

        if self._toast_ms > 0:
            ctx.save()
            ctx.text_align = ctx.CENTER
            ctx.text_baseline = ctx.MIDDLE
            ctx.font_size = 20
            ctx.rgb(1.0, 0.85, 0.1).move_to(0, 62).text(self._toast)
            ctx.restore()

    def _draw_status(self, ctx, title, subtitle, colour):
        ctx.save()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 26
        ctx.rgb(*colour).move_to(0, -12).text(title)
        ctx.font_size = 18
        ctx.rgb(0.85, 0.85, 0.85).move_to(0, 16).text(subtitle)
        ctx.restore()

    def _draw_skymap(self, ctx):
        ctx.save()

        # Elevation rings: horizon (0), 30 and 60 degrees
        ctx.line_width = 2
        for el in (0, 30, 60):
            r = HORIZON_R * (90 - el) / 90
            ctx.rgba(0.45, 0.50, 0.60, 0.9 if el == 0 else 0.4)
            ctx.begin_path()
            ctx.arc(0, 0, r, 0, 2 * math.pi, False)
            ctx.stroke()

        # Cross hairs N-S / E-W
        ctx.rgba(0.35, 0.40, 0.50, 0.4)
        ctx.line_width = 1
        ctx.begin_path()
        ctx.move_to(0, -HORIZON_R).line_to(0, HORIZON_R)
        ctx.move_to(-HORIZON_R, 0).line_to(HORIZON_R, 0)
        ctx.stroke()

        # Cardinal labels
        ctx.font_size = 14
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.rgb(0.65, 0.70, 0.85)
        for label, (x, y) in (
            ("N", (0, -CARD_R)), ("S", (0, CARD_R)),
            ("E", (CARD_R, 0)), ("W", (-CARD_R, 0)),
        ):
            ctx.move_to(x, y).text(label)

        # Satellites -- or rubber ducks near the EMF ponds
        ducks = self._ducks_active()
        if ducks:
            ctx.text_align = ctx.CENTER
            ctx.text_baseline = ctx.MIDDLE
        for s in self.sats:
            x, y = sky_xy(s["azimuth"], s["elevation"])
            snr = s["snr"]
            if ducks:
                ctx.font_size = 16 + min(12.0, snr / 3.0)
                ctx.rgb(*DUCK_YELLOW).move_to(x, y).text(DUCK)
                continue
            rad = 4 + min(4.0, snr / 12.0)
            ctx.begin_path()
            ctx.rgb(*snr_colour(snr))
            ctx.arc(x, y, rad, 0, 2 * math.pi, False)
            ctx.fill()
            if s["used"]:
                ctx.begin_path()
                ctx.rgb(1, 1, 1)
                ctx.line_width = 1.5
                ctx.arc(x, y, rad + 2, 0, 2 * math.pi, False)
                ctx.stroke()

        # Summary text: fix type (top), counts (bottom)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 18
        ctx.rgb(1, 1, 1)
        ctx.move_to(0, -86).text(FIX_LABELS.get(self.fix_type, "--"))
        if ducks and self.sky_data:
            ctx.rgb(*DUCK_YELLOW)
            ctx.move_to(0, 86).text(f"{len(self.sats)} ducks {DUCK}")
        elif self.sky_data:
            ctx.move_to(0, 86).text(f"{self.num_sats} used / {len(self.sats)} seen")
        else:
            ctx.font_size = 15
            ctx.rgb(1.0, 0.75, 0.0)
            ctx.move_to(0, 86).text("update GPS firmware")

        ctx.restore()

    def _draw_data(self, ctx):
        ctx.save()
        ctx.text_baseline = ctx.MIDDLE

        if self.position:
            lat = f"{self.position[0]:.5f}"
            lon = f"{self.position[1]:.5f}"
        else:
            lat = lon = "--"

        rows = [
            ("Fix", FIX_LABELS.get(self.fix_type, "--")),
            ("Sats", f"{self.num_sats}/{len(self.sats)}"),
            ("Lat", lat),
            ("Lon", lon),
            ("Alt", f"{self.altitude:.0f} m" if self.sky_data else "n/a"),
            ("Speed", f"{self.speed:.1f} kn"),
            ("Course", f"{self.bearing:.0f}°"),
        ]

        ctx.font_size = 18
        y = -((len(rows) - 1) * 22) / 2
        for label, val in rows:
            ctx.text_align = ctx.LEFT
            ctx.rgb(0.6, 0.7, 0.9).move_to(-66, y).text(label)
            ctx.text_align = ctx.RIGHT
            ctx.rgb(1, 1, 1).move_to(66, y).text(val)
            y += 22

        ctx.restore()


__app_export__ = GPSSkyMap # pylint: disable=invalid-name
