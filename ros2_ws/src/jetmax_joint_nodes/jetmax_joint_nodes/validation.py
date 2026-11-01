"""Small strict validators shared by the hardware-free core."""
from math import isfinite
from numbers import Real


def finite(value, name="value"):
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite real number")
    return float(value)


def bounded(value, lower, upper, name="value"):
    value = finite(value, name)
    if not lower <= value <= upper:
        raise ValueError(f"{name} {value} outside [{lower}, {upper}]")
    return value


def integer(value, lower, upper, name="value"):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")
    return value


def vector(values, size, name="vector"):
    try:
        values = tuple(values)
    except TypeError as exc:
        raise ValueError(f"{name} must contain {size} numbers") from exc
    if len(values) != size:
        raise ValueError(f"{name} must contain {size} numbers")
    return tuple(finite(v, f"{name}[{i}]") for i, v in enumerate(values))
