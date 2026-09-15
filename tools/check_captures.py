"""Re-check debug_captures/ against the current detection pipeline.

This is the same pass the app runs on every launch (see main.py's
CAPTURE_CHECK_LEDGER and check_pending_captures) exposed as a standalone
command, for when you want to grade the backlog without starting the app
— after changing the classifier or rebuilding digit templates, say.

Usage:
    python tools/check_captures.py              # check what the ledger hasn't judged yet
    python tools/check_captures.py --recheck    # forget all verdicts and re-grade everything
    python tools/check_captures.py --summary    # print the ledger's current totals, check nothing

Equivalent to `python main.py --check-captures`, which exists so the
shipped exe can do this too (tools/ is not bundled into it).

Only captures stamped on or after main.py's CAPTURE_CHECK_CUTOFF are
considered; older ones predate the current pipeline and would report
failures nobody intends to fix.
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import main as m


def print_ledger_summary():
    ledger = m._load_check_ledger()
    checked = ledger.get("checked", {})
    hits = [e for e in checked.values() if "expected" in e]
    misses = [e for e in checked.values() if "recovered" in e]
    correct = sum(1 for e in hits if e.get("ok"))
    recovered = sum(1 for e in misses if e.get("recovered"))
    print(f"  Ledger: {m.CAPTURE_CHECK_LEDGER}")
    print(f"  Judged: {len(checked)} capture(s) since cutoff {ledger.get('cutoff')}")
    if hits:
        print(f"  Confirmed hits: {correct}/{len(hits)} still read correctly")
    if misses:
        print(f"  Misses: {recovered}/{len(misses)} now read as a +<number>")
    pending = len(m._pending_captures(ledger))
    print(f"  Pending: {pending} capture(s) not yet judged")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recheck", action="store_true",
                        help="clear every recorded verdict first, so the whole backlog is re-graded")
    parser.add_argument("--summary", action="store_true",
                        help="print what the ledger already says and exit without checking anything")
    args = parser.parse_args()

    if args.summary:
        print_ledger_summary()
        return

    if args.recheck:
        # Only the verdicts are dropped; the captures themselves are never touched.
        ledger = m._load_check_ledger()
        ledger["checked"] = {}
        m._save_check_ledger(ledger)
        print("  Cleared previous verdicts — re-grading the full backlog.")

    def progress(done, total):
        print(f"\r  Re-checking captures: {done}/{total}", end="", flush=True)

    summary = m.check_pending_captures(on_progress=progress)
    if summary["checked"]:                      # only if a progress line was actually drawn
        print("\r" + " " * 40 + "\r", end="")
    print(m.format_check_summary(summary))


if __name__ == "__main__":
    main()
