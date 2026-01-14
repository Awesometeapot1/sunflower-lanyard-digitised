# mic_level.py
# Simple "sound level" + quiet detection for an analog mic module on a Pico ADC pin.
# Works well for lanyard-style ambient quiet/loud detection.

from machine import ADC
import time
import math

class MicLevel:
    def __init__(
        self,
        adc_pin=27,              # GP26 / ADC0 by default
        sample_count=200,        # samples per update (tradeoff: speed vs stability)
        sample_us=200,           # delay between samples in microseconds
        ema_alpha=0.15,          # smoothing (0..1), higher = faster response
        quiet_threshold=0.015,   # threshold in "relative RMS" (tune this!)
        hysteresis=0.003,        # prevents flicker around threshold
        quiet_hold_ms=1500       # must be quiet this long to switch into quiet
    ):
        self.adc = ADC(adc_pin)

        self.sample_count = sample_count
        self.sample_us = sample_us

        self.ema_alpha = ema_alpha
        self.quiet_threshold = quiet_threshold
        self.hysteresis = hysteresis
        self.quiet_hold_ms = quiet_hold_ms

        self._ema_rms = 0.0
        self._quiet = False
        self._quiet_start = None

    def _read_rms(self):
        """
        Reads the mic ADC multiple times and returns RMS of AC component.
        ADC returns 0..65535. Most mic amps are biased ~mid-scale.
        """
        # Collect samples
        s = 0.0
        ss = 0.0

        for _ in range(self.sample_count):
            v = self.adc.read_u16() / 65535.0  # normalize 0..1
            s += v
            ss += v * v
            time.sleep_us(self.sample_us)

        mean = s / self.sample_count
        # variance = E[x^2] - (E[x])^2
        var = (ss / self.sample_count) - (mean * mean)
        if var < 0:
            var = 0
        rms = math.sqrt(var)  # RMS of AC component (relative)
        return rms

    def update(self):
        """
        Call this regularly (e.g., in your main loop).
        Returns a dict with raw_rms, smoothed_rms, quiet boolean.
        """
        raw_rms = self._read_rms()

        # Exponential moving average smoothing
        self._ema_rms = (self.ema_alpha * raw_rms) + ((1.0 - self.ema_alpha) * self._ema_rms)

        now = time.ticks_ms()

        # Quiet detection with hysteresis + hold time
        enter_quiet_at = self.quiet_threshold
        exit_quiet_at  = self.quiet_threshold + self.hysteresis

        if not self._quiet:
            # Candidate to become quiet
            if self._ema_rms < enter_quiet_at:
                if self._quiet_start is None:
                    self._quiet_start = now
                elif time.ticks_diff(now, self._quiet_start) >= self.quiet_hold_ms:
                    self._quiet = True
            else:
                self._quiet_start = None
        else:
            # Already quiet; leave quiet if it gets louder than exit threshold
            if self._ema_rms > exit_quiet_at:
                self._quiet = False
                self._quiet_start = None

        return {
            "raw_rms": raw_rms,
            "rms": self._ema_rms,
            "quiet": self._quiet
        }

    @property
    def quiet(self):
        return self._quiet

    @property
    def rms(self):
        return self._ema_rms
