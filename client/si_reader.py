"""
SportIdent chip reader — background thread that talks to the SI readout station.

How SportIdent readout works:
  1. A BSM7/BS11 station is plugged in via USB (appears as a COM port on Windows).
  2. We open it with SIReaderReadout (the sportident Python library).
  3. The station is put into M_READOUT mode so it actively waits for chips.
  4. poll_sicard() is called in a tight loop — it returns True when a chip is
     inserted or removed. It takes NO arguments; it's not a callback registration.
  5. On insert: read_sicard() downloads all punch data from the chip into a dict.
  6. ack_sicard() sends an ACK byte back to the station — this is what makes it BEEP.
     The beep confirms to the competitor that their chip was read successfully.
  7. On remove: we clear the last-seen card so the same chip can be read again.

Known library quirk — SI Code Number truncation:
  The sportident Python library reads only 8 bits of the 12-bit CN (Code Number)
  field in each punch record. If your SI stations are programmed with numbers
  above 255 (e.g. 256–511), the library returns (actual_code - 256). The app
  corrects this via the si_cn_offset setting (default 256) applied in app.py
  before punches are stored.

Known library quirk — no context manager support:
  SIReaderReadout does not implement __exit__, so it cannot be used with `with`.
  We open it manually and call reader.disconnect() in a finally block instead.
"""
import threading
import time
import logging
from datetime import datetime
from typing import Callable

log = logging.getLogger(__name__)

# Type aliases for the two callback signatures.
# ReadCallback:  called when a chip has been fully read and processed.
# EventCallback: called with a human-readable status string for the dashboard log.
ReadCallback  = Callable[[int, list[tuple[int, datetime]], "datetime | None", "datetime | None"], None]
EventCallback = Callable[[str], None]


class SIReader(threading.Thread):
    """
    Background daemon thread that manages the SI readout station connection.

    Runs as a daemon so it is automatically killed when the main process exits.
    Call stop() to request a clean shutdown (drains the current poll cycle first).
    """

    def __init__(
        self,
        port: str,
        on_read: ReadCallback,
        on_event: EventCallback | None = None,
        daemon: bool = True,
    ):
        """
        port      — COM port the SI station is connected to (e.g. "COM4").
        on_read   — Called with (si_chip, punches, finish_time) after each successful read.
        on_event  — Optional callback for human-readable status messages shown on dashboard.
        """
        super().__init__(daemon=daemon)
        self.port     = port
        self.on_read  = on_read
        # Default to a no-op so we never need to guard on_event calls.
        self.on_event = on_event or (lambda msg: None)
        self._stop_event = threading.Event()

    def run(self):
        """Main thread body — opens the station and polls until stop() is called."""
        try:
            from sportident import SIReaderReadout, SIReader as _SI, SIReaderException
        except ImportError:
            log.error("sportident package not installed — SI reading disabled")
            self.on_event("FATAL: sportident package not installed")
            return

        self.on_event(f"Opening SI reader on {self.port}")
        log.info("Opening SI reader on %s", self.port)

        reader = None
        try:
            # Open the serial connection to the SI station.
            # SIReaderReadout does NOT support the context manager protocol (__exit__
            # is missing in the installed version), so we open manually and use finally.
            reader = SIReaderReadout(self.port)

            # Put the station into M_READOUT mode. In this mode:
            #   - The station waits for a chip to be inserted.
            #   - It transmits chip data to the computer when a chip is detected.
            #   - poll_sicard() can detect card presence via serial buffer activity.
            # We check the current mode first to avoid unnecessarily resetting a
            # station that's already configured — some firmware versions reset
            # their internal state when set_operating_mode is called redundantly.
            try:
                current_mode = reader.get_station_mode()
                if current_mode != _SI.M_READOUT:
                    reader.set_operating_mode(_SI.M_READOUT)
                    self.on_event("Station mode set to READOUT")
                    log.info("SI station switched to readout mode on %s", self.port)
                else:
                    self.on_event("Station already in READOUT mode")
                    log.info("SI station already in readout mode on %s", self.port)
            except AttributeError:
                # Older library builds don't have get_station_mode — just set it.
                try:
                    reader.set_operating_mode(_SI.M_READOUT)
                    self.on_event("Station mode set to READOUT (forced)")
                    log.info("SI station in readout mode on %s", self.port)
                except Exception as exc:
                    # Some stations report "Unsupported mode" if already correct.
                    # Log a warning and continue — the station usually still works.
                    self.on_event(f"WARNING: Could not set readout mode: {exc}")
                    log.warning("Could not set readout mode: %s", exc)
            except Exception as exc:
                self.on_event(f"WARNING: Mode check failed: {exc} — continuing anyway")
                log.warning("Could not check/set readout mode: %s", exc)

            self.on_event("Polling for cards...")
            last_sicard = None   # last card number we processed
            poll_errors = 0      # consecutive poll failures (for throttling error logs)

            while not self._stop_event.is_set():

                # poll_sicard() checks the serial buffer for incoming bytes.
                # Returns True on state change (card inserted OR card removed).
                # It takes NO arguments — a common mistake is passing a callback here.
                try:
                    changed = reader.poll_sicard()
                    poll_errors = 0   # reset on success
                except Exception as exc:
                    poll_errors += 1
                    # Log first 3 errors immediately, then throttle to every 20th
                    # to avoid flooding the log if the connection drops.
                    if poll_errors <= 3 or poll_errors % 20 == 0:
                        self.on_event(f"ERROR: Poll error #{poll_errors}: {exc}")
                        log.warning("SI poll error: %s", exc)
                    time.sleep(0.5)
                    continue

                if changed:
                    if reader.sicard is not None:
                        # A chip was inserted. Only process if it's a NEW chip —
                        # the station can fire a second 'changed' event for the same
                        # chip while it's still in the reader.
                        if reader.sicard != last_sicard:
                            last_sicard = reader.sicard
                            self.on_event(f"Card detected: {reader.sicard} — reading...")
                            log.info("SI card detected: %s", reader.sicard)

                            # Download all punch records from the chip.
                            # This is a separate step from poll_sicard() — poll just
                            # detects presence, read_sicard() actually fetches data.
                            try:
                                card_data = reader.read_sicard()
                                self.on_event("Card data downloaded OK")
                                log.info("SI card data downloaded")
                            except Exception as exc:
                                self.on_event(f"ERROR reading card data: {exc}")
                                log.exception("Failed to read SI card data: %s", exc)
                                continue  # skip ack and processing for this card

                            # Send the ACK byte. This is what triggers the station's BEEP,
                            # confirming to the competitor their chip was read successfully.
                            # Must be called AFTER read_sicard(), not before.
                            try:
                                reader.ack_sicard()
                                self.on_event("ACK sent (station should beep)")
                                log.info("SI ack sent")
                            except Exception as exc:
                                # Not fatal — we still have the data; just no beep.
                                self.on_event(f"WARNING: ACK failed (no beep): {exc}")
                                log.warning("SI ack failed: %s", exc)

                            # Parse and forward the card data to the app callback.
                            self._process(card_data)
                    else:
                        # Card was removed — reset so the same chip can be read again.
                        last_sicard = None
                        self.on_event("Card removed")

                # 50 ms poll interval keeps CPU usage negligible while still being
                # responsive enough for real-time results at a race finish.
                self._stop_event.wait(0.05)

        except Exception as exc:
            # Catch-all for connection failures (wrong port, USB unplugged, etc.).
            msg = f"SI reader unavailable on {self.port}: {exc}"
            log.warning("%s — check USB connection.", msg)
            self.on_event(f"FATAL: {msg}")
        finally:
            # Always attempt a clean disconnect, even if an exception occurred.
            if reader is not None:
                try:
                    reader.disconnect()
                except Exception:
                    pass

    def _process(self, card_data: dict):
        """
        Parse the raw card_data dict from read_sicard() and invoke on_read.

        card_data keys: card_number, start, finish, check, clear, punches
          punches = list of (cn, datetime) tuples where cn is the station Code Number.

        Note: the library returns only 8 bits of the 12-bit CN field. The caller
        (on_chip_read in app.py) applies the si_cn_offset correction before storing.
        """
        si_chip = card_data.get("card_number")
        if not si_chip:
            log.warning("Card read returned no card_number — skipping")
            return

        # Build the punch list. Each element is (station_code, datetime).
        # We filter out any entries missing either value to avoid DB errors.
        raw_punches: list[tuple] = card_data.get("punches", [])
        punches: list[tuple[int, datetime]] = []
        for code, t in raw_punches:
            if code and t:
                punches.append((int(code), _to_datetime(t)))

        # The finish and start times come from dedicated slots on the chip,
        # separate from the intermediate control punches list.
        finish_raw   = card_data.get("finish")
        finish_time  = _to_datetime(finish_raw)  if finish_raw  else None
        start_raw    = card_data.get("start")
        chip_start   = _to_datetime(start_raw)   if start_raw   else None

        log.info("Read chip %s — %d punches, start=%s, finish=%s",
                 si_chip, len(punches), chip_start, finish_time)
        try:
            self.on_read(int(si_chip), punches, finish_time, chip_start)
        except Exception:
            log.exception("Error in chip-read callback")

    def stop(self):
        """Signal the poll loop to exit cleanly after the current iteration."""
        self._stop_event.set()


def _to_datetime(raw) -> datetime:
    """
    Normalise whatever the sportident library gives us for a time value into a datetime.

    The library may return datetime objects, time objects (no date), or occasionally
    raw values. We convert time-only objects by combining with today's date.
    """
    if isinstance(raw, datetime):
        return raw
    from datetime import date, time as dtime
    if isinstance(raw, dtime):
        # time-only (no date) — combine with today so we get a full datetime.
        return datetime.combine(date.today(), raw)
    # Fallback: return as-is and let the caller handle it.
    return raw


def list_ports() -> list[str]:
    """Return a list of available COM port names for the port-picker UI."""
    try:
        import serial.tools.list_ports  # type: ignore
        return [p.device for p in serial.tools.list_ports.comports()]
    except ImportError:
        # pyserial not installed — return empty list so the UI shows a graceful message.
        return []
