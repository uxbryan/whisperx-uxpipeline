"""
core/pipeline.py — Steps 1–5: WhisperX 轉錄 + alignment + pyannote 聲紋分群 + OpenCC + 樣本蒐集

從 transcribe.py / process_transcripts.py 重構而來，去掉硬編路徑與檔案迴圈，
改為單檔處理 + 回傳結構化資料，不直接寫檔（由 orchestrator 決定產出路徑）。

公開介面：
    transcribe_and_diarize(audio_path, *, hf_token, ...) -> list[Segment]
    convert_to_traditional(segments) -> list[Segment]
    collect_speaker_samples(segments, n=5) -> dict[str, list[str]]
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from opencc import OpenCC

_cc_s2t = OpenCC("s2t")

# 模型 lazy load (避免 import 時阻塞)。Whisper 模型依名稱 cache（small/medium/large 各一份）
_whisper_models: dict[str, object] = {}
_align_model = None
_align_metadata = None
_diarize_model = None


@dataclass
class Segment:
    """單一逐字稿段落。"""
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    words: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "speaker": self.speaker,
            "words": self.words,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(
            start=float(d.get("start", 0)),
            end=float(d.get("end", 0)),
            text=str(d.get("text", "")),
            speaker=d.get("speaker"),
            words=d.get("words", []),
        )


def _load_whisper(model_name: str, device: str, compute_type: str, language: str):
    """Lazy load Whisper model（依 model_name 各自 cache，支援使用者選擇 small/medium/large-v3）。"""
    key = f"{model_name}|{device}|{compute_type}|{language}"
    if key not in _whisper_models:
        import whisperx
        _whisper_models[key] = whisperx.load_model(
            model_name, device, compute_type=compute_type, language=language
        )
    return _whisper_models[key]


def _load_align(language: str, device: str):
    """Lazy load alignment model。"""
    global _align_model, _align_metadata
    if _align_model is None:
        import whisperx
        _align_model, _align_metadata = whisperx.load_align_model(
            language_code=language, device=device
        )
    return _align_model, _align_metadata


def _resolve_hf_token(hf_token: Optional[str]) -> Optional[str]:
    """env 沒給的話，fallback 讀 huggingface-cli 的 cached token。"""
    if hf_token:
        return hf_token
    cache_token = Path.home() / ".cache" / "huggingface" / "token"
    if cache_token.exists():
        try:
            return cache_token.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def _load_diarize(hf_token: Optional[str], device: str):
    """Lazy load pyannote diarization pipeline。"""
    global _diarize_model
    token = _resolve_hf_token(hf_token)
    if _diarize_model is None and token:
        from whisperx.diarize import DiarizationPipeline
        _diarize_model = DiarizationPipeline(token=token, device=device)
    return _diarize_model


def transcribe_and_diarize(
    audio_path: str | Path,
    *,
    hf_token: Optional[str] = None,
    model: str = "large-v3",
    device: str = "cpu",
    compute_type: str = "int8",
    batch_size: int = 8,
    language: str = "zh",
    cache_path: Optional[Path] = None,
    progress_cb: Optional[callable] = None,
) -> list[Segment]:
    """Steps 1–3: 轉錄 → 對齊 → 聲紋分群。

    cache_path 指向一個 JSON，存對齊結果（最重的部份）。若存在則跳過 Steps 1–2。
    progress_cb(stage_name, pct) 用於回報進度給 web UI。

    回傳已分配 speaker 標籤的 Segment 列表（簡體中文，下一步 convert_to_traditional）。
    """
    import whisperx

    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    audio = whisperx.load_audio(str(audio_path))

    # Steps 1–2: 轉錄 + 對齊（吃 cache）
    if cache_path and Path(cache_path).exists():
        if progress_cb: progress_cb("aligned_cache_hit", 50)
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        aligned_segments = cached["segments"]
    else:
        if progress_cb: progress_cb("transcribing", 10)
        whisper = _load_whisper(model, device, compute_type, language)
        raw = whisper.transcribe(audio, batch_size=batch_size, language=language)

        if progress_cb: progress_cb("aligning", 35)
        align_model, align_meta = _load_align(language, device)
        aligned = whisperx.align(
            raw["segments"], align_model, align_meta, audio, device,
            return_char_alignments=False,
        )
        aligned_segments = aligned["segments"]

        if cache_path:
            cache_path = Path(cache_path)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"segments": aligned_segments}, f,
                          ensure_ascii=False, default=str)

    # Step 3: 聲紋分群
    diarize = _load_diarize(hf_token, device) if hf_token else None
    if diarize:
        if progress_cb: progress_cb("diarizing", 70)
        diarize_segments = diarize(audio)
        result = whisperx.assign_word_speakers(
            diarize_segments, {"segments": aligned_segments}
        )
        aligned_segments = result["segments"]

    if progress_cb: progress_cb("transcribed", 80)
    return [Segment.from_dict(s) for s in aligned_segments]


def convert_to_traditional(segments: list[Segment]) -> list[Segment]:
    """Step 4: 簡體 → 繁體（OpenCC 字典規則，本機免費）。"""
    for seg in segments:
        seg.text = _cc_s2t.convert(seg.text)
        for w in seg.words:
            if "word" in w:
                w["word"] = _cc_s2t.convert(w["word"])
    return segments


def collect_speaker_samples(
    segments: list[Segment], n_per_speaker: int = 5
) -> dict[str, list[str]]:
    """Step 5: 蒐集每位 speaker 的前 N 句樣本，給 role_classify 用。

    例：{"SPEAKER_00": ["你們應該要...", "在 Figma 裡...", ...]}
    """
    samples: dict[str, list[str]] = {}
    for seg in segments:
        speaker = seg.speaker or "UNKNOWN"
        if speaker not in samples:
            samples[speaker] = []
        if len(samples[speaker]) < n_per_speaker and seg.text.strip():
            samples[speaker].append(seg.text.strip())
    return samples
