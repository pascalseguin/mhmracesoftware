"""
ESC/POS thermal receipt printer for MHM results.

Prints a formatted result slip when a chip is read at the finish line.
Uses the python-escpos library with Win32Raw for direct Windows printer access
(no driver print queue — sends raw ESC/POS bytes directly to the printer).

If printing fails for any reason (no printer, library missing, paper out, etc.),
the receipt is saved as a .txt file in the receipts/ data directory so results
are never lost.

Receipt layout (42 characters wide, 58mm thermal paper):
  ==========================================
           MEDICINE HAT MASSACRE
  ==========================================

  Team :                    Mountain Goats
  Bib  :                               #42
  Course:                           8-Hour
  Rank  :                               #3

  Total Score :                         85
  Reductions  :                        -10
    (10pts overtime (1min))
  Time        :                  04:23:11

  ------------------------------------------
  Control              Pts   Time
  ------------------------------------------
  CP A                  10   04:09:24
  CP B                  15   04:35:55
  ...
  ------------------------------------------
  TOTAL                 85
  ==========================================
            2026-05-09  10:41
         Good work out there!
  ==========================================
"""
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from shared.models import ScoredResult

log = logging.getLogger(__name__)

LINE_WIDTH        = 42   # 58mm thermal paper at default font = 42 chars/line
DEFAULT_RACE_NAME = "MEDICINE HAT MASSACRE"


# ── Formatting helpers ────────────────────────────────────────────────────────

def _center(text: str) -> str:
    """Centre text within LINE_WIDTH characters."""
    return text.center(LINE_WIDTH)


def _divider(char: str = "-") -> str:
    """Full-width divider line."""
    return char * LINE_WIDTH


def _row(label: str, value: str, label_w: int = 28) -> str:
    """Left-align label, right-align value, total LINE_WIDTH characters."""
    return f"{label:<{label_w}}{value:>{LINE_WIDTH - label_w}}"


def _punch_time_map(result: ScoredResult) -> dict[int, datetime]:
    """
    Build a map of si_code → first punch time for this result.

    Matches the scoring engine's rule: only the first punch per control counts.
    Used to print the time next to each visited control on the receipt.
    """
    times: dict[int, datetime] = {}
    for p in sorted(result.entry.punches, key=lambda p: p.punch_time):
        if p.si_code not in times:
            times[p.si_code] = p.punch_time
    return times


# ── Receipt content builder ───────────────────────────────────────────────────

def build_receipt_lines(
    result: ScoredResult,
    rank: int | None = None,
    race_name: str = DEFAULT_RACE_NAME,
) -> list[str]:
    """
    Build the receipt as a list of plain-text lines.

    Separated from print_receipt() so it can be tested without a printer,
    and so the fallback file-save path uses the same formatted content.
    """
    r = result
    punch_times = _punch_time_map(r)
    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append(_divider("="))
    lines.append(_center(race_name))
    lines.append(_divider("="))
    lines.append("")

    lines.append(_row("Team :", r.racer.name[:LINE_WIDTH - 7]))
    if r.racer.bib_number:
        lines.append(_row("Bib  :", f"#{r.racer.bib_number}"))
    lines.append(_row("Course:", r.course.name[:LINE_WIDTH - 8]))
    if rank:
        lines.append(_row("Rank  :", f"#{rank}"))
    lines.append("")

    # ── Score summary ─────────────────────────────────────────────────────────
    lines.append(_row("Total Score :", str(r.total_points)))

    # Show reductions (overtime + negative adjustments) as a single line with breakdown.
    reductions = r.overtime_penalty - min(0, r.total_adjustments)
    if reductions:
        parts = []
        if r.overtime_penalty:
            parts.append(f"{r.overtime_penalty}pts overtime ({r.overtime_minutes}min)")
        neg_adj = -min(0, r.total_adjustments)
        if neg_adj:
            parts.append(f"{neg_adj}pts penalties")
        lines.append(_row("Reductions  :", str(-reductions)))
        lines.append(f"  ({', '.join(parts)})")
    else:
        lines.append(_row("Reductions  :", "none"))

    # Only show bonuses line if there were positive adjustments.
    if r.total_adjustments > 0:
        lines.append(_row("Bonuses     :", f"+{r.total_adjustments}"))

    if r.elapsed_seconds is not None:
        h, rem = divmod(r.elapsed_seconds, 3600)
        m, s   = divmod(rem, 60)
        lines.append(_row("Time        :", f"{h:02d}:{m:02d}:{s:02d}"))

    lines.append("")

    # ── Body: visited controls ────────────────────────────────────────────────
    lines.append(_divider())
    lines.append(f"{'Control':<20} {'Pts':>5}  {'Time':<8}")
    lines.append(_divider())

    if r.controls_visited:
        for cp in r.controls_visited:
            pt       = punch_times.get(cp.si_code)
            time_str = pt.strftime("%H:%M:%S") if pt else "       "
            name     = (cp.name or str(cp.si_code))[:20]   # truncate long names
            lines.append(f"{name:<20} {cp.points:>5}  {time_str}")
    else:
        lines.append("  (no controls visited)")

    # Flag missed mandatory controls prominently — these zeroed the raw score.
    if r.controls_missed_mandatory:
        lines.append("")
        lines.append("MISSED MANDATORY:")
        for cp in r.controls_missed_mandatory:
            lines.append(f"  !! {(cp.name or str(cp.si_code))[:36]}")

    if r.order_violation:
        lines.append("  !! Order violation")

    lines.append(_divider())
    lines.append(f"{'TOTAL':<20} {r.total_points:>5}")

    # ── Footer ────────────────────────────────────────────────────────────────
    lines.append("")
    lines.append(_divider("="))
    lines.append(_center(datetime.now().strftime("%Y-%m-%d  %H:%M")))
    lines.append(_center("Good work out there!"))
    lines.append(_divider("="))
    lines.append("")
    lines.append("")   # extra blank lines to feed paper past the cutter
    return lines


# ── Low-level Windows raw print ───────────────────────────────────────────────

_ESC_INIT   = b"\x1b\x40"          # ESC @ — reset/initialize printer
_ESC_FONT_B = b"\x1b\x4d\x01"     # ESC M 1 — Font B (9-dot wide chars, 42 cols on 58mm paper)
_ESC_CENTER = b"\x1b\x61\x01"     # ESC a 1 — center alignment
_ESC_CUT    = b"\x1d\x56\x41\x00" # GS V 65 0 — partial cut (widely compatible)
_ESC_FEED   = b"\n\n\n"           # extra line feed so text clears the cutter
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# C# compiled at runtime by PowerShell's Add-Type.
# Uses .NET IntPtr (automatically pointer-sized) for HANDLE so it works on
# both 32-bit and 64-bit without any ctypes marshalling issues.
_PRINT_CS = """\
using System;
using System.IO;
using System.Runtime.InteropServices;
public class MhmRawPrint {
    [DllImport("winspool.drv", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool OpenPrinterW(string szPrinter, ref IntPtr hPrinter, IntPtr pd);
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct DOC_INFO_1 {
        public string pDocName;
        public string pOutputFile;
        public string pDataType;
    }
    [DllImport("winspool.drv", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern int StartDocPrinterW(IntPtr hPrinter, int Level, IntPtr pDocInfo);
    [DllImport("winspool.drv", SetLastError=true)]
    public static extern bool StartPagePrinter(IntPtr hPrinter);
    [DllImport("winspool.drv", SetLastError=true)]
    public static extern bool WritePrinter(IntPtr hPrinter, byte[] pBuf, int cbBuf, ref int pcWritten);
    [DllImport("winspool.drv", SetLastError=true)]
    public static extern bool EndPagePrinter(IntPtr hPrinter);
    [DllImport("winspool.drv", SetLastError=true)]
    public static extern bool EndDocPrinter(IntPtr hPrinter);
    [DllImport("winspool.drv", SetLastError=true)]
    public static extern bool ClosePrinter(IntPtr hPrinter);
    public static void Print(string printer, string file) {
        IntPtr h = IntPtr.Zero;
        if (!OpenPrinterW(printer, ref h, IntPtr.Zero))
            throw new Exception("OpenPrinter failed: " + Marshal.GetLastWin32Error());
        try {
            var di = new DOC_INFO_1 { pDocName="MHM Receipt", pOutputFile=null, pDataType="RAW" };
            IntPtr pdi = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(DOC_INFO_1)));
            int job;
            try {
                Marshal.StructureToPtr(di, pdi, false);
                job = StartDocPrinterW(h, 1, pdi);
            } finally {
                Marshal.DestroyStructure(pdi, typeof(DOC_INFO_1));
                Marshal.FreeHGlobal(pdi);
            }
            if (job == 0)
                throw new Exception("StartDocPrinter failed: " + Marshal.GetLastWin32Error());
            StartPagePrinter(h);
            byte[] data = File.ReadAllBytes(file);
            int written = 0;
            WritePrinter(h, data, data.Length, ref written);
            EndPagePrinter(h);
            EndDocPrinter(h);
        } finally {
            ClosePrinter(h);
        }
    }
}
"""


def _win32_print(printer_name: str, text: str):
    """
    Send raw ESC/POS bytes to a Windows printer.

    Writes the data to a temp file then invokes PowerShell to compile and run
    C# P/Invoke code against winspool.drv. .NET IntPtr is automatically
    pointer-sized, so this works on 32-bit and 64-bit without ctypes issues.
    No pip packages required. Adds ~1 s for C# JIT on first call.
    """
    raw = _ESC_INIT + _ESC_FONT_B + _ESC_CENTER + text.encode("cp437", errors="replace") + _ESC_FEED + _ESC_CUT

    fd_data, data_file = tempfile.mkstemp(suffix=".prn")
    fd_ps,   ps_file   = tempfile.mkstemp(suffix=".ps1")
    try:
        os.write(fd_data, raw)
        os.close(fd_data)
        fd_data = -1

        # Single-quote escape for PowerShell — backslashes are literal in '...'
        safe_printer = printer_name.replace("'", "''")
        safe_path    = data_file.replace("'", "''")

        # Build PS script using string concat so C# braces aren't f-string tokens
        ps_script = (
            "Add-Type -TypeDefinition @'\n"
            + _PRINT_CS
            + "'@ -Language CSharp -ErrorAction Stop\n"
            + "[MhmRawPrint]::Print('" + safe_printer + "', '" + safe_path + "')\n"
        )
        os.write(fd_ps, ps_script.encode("utf-8"))
        os.close(fd_ps)
        fd_ps = -1

        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass",
             "-NoProfile", "-NonInteractive", "-File", ps_file],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            raise OSError(err or "PowerShell raw print failed")
    finally:
        for fd, path in [(fd_data, data_file), (fd_ps, ps_file)]:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(path)
            except OSError:
                pass


# ── Print entry point ─────────────────────────────────────────────────────────

def print_receipt(
    result: ScoredResult,
    rank: int | None = None,
    printer_name: str | None = None,
    race_name: str = DEFAULT_RACE_NAME,
):
    """
    Print a result receipt to a Windows ESC/POS thermal printer.

    Uses PowerShell + C# P/Invoke to send raw ESC/POS bytes to the Windows
    print spooler. No pip packages required. Falls back to saving a .txt file
    in the receipts/ data directory if printing fails for any reason.
    """
    if not printer_name:
        log.warning("print_receipt called with no printer_name — skipping")
        return

    lines = build_receipt_lines(result, rank, race_name=race_name)
    text  = "\n".join(lines) + "\n"

    # ── Method 1: PowerShell + C# P/Invoke ───────────────────────────────────
    try:
        _win32_print(printer_name, text)
        log.info("Receipt printed for %s", result.racer.name)
        return
    except Exception as exc:
        log.warning("Printing failed (%s) — saving receipt to file", exc)

    # ── Method 2: save to file ────────────────────────────────────────────────
    # Named by chip number + time so multiple reads don't overwrite each other.
    from client.utils import data_path
    out   = data_path("receipts")
    out.mkdir(exist_ok=True)
    chip  = result.entry.si_chip or "nochip"
    fname = out / f"{chip}_{datetime.now().strftime('%H%M%S')}.txt"
    fname.write_text(text, encoding="utf-8")
    log.info("Receipt saved to %s", fname)
