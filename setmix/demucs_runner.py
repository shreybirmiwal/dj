from __future__ import annotations

import sys
from pathlib import Path

import soundfile as sf


def _soundfile_save(
    path: str,
    waveform,
    sample_rate: int,
    *,
    bits_per_sample: int = 24,
    **_kwargs,
) -> None:
    """Write Demucs output without torchaudio's optional TorchCodec runtime."""
    audio = waveform.detach().cpu().numpy()
    if audio.ndim == 2:
        audio = audio.T
    subtype = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}.get(bits_per_sample, "PCM_24")
    sf.write(Path(path), audio, sample_rate, subtype=subtype)


def main() -> int:
    import torchaudio

    # Demucs uses torchaudio only as its final WAV/FLAC writer here. Recent
    # torchaudio builds delegate that call to an optional shared TorchCodec
    # runtime; SoundFile is already a SetMix dependency and writes sample-exact
    # FLAC directly.
    torchaudio.save = _soundfile_save
    from demucs.separate import main as demucs_main

    demucs_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
