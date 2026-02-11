#!/usr/bin/env python3
"""
Basic signal processing on recorded Muse S EEG data.

Computes:
  1. Frequency band powers (delta, theta, alpha, beta, gamma)
  2. Blink detection from frontal electrodes
  3. Alpha asymmetry (left vs right)
  4. Time-series plots of all the above

Usage:
    python analyze.py                                    # latest session
    python analyze.py muse_data/session_20260210_172440  # specific session
"""

import csv
import os
import sys
from collections import defaultdict

import numpy as np
from scipy.signal import butter, filtfilt, welch, find_peaks
import matplotlib
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt

SAMPLE_RATE = 256

BANDS = {
    "delta": (0.5, 4),
    "theta": (4, 8),
    "alpha": (8, 13),
    "beta":  (13, 30),
    "gamma": (30, 50),
}

BAND_COLORS = {
    "delta": "#9575cd",
    "theta": "#4fc3f7",
    "alpha": "#66bb6a",
    "beta":  "#ffb74d",
    "gamma": "#ef5350",
}

MAIN_CHANNELS = ["TP9", "AF7", "AF8", "TP10"]


def load_eeg(session_dir: str) -> dict[str, np.ndarray]:
    """Load EEG data from session CSV into per-channel arrays."""
    path = os.path.join(session_dir, "eeg.csv")
    if not os.path.exists(path):
        print(f"No eeg.csv in {session_dir}")
        sys.exit(1)

    channels = defaultdict(list)
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ch = row["channel"]
            if ch in MAIN_CHANNELS:
                samples = [float(row[f"s{i}"]) for i in range(12)]
                channels[ch].extend(samples)

    return {ch: np.array(samples) for ch, samples in channels.items()}


def bandpass(data, low, high, fs=SAMPLE_RATE, order=4):
    nyq = fs / 2
    b, a = butter(order, [low / nyq, high / nyq], btype="band")
    return filtfilt(b, a, data)


def compute_band_powers(data, fs=SAMPLE_RATE, window_sec=2.0):
    """Compute power in each frequency band over sliding windows."""
    window_samples = int(fs * window_sec)
    step = window_samples // 2
    n = len(data)

    powers = {band: [] for band in BANDS}
    times = []

    for start in range(0, n - window_samples, step):
        segment = data[start:start + window_samples]
        segment = segment - np.mean(segment)

        freqs, psd = welch(segment, fs=fs, nperseg=min(256, len(segment)))

        for band, (lo, hi) in BANDS.items():
            mask = (freqs >= lo) & (freqs <= hi)
            powers[band].append(np.mean(psd[mask]) if mask.any() else 0)

        times.append((start + window_samples / 2) / fs)

    return np.array(times), {b: np.array(v) for b, v in powers.items()}


def detect_blinks(data, fs=SAMPLE_RATE, threshold_uv=150):
    """Detect blinks as large amplitude spikes in frontal channels."""
    filtered = bandpass(data, 1, 10, fs)
    abs_signal = np.abs(filtered)

    peaks, properties = find_peaks(
        abs_signal,
        height=threshold_uv,
        distance=int(fs * 0.3),
        prominence=threshold_uv * 0.5,
    )

    return peaks, abs_signal


def alpha_asymmetry(left_data, right_data, fs=SAMPLE_RATE, window_sec=2.0):
    """Compute frontal alpha asymmetry: ln(right_alpha) - ln(left_alpha)."""
    times_l, powers_l = compute_band_powers(left_data, fs, window_sec)
    times_r, powers_r = compute_band_powers(right_data, fs, window_sec)

    n = min(len(powers_l["alpha"]), len(powers_r["alpha"]))
    left_alpha = powers_l["alpha"][:n]
    right_alpha = powers_r["alpha"][:n]

    epsilon = 1e-10
    asymmetry = np.log(right_alpha + epsilon) - np.log(left_alpha + epsilon)

    return times_l[:n], asymmetry


def get_latest_session(data_dir="muse_data"):
    if not os.path.isdir(data_dir):
        return None
    sessions = sorted(
        [d for d in os.listdir(data_dir) if d.startswith("session_")],
        reverse=True,
    )
    for s in sessions:
        eeg_path = os.path.join(data_dir, s, "eeg.csv")
        if os.path.exists(eeg_path) and os.path.getsize(eeg_path) > 100:
            return os.path.join(data_dir, s)
    return None


def main():
    if len(sys.argv) > 1:
        session_dir = sys.argv[1]
    else:
        session_dir = get_latest_session()
        if not session_dir:
            print("No session with EEG data found.")
            sys.exit(1)

    print(f"Analyzing: {session_dir}")
    channels = load_eeg(session_dir)

    if not channels:
        print("No EEG data found.")
        sys.exit(1)

    for ch, data in channels.items():
        print(f"  {ch}: {len(data)} samples ({len(data)/SAMPLE_RATE:.1f}s)")

    duration = min(len(d) for d in channels.values()) / SAMPLE_RATE
    print(f"  Duration: {duration:.1f}s")

    # --- Analysis ---
    fig, axes = plt.subplots(5, 1, figsize=(14, 16), constrained_layout=True)
    fig.suptitle(f"Muse S EEG Analysis — {os.path.basename(session_dir)}", fontsize=14)

    # 1. Raw EEG waveforms
    ax = axes[0]
    t = np.arange(len(channels["TP9"])) / SAMPLE_RATE
    colors = ["#4fc3f7", "#81c784", "#ffb74d", "#e57373"]
    for i, ch in enumerate(MAIN_CHANNELS):
        offset = i * 200
        ax.plot(t[:len(channels[ch])], channels[ch] + offset,
                color=colors[i], linewidth=0.4, label=ch)
    ax.set_ylabel("µV (offset)")
    ax.set_title("Raw EEG")
    ax.legend(loc="upper right", fontsize=8)
    ax.set_xlim(0, duration)

    # 2. Band powers for TP9
    ax = axes[1]
    times, powers = compute_band_powers(channels["TP9"])
    for band, power in powers.items():
        ax.plot(times, 10 * np.log10(power + 1e-10),
                color=BAND_COLORS[band], linewidth=1.2, label=band)
    ax.set_ylabel("Power (dB)")
    ax.set_title("Band Powers — TP9 (left temporal)")
    ax.legend(loc="upper right", fontsize=8)
    ax.set_xlim(0, duration)

    # 3. Band powers for AF7
    ax = axes[2]
    times, powers = compute_band_powers(channels["AF7"])
    for band, power in powers.items():
        ax.plot(times, 10 * np.log10(power + 1e-10),
                color=BAND_COLORS[band], linewidth=1.2, label=band)
    ax.set_ylabel("Power (dB)")
    ax.set_title("Band Powers — AF7 (left frontal)")
    ax.legend(loc="upper right", fontsize=8)
    ax.set_xlim(0, duration)

    # 4. Blink detection
    ax = axes[3]
    af7 = channels["AF7"]
    af8 = channels["AF8"]
    combined = (af7[:min(len(af7), len(af8))] + af8[:min(len(af7), len(af8))]) / 2
    blink_peaks, blink_signal = detect_blinks(combined)
    t_blink = np.arange(len(blink_signal)) / SAMPLE_RATE
    ax.plot(t_blink, blink_signal, color="#78909c", linewidth=0.5, label="Filtered |AF7+AF8|")
    if len(blink_peaks) > 0:
        ax.plot(blink_peaks / SAMPLE_RATE, blink_signal[blink_peaks],
                "rv", markersize=6, label=f"Blinks ({len(blink_peaks)})")
    ax.set_ylabel("|µV|")
    ax.set_title(f"Blink Detection — {len(blink_peaks)} blinks detected")
    ax.legend(loc="upper right", fontsize=8)
    ax.set_xlim(0, duration)

    # 5. Alpha asymmetry
    ax = axes[4]
    tp9 = channels["TP9"]
    tp10 = channels["TP10"]
    asym_times, asymmetry = alpha_asymmetry(tp9, tp10)
    ax.plot(asym_times, asymmetry, color="#ab47bc", linewidth=1.2)
    ax.axhline(0, color="#555", linewidth=0.5, linestyle="--")
    ax.fill_between(asym_times, asymmetry, 0,
                    where=asymmetry > 0, alpha=0.3, color="#66bb6a", label="Right > Left")
    ax.fill_between(asym_times, asymmetry, 0,
                    where=asymmetry < 0, alpha=0.3, color="#ef5350", label="Left > Right")
    ax.set_ylabel("ln(R) - ln(L)")
    ax.set_xlabel("Time (seconds)")
    ax.set_title("Alpha Asymmetry (TP10 vs TP9)")
    ax.legend(loc="upper right", fontsize=8)
    ax.set_xlim(0, duration)

    # Summary stats
    print(f"\n{'='*50}")
    print("ANALYSIS SUMMARY")
    print(f"{'='*50}")

    for ch in MAIN_CHANNELS:
        _, powers = compute_band_powers(channels[ch])
        print(f"\n  {ch}:")
        for band, p in powers.items():
            mean_db = 10 * np.log10(np.mean(p) + 1e-10)
            print(f"    {band:>6}: {mean_db:+.1f} dB")

    print(f"\n  Blinks detected: {len(blink_peaks)}")
    if len(blink_peaks) > 0:
        rate = len(blink_peaks) / duration * 60
        print(f"  Blink rate: {rate:.1f} per minute")

    mean_asym = np.mean(asymmetry)
    print(f"\n  Alpha asymmetry (mean): {mean_asym:+.3f}")
    if mean_asym > 0.1:
        print(f"  → Right-dominant (approach motivation)")
    elif mean_asym < -0.1:
        print(f"  → Left-dominant (withdrawal motivation)")
    else:
        print(f"  → Balanced")

    # Save figure
    out_path = os.path.join(session_dir, "analysis.png")
    fig.savefig(out_path, dpi=150, facecolor="#1e1e1e")
    print(f"\n  Plot saved: {out_path}")

    plt.show()


if __name__ == "__main__":
    main()
