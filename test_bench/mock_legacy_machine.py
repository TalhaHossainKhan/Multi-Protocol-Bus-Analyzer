#!/usr/bin/env python3
"""Mock legacy DAQ machine — streams live data into a CSV file.

Simulates a piece of test equipment that continuously appends one row of
sensor data every 100 ms to ``live_test.csv``.  The DAQ app's
Live File Watcher can open this file while the script is running to see
a live signal, exercising every edge-case the worker is designed to handle:

  * Mid-write OS file-locking (explicit flush causes the OS to briefly
    hold an exclusive lock on Windows while the kernel flushes the buffer).
  * Partial lines — the script writes the line in two halves with a tiny
    sleep between them (``--partial`` flag) to test the leftover accumulator.
  * Rapid appends — ``--fast`` flag drops the interval to 10 ms so the
    file grows faster than one UI tick, exercising the multi-row drain path.

Usage
-----
    python test_bench/mock_legacy_machine.py             # default 100 ms
    python test_bench/mock_legacy_machine.py --fast      # 10 ms
    python test_bench/mock_legacy_machine.py --partial   # partial-line mode
    python test_bench/mock_legacy_machine.py --out /tmp/my_data.csv

The script writes to the path printed at startup.  Open the DAQ app,
click  "+ Add Source → Live File Watcher…", and pick that file.
Press Ctrl-C to stop.
"""

import argparse
import csv
import math
import os
import sys
import time


_HEADERS = ["Time_s", "Channel_1", "Channel_2", "Channel_3"]

_DEFAULT_OUT = os.path.join(os.path.dirname(__file__), "live_test.csv")


def _row(t: float):
    ch1 = math.sin(2 * math.pi * 0.5 * t) * 50.0 + 50.0
    ch2 = math.cos(2 * math.pi * 0.25 * t) * 25.0 + 75.0
    ch3 = (math.sin(2 * math.pi * 1.0 * t) + 1.0) * 512.0
    return [round(t, 4), round(ch1, 5), round(ch2, 5), round(ch3, 3)]


def run(out_path: str, interval_s: float, partial_mode: bool) -> None:
    print(f"Writing to: {os.path.abspath(out_path)}", flush=True)
    print(f"Rate: {1.0 / interval_s:.0f} rows/s  |  partial mode: {partial_mode}")
    print("Press Ctrl-C to stop.\n")

    with open(out_path, "w", newline="", buffering=1) as f:
        writer = csv.writer(f)
        writer.writerow(_HEADERS)
        f.flush()   # make the header immediately visible to the watcher

        t = 0.0
        row_count = 0

        try:
            while True:
                row = _row(t)
                line = ",".join(str(v) for v in row)

                if partial_mode:
                    # Write the first half of the line, flush (simulates a
                    # machine that buffers stdout line-by-line and lets the OS
                    # catch a reader mid-write).
                    half = len(line) // 2
                    f.write(line[:half])
                    f.flush()
                    time.sleep(0.002)   # 2 ms pause mid-line
                    f.write(line[half:] + "\n")
                else:
                    f.write(line + "\n")

                # Explicit flush on EVERY write — this is the critical step
                # that makes the data immediately visible on disk and also
                # briefly exercises OS file-locking on Windows/macOS.
                f.flush()

                row_count += 1
                if row_count % 50 == 0:
                    print(
                        f"  {row_count} rows written  |  t={t:.1f}s",
                        flush=True,
                    )

                t += interval_s
                time.sleep(interval_s)

        except KeyboardInterrupt:
            print(f"\nStopped after {row_count} rows.")


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--out", default=_DEFAULT_OUT,
        help=f"Output CSV path (default: {_DEFAULT_OUT})",
    )
    p.add_argument(
        "--fast", action="store_true",
        help="Append a row every 10 ms instead of 100 ms",
    )
    p.add_argument(
        "--partial", action="store_true",
        help="Write each line in two halves to simulate a mid-write read",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    interval = 0.010 if args.fast else 0.100
    run(args.out, interval, args.partial)
