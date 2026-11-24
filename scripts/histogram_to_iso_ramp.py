import json
import os.path
import sys
import numpy as np


def dvr_alpha_params_from_hist(
    hist: np.ndarray,
    ramp_fraction: float = 0.5,
) -> tuple[float, float, float]:
    """
    Compute (iso_value, ramp_start, ramp_end) directly from a 1D uint16 histogram.

    Parameters:
        hist: 1D array of bin counts (e.g., length 65536 from np.bincount).
        ramp_fraction: Ramp width factor in (0, 1] relative to class means [mu1, mu2].

    Returns:
        (iso_value, ramp_start, ramp_end) as floats.
    """
    h = np.array(hist, dtype=np.float64, copy=True)

    total = h.sum()
    if total == 0:
        return 0.0, 0.0, 0.0

    bins = np.arange(h.size, dtype=np.float64)
    w1 = np.cumsum(h[:-1])
    w2 = total - w1

    valid = (w1 > 0) & (w2 > 0)
    if not np.any(valid):
        val = float(np.flatnonzero(h)[0])
        return val, val, val

    cumsum_i = np.cumsum(h * bins)
    mu1 = cumsum_i[:-1][valid] / w1[valid]
    mu2 = (cumsum_i[-1] - cumsum_i[:-1][valid]) / w2[valid]

    var_between = w1[valid] * w2[valid] * (mu1 - mu2) ** 2
    best_pos = int(np.argmax(var_between))

    iso_value = float(np.flatnonzero(valid)[best_pos])
    bg_mean = float(mu1[best_pos])
    fg_mean = float(mu2[best_pos])

    ramp_start = iso_value - ramp_fraction * (iso_value - bg_mean)
    ramp_end = iso_value + ramp_fraction * (fg_mean - iso_value)

    return iso_value, ramp_start, ramp_end


def main(path):
    with open(os.path.join(path,  "histogram.json"), "r") as f:
        hist = json.load(f)
        hist = hist["counts"]

    iso_value, ramp_start, ramp_end = dvr_alpha_params_from_hist(hist)

    with open(os.path.join(path, "volume.json"), "r") as f:
        data = json.load(f)

    with open(os.path.join(path, "volume.json"), "w") as f:
        data["isoValue"] = iso_value/1024
        data["ramp"] = [ramp_start/1024, ramp_end/1024]
        json.dump(data, f, indent=2)


if __name__ == "__main__":
    main(sys.argv[1])
