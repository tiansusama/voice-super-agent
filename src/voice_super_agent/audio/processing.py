from collections import deque
from threading import Lock

import numpy as np
from pywebrtc_audio import AudioProcessor


RATE = 16000
BLOCK_SAMPLES = RATE // 100


class EchoAudio:
    """以设备 ADC/DAC 时间对齐参考，AEC 实例由采集回调独占。"""

    def __init__(self):
        self._processor = AudioProcessor(sample_rate=RATE, echo_cancellation=True, stream_delay_ms=0)
        self._rendered: deque[tuple[float, np.ndarray]] = deque()
        self._lock = Lock()

    def rendered(self, audio: bytes, dac_time: float):
        samples = np.frombuffer(audio, dtype="<i2").copy()
        if samples.any():
            with self._lock:
                self._rendered.append((dac_time, samples))

    def reference(self, adc_time: float, count: int) -> np.ndarray:
        far = np.zeros(count, dtype=np.int16)
        with self._lock:
            while self._rendered and self._rendered[0][0] + len(self._rendered[0][1]) / RATE <= adc_time:
                self._rendered.popleft()
            for start, samples in self._rendered:
                offset = round((start - adc_time) * RATE)
                left, right = max(0, offset), min(count, offset + len(samples))
                if left < right:
                    far[left:right] = samples[left - offset:right - offset]
        return far

    def capture(self, audio: bytes, adc_time: float) -> bytes:
        near = np.frombuffer(audio, dtype="<i2")
        far = self.reference(adc_time, len(near))
        # 固定 10ms 设备块避免跨次调用补零；声学传播延迟交给 AEC3 内部估计。
        return self._processor.process(near, far).tobytes()


def audio_level(audio: bytes) -> float:
    samples = np.frombuffer(audio, dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean(samples * samples)))
    return float(np.clip((20 * np.log10(rms / 32768) + 60) / 60, 0, 1)) if rms else 0.0
