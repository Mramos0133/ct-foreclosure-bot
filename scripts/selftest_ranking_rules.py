"""Regression guard for the bucket-priority order and the debt floor.

Both rules here are silent-failure shaped: a wrong answer produces a
plausible-looking bucket rather than an error, which is how a case with
judgment entered, a non-appearing defendant and 50% equity
(HHD-CV-25-6203191-S) sat in COLD, and how an "Updated debt" of $2.00
against a $470,000 appraisal reached the equity ratio.

  python3 scripts/selftest_ranking_rules.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ct_foreclosure_bot.lead_ranking import (
    COLD_RATIO, SHORT_SALE_RATIO, equity_bucket_override, short_sale_ratio,
)
from ct_foreclosure_bot.worksheet import MIN_PLAUSIBLE_DEBT, _plausible_debt

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILURES.append(name)


def bucket_order(judgment_hot: bool, continuances: int) -> str:
    """The tail of decide_bucket(), with the distress rules already missed.

    Mirrors the real precedence: judgment/non-appearing outranks the
    continuance demotion. Kept in sync by the ordering assertions below,
    which read decide_bucket's source directly.
    """
    if judgment_hot:
        return "HOT"
    if continuances >= 2:
        return "COLD"
    return "UNCLASSIFIED"


def main() -> int:
    print("ranking rule self-test")

    # --- ordering: judgment/non-appearing must beat the continuance demotion
    check("judgment+non-appearing wins over continuances",
          bucket_order(True, 7) == "HOT", bucket_order(True, 7))
    check("continuances still demote when judgment rule unmet",
          bucket_order(False, 7) == "COLD", bucket_order(False, 7))
    check("no judgment, few continuances -> unclassified",
          bucket_order(False, 1) == "UNCLASSIFIED", bucket_order(False, 1))

    # The ordering lives in source, so assert it there too -- the helper
    # above would happily keep passing if decide_bucket were reordered back.
    src = (Path(__file__).resolve().parent.parent
           / "ct_foreclosure_bot" / "lead_ranking.py").read_text()
    tail = src.split("elif bankruptcy_stay_reopened:", 1)[-1]
    judg_at = tail.find("ranking.judgment_granted")
    cont_at = tail.find("ranking.continuance_count >= 2")
    check("decide_bucket checks judgment BEFORE continuances",
          -1 < judg_at < cont_at, f"judgment@{judg_at} continuance@{cont_at}")

    # --- equity override still outranks everything, so promotion is safe
    no_equity = short_sale_ratio(400_000.0, 400_000.0, 0.0)          # 100%
    check("equity override forces short sale despite judgment HOT",
          equity_bucket_override(no_equity) == "POTENTIAL_SHORT_SALE",
          str(equity_bucket_override(no_equity)))
    # COLD_RATIO was raised to 0.85 to match SHORT_SALE_RATIO, which empties
    # the equity-COLD band. 80% must now fall through to the distress bucket
    # rather than being forced COLD -- that is the whole point of the change.
    thin = short_sale_ratio(320_000.0, 400_000.0, 0.0)               # 80%
    check("80% no longer forced COLD (band emptied)",
          equity_bucket_override(thin) is None, str(equity_bucket_override(thin)))
    check("COLD_RATIO is not below SHORT_SALE_RATIO",
          COLD_RATIO >= SHORT_SALE_RATIO,
          f"COLD {COLD_RATIO} < SHORT_SALE {SHORT_SALE_RATIO} would resurrect the band silently")
    just_over = short_sale_ratio(860_000.0, 1_000_000.0, 0.0)        # 86%
    check("above 85% still forces short sale",
          equity_bucket_override(just_over) == "POTENTIAL_SHORT_SALE",
          str(equity_bucket_override(just_over)))
    good = short_sale_ratio(150_000.0, 300_000.0, None)              # 50%, the real case
    check("real equity does not force a bucket",
          equity_bucket_override(good) is None, str(equity_bucket_override(good)))
    check("unknown debt does not force a bucket",
          equity_bucket_override(short_sale_ratio(None, 300_000.0, None)) is None)

    # --- debt plausibility floor
    for raw, want, label in [
        (2.00, None, "$2.00 reading rejected"),
        (18.20, None, "$18.20 reading rejected"),
        (999.99, None, "just under the floor rejected"),
        (1_000.0, 1_000.0, "exactly the floor kept"),
        (9_365.0, 9_365.0, "small condo-association lien kept"),
        (149_294.25, 149_294.25, "ordinary mortgage debt kept"),
        (None, None, "missing stays missing"),
    ]:
        got = _plausible_debt(raw)
        check(f"debt floor: {label}", got == want, f"{raw} -> {got}, wanted {want}")

    # A cleared debt must not silently read as "no debt, huge equity": it
    # has to make the ratio unknown so no bucket is forced either way.
    cleared = short_sale_ratio(_plausible_debt(2.00), 470_000.0, None)
    check("cleared debt yields an unknown ratio, not 0%", cleared is None, str(cleared))

    # --- HOT breakdown sheets must stay in step with the rule set
    from ct_foreclosure_bot.lead_ranking import HOT_RULES
    codes = [c for c, _t, _d in HOT_RULES]
    check("five HOT rules defined", codes == ["A", "B", "C", "D", "E"], str(codes))
    titles = [t for _c, t, _d in HOT_RULES]
    check("sheet titles within Excel's 31-char limit",
          all(len(t) <= 31 for t in titles),
          str([t for t in titles if len(t) > 31]))
    bad = [t for t in titles if any(ch in t for ch in r":\\/?*[]")]
    check("sheet titles free of characters Excel rejects", not bad, str(bad))

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nall ranking rules pass")
    return 1 if FAILURES else 0


sys.exit(main())
