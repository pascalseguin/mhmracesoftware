"""
Background sync worker — pushes unsynced local records to the remote results server.

How it works:
  1. SyncWorker runs as a background daemon thread, waking every `interval` seconds.
  2. On each wake: fetches all records from the local DB where synced=0.
  3. POSTs them to the server's /api/sync endpoint as a JSON payload.
  4. On success: the server responds with a dict of {table: [ids_that_were_stored]}.
     We mark those IDs as synced=1 locally so they're not sent again.
  5. On failure: logs a warning and retries next interval. No data is lost.

The remote server is the public-facing results site (e.g. results.mhmassacre.ca).
Syncing is optional — the app works fully offline with no server configured.

Authentication uses a pre-shared API key sent as the X-API-Key header.
"""
import logging
import threading
import time
import requests
from client import database as db

log = logging.getLogger(__name__)


class SyncWorker(threading.Thread):
    """
    Daemon thread that periodically pushes unsynced records to the remote server.

    Runs as a daemon so it is killed automatically when the main process exits.
    Call stop() to request a clean shutdown.
    """

    def __init__(
        self,
        server_url: str,
        api_key: str,
        interval_seconds: int = 30,
        daemon: bool = True,
    ):
        super().__init__(daemon=daemon)
        # Strip trailing slash so we can always safely append /api/sync.
        self.server_url = server_url.rstrip("/")
        self.api_key    = api_key
        self.interval   = interval_seconds
        self._stop      = threading.Event()

    def run(self):
        """
        Wait `interval` seconds, then sync. Repeat until stop() is called.

        Using _stop.wait(interval) instead of time.sleep() means stop() wakes
        the thread immediately rather than waiting for the next sleep to expire.
        """
        while not self._stop.wait(self.interval):
            self._sync()

    def _sync(self):
        """
        Fetch unsynced records and POST them to the server.

        Payload shape:
          {
            "racers":      [...],
            "courses":     [...],
            "entries":     [...],
            "punches":     [...],
            "adjustments": [...]
          }

        Server responds with:
          {
            "racers":      [1, 2, 3],    ← IDs successfully stored
            "punches":     [10, 11, 12],
            ...
          }

        We only mark records as synced if the server confirms it received them.
        """
        payload = db.get_unsynced()

        # Skip the HTTP call if there's nothing new to send.
        if not any(payload.values()):
            return

        try:
            resp = requests.post(
                f"{self.server_url}/api/sync",
                json=payload,
                headers={"X-API-Key": self.api_key},
                timeout=10,   # don't block the app for more than 10 seconds
            )
            resp.raise_for_status()     # raise on 4xx/5xx

            # Server tells us which IDs it stored — mark those as synced locally.
            acked = resp.json()
            for table, ids in acked.items():
                db.mark_synced(table, ids)

            log.info(
                "Synced to server: %s",
                {k: len(v) for k, v in payload.items() if v}
            )

        except Exception as exc:
            # Network issues, server down, wrong API key, etc.
            # We don't raise — just log and retry next interval.
            log.warning("Sync failed (will retry): %s", exc)

    def stop(self):
        """Signal the sync loop to exit after the current iteration completes."""
        self._stop.set()
