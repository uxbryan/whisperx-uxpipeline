"""
core/orchestrator.py — 端到端 pipeline 串接

把 pipeline / role_classify / polish / writer / db 串成一個函式：
給定一個 job_id（pending 狀態），跑完整個流程，更新 DB 狀態，最後寫檔到 NAS。

由 watcher / web worker 呼叫。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import db, pipeline, polish, role_classify, writer


def _audio_duration_sec(audio_path: Path) -> float:
    """ffprobe 拿音訊時長秒數。失敗則 fallback 300 秒（5 分）。"""
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0", str(audio_path),
        ], timeout=10)
        return float(out.decode().strip() or 300)
    except Exception:
        return 300.0


# 整條 pipeline (transcribe + align + diarize) 在 M4 Pro CPU 上的「實時倍率」
# 等真實量到的數據再校。以 8.5 分音訊 medium 跑 5 分鐘來反推：5*60 / (8.5*60) ≈ 0.59
_MODEL_REALTIME_FACTOR = {
    "small":    0.30,
    "medium":   0.60,
    "large-v3": 1.20,
}


def _spawn_progress_ticker(
    job_id: str,
    audio_path: Path,
    whisper_model: str,
    *,
    pct_start: int = 10,
    pct_end: int = 78,
    tick_sec: int = 3,
) -> threading.Event:
    """背景 thread：依時間比例更新 DB progress_pct，避免 UI 看起來像停。

    重要規則：
      - 只往上推進（讀現有 pct，比較後才寫）→ 不會把 progress_cb 設的階段值蓋回去
      - 保留現有 status（讀 DB 拿，不寫死 "transcribing"）→ 不會把 'aligning' 改回 'transcribing'
      - 涵蓋 transcribe + align + diarize 整段，不再有「假卡 2 分鐘」現象

    主流程 transcribe_and_diarize 結束後呼叫 stop.set() 收尾。
    """
    duration = _audio_duration_sec(audio_path)
    factor = _MODEL_REALTIME_FACTOR.get(whisper_model, 0.60)
    expected_total = max(duration * factor, 10.0)

    stop = threading.Event()
    start = time.time()

    def tick():
        while not stop.is_set():
            elapsed = time.time() - start
            ratio = min(elapsed / expected_total, 0.95)
            estimated = int(pct_start + ratio * (pct_end - pct_start))
            try:
                job = db.get_job(job_id)
                if job:
                    current_pct = job.get("progress_pct", 0) or 0
                    current_status = job.get("status", "transcribing")
                    if estimated > current_pct:
                        db.update_status(job_id, current_status, progress_pct=estimated)
            except Exception:
                pass
            stop.wait(tick_sec)

    t = threading.Thread(target=tick, name=f"vtt-ticker-{job_id}", daemon=True)
    t.start()
    return stop


def _load_config() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _output_dir_for(job: dict, cfg: dict) -> Path:
    """NAS result/YYYY/MM/<job_id>/"""
    created = job.get("created_at") or datetime.now(timezone.utc).isoformat()
    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    base = Path(cfg["storage"]["result_dir"]).expanduser()
    return base / f"{dt.year:04d}" / f"{dt.month:02d}" / job["id"]


def _cache_path_for(job: dict, cfg: dict) -> Path:
    """本機 SSD 對齊中間檔。"""
    scratch = Path(cfg["local"]["scratch_dir"]).expanduser()
    return scratch / "aligned" / f"{job['id']}.json"


def run_job(job_id: str) -> None:
    """執行一個 job 的完整 pipeline。失敗會更新 status=failed + error_msg。"""
    cfg = _load_config()
    job = db.get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    audio_path = Path(job["nas_input_path"])
    output_dir = _output_dir_for(job, cfg)
    cache_path = _cache_path_for(job, cfg)
    meeting_type = job.get("meeting_type") or "other"

    # 從 options_json 取得使用者指定的 Whisper 模型，缺則用 config 預設
    options = json.loads(job.get("options_json") or "{}")
    requested_model = options.get("whisper_model")
    allowed = cfg["whisper"].get("allowed_models", ["large-v3"])
    if requested_model in allowed:
        whisper_model = requested_model
    else:
        whisper_model = cfg["whisper"].get("default_model", "large-v3")

    def progress(stage: str, pct: int):
        db.update_status(job_id, _stage_to_status(stage), progress_pct=pct)

    try:
        # Steps 1–3: 轉錄 + 對齊 + 聲紋分群
        db.update_status(job_id, "transcribing", progress_pct=5)

        # 進度 ticker：transcribe 期間每 3 秒推進度（避免 UI 看起來像停）
        ticker_stop = _spawn_progress_ticker(
            job_id, audio_path, whisper_model, pct_start=10, pct_end=30
        )
        try:
            segments = pipeline.transcribe_and_diarize(
                audio_path,
                hf_token=os.environ.get("HF_TOKEN"),
                model=whisper_model,
                device=cfg["whisper"]["device"],
                compute_type=cfg["whisper"]["compute_type"],
                batch_size=cfg["whisper"]["batch_size"],
                language=cfg["whisper"]["language"],
                cache_path=cache_path,
                progress_cb=progress,
            )
        finally:
            ticker_stop.set()

        # Step 4: 簡轉繁
        segments = pipeline.convert_to_traditional(segments)

        # Step 5: 蒐集樣本
        samples = pipeline.collect_speaker_samples(segments)

        # Step 6: 角色辨識
        db.update_status(job_id, "classifying", progress_pct=82)
        role_map, role_cost = role_classify.classify(samples, meeting_type)
        if role_cost > 0:
            db.add_cost(job_id, role_cost)

        # Step 7: LLM 標點補齊
        db.update_status(job_id, "polishing", progress_pct=85)
        polished, polish_cost = polish.polish(
            segments,
            model=cfg["llm"]["model"],
            chunk_minutes=cfg["llm"]["chunk_minutes"],
            parallel=cfg["llm"]["parallel_chunks"],
            progress_cb=progress,
        )
        if polish_cost > 0:
            db.add_cost(job_id, polish_cost)

        # 寫檔到 NAS
        job_meta = dict(db.get_job(job_id) or {})
        writer.write_results(
            output_dir=output_dir,
            raw_segments=segments,
            polished_segments=polished,
            role_map=role_map,
            speaker_samples=samples,
            job_meta=job_meta,
        )

        db.update_status(
            job_id, "done",
            progress_pct=100,
            nas_output_path=str(output_dir),
        )

    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        db.update_status(job_id, "failed", error_msg=err[:2000])
        raise


def _stage_to_status(stage: str) -> str:
    """從 progress_cb 的 stage 名稱映射到 DB status。"""
    mapping = {
        "transcribing": "transcribing",
        "aligning": "aligning",
        "aligned_cache_hit": "aligning",
        "diarizing": "diarizing",
        "transcribed": "diarizing",
        "polishing": "polishing",
    }
    return mapping.get(stage, "transcribing")
