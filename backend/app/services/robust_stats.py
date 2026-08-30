"""Robust statistics for anomaly detection.

Pure functions over a list of numbers: no database, no configuration, no
company. Kept separate from the engine so the arithmetic can be read, reviewed
and unit-tested on its own, and so it is obvious that nothing here can consult
a model.

Why robust rather than mean and standard deviation: a single spike in the
reference window inflates the standard deviation enough to hide the *next*
spike, and a single outage drags the mean below every normal day. The median and
the median absolute deviation both tolerate up to half the window being
contaminated, which is the realistic condition for KPI history.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Scales the MAD to be a consistent estimator of the standard deviation for
#: normally distributed data (1 / 0.75-quantile of the standard normal, 1.4826;
#: the reciprocal 0.6745 is folded into the numerator here as in Iglewicz &
#: Hoaglin's formulation of the modified z-score).
MODIFIED_Z_CONSTANT = 0.6745

#: Equivalent scaling for the *mean* absolute deviation, used only when the MAD
#: collapses to zero. sqrt(pi / 2).
MEAN_AD_SCALE = 1.253314

#: Iglewicz & Hoaglin's suggested outlier cut-off, used when a KPI's governance
#: record does not state its own statistical rule.
DEFAULT_Z_THRESHOLD = 3.5

class DispersionBasis:
    """Which measure of spread the z-score was computed from."""

    MAD = "MAD"
    MEAN_ABSOLUTE_DEVIATION = "MEAN_ABSOLUTE_DEVIATION"
    NONE = "NONE"


def median(values: Sequence[float]) -> float:
    """Middle value; mean of the two middle values for an even count."""

    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2.0


def median_absolute_deviation(values: Sequence[float], center: float | None = None) -> float:
    """Median of the absolute distances from the centre."""

    if not values:
        raise ValueError("median_absolute_deviation requires at least one value")
    mid = median(values) if center is None else center
    return median([abs(float(value) - mid) for value in values])


def mean_absolute_deviation(values: Sequence[float], center: float | None = None) -> float:
    if not values:
        raise ValueError("mean_absolute_deviation requires at least one value")
    mid = median(values) if center is None else center
    return sum(abs(float(value) - mid) for value in values) / len(values)


@dataclass(frozen=True)
class Dispersion:
    """The spread of the reference set, and which measure produced it."""

    value: float
    basis: str
    mad: float
    mean_ad: float
    note: str | None = None


def dispersion_of(values: Sequence[float], center: float) -> Dispersion:
    """Spread of ``values`` about ``center``, with the zero-MAD guard.

    A zero MAD is common and not an error: a KPI that sits at exactly the same
    value on more than half its comparable days has one. Dividing by it would
    make every non-identical actual infinitely abnormal, so the fallback ladder
    is explicit:

    1. the MAD, when it is non-zero;
    2. otherwise the scaled mean absolute deviation, which survives when only
       *some* of the window is identical;
    3. otherwise no dispersion at all -- the window is a single repeated value,
       and the caller must decide on business tolerance alone rather than
       manufacture a z-score.
    """

    mad = median_absolute_deviation(values, center)
    mean_ad = mean_absolute_deviation(values, center)

    if mad > 0:
        return Dispersion(value=mad, basis=DispersionBasis.MAD, mad=mad, mean_ad=mean_ad)
    if mean_ad > 0:
        return Dispersion(
            value=mean_ad * MEAN_AD_SCALE,
            basis=DispersionBasis.MEAN_ABSOLUTE_DEVIATION,
            mad=mad,
            mean_ad=mean_ad,
            note=(
                "The median absolute deviation was zero, so the scaled mean "
                "absolute deviation was used instead."
            ),
        )
    return Dispersion(
        value=0.0,
        basis=DispersionBasis.NONE,
        mad=mad,
        mean_ad=mean_ad,
        note=(
            "Every comparable value was identical, so the history carries no "
            "measurable spread. Business tolerance decides on its own."
        ),
    )


@dataclass(frozen=True)
class RobustScore:
    center: float
    dispersion: Dispersion
    score: float | None
    note: str | None = None

    @property
    def mad(self) -> float:
        return self.dispersion.mad

    @property
    def basis(self) -> str:
        return self.dispersion.basis


def modified_z_score(actual: float, values: Sequence[float], center: float | None = None) -> RobustScore:
    """Iglewicz & Hoaglin modified z-score of ``actual`` against ``values``.

    ``score = 0.6745 * (actual - median) / dispersion``.

    Returns ``score=None`` -- never a fabricated number -- when the reference
    window has no measurable spread and the actual differs from it. The caller
    is expected to fall back to business tolerance and to say so.
    """

    if not values:
        raise ValueError("modified_z_score requires at least one reference value")
    mid = median(values) if center is None else center
    spread = dispersion_of(values, mid)

    if spread.value > 0:
        score = MODIFIED_Z_CONSTANT * (float(actual) - mid) / spread.value
        return RobustScore(center=mid, dispersion=spread, score=score, note=spread.note)

    if float(actual) == mid:
        return RobustScore(
            center=mid,
            dispersion=spread,
            score=0.0,
            note=(
                "Every comparable value was identical and the actual matches "
                "them exactly, so the deviation is zero by inspection."
            ),
        )
    return RobustScore(center=mid, dispersion=spread, score=None, note=spread.note)


def parse_z_threshold(statistical_rule: str | None) -> tuple[float, str]:
    """Read a z-score cut-off out of a KPI's governed statistical rule.

    The rule is free text owned by the business (``"z_score>2"``,
    ``"modified z >= 3.5"``). Anything unparseable falls back to the documented
    default rather than guessing, and the returned description says which
    happened so the result can explain itself.
    """

    if statistical_rule:
        digits = ""
        seen_dot = False
        collecting = False
        for char in statistical_rule:
            if char.isdigit():
                digits += char
                collecting = True
            elif char == "." and collecting and not seen_dot:
                digits += char
                seen_dot = True
            elif collecting:
                break
        if digits.strip("."):
            try:
                value = float(digits)
            except ValueError:  # pragma: no cover - guarded by the scan above
                value = 0.0
            if value > 0:
                return value, f"threshold {value:g} from the KPI's statistical rule"
    return (
        DEFAULT_Z_THRESHOLD,
        f"default threshold {DEFAULT_Z_THRESHOLD:g} (no statistical rule on the KPI)",
    )
