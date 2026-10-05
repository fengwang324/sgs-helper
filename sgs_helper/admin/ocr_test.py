"""PaddleOCR 官方 API 的本地试跑封装（给管理页的 /ocr 子页面用）。

只用一个令牌：AI Studio Access Token（PaddleOCR 官方 API），无需百度 AK/SK 换 token。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

BASE = os.environ.get("PADDLEOCR_BASE_URL", "https://paddleocr.aistudio-app.com")
JOBS = BASE + "/api/v2/ocr/jobs"

#: 官方 API 支持的模型
MODELS: tuple[str, ...] = (
    "PP-OCRv6",
    "PP-OCRv5",
    "PP-OCRv5-latin",
    "PaddleOCR-VL-1.6",
    "PaddleOCR-VL",
    "PP-StructureV3",
)
DEFAULT_MODEL = "PP-OCRv6"

_TOKEN_FILE = Path(__file__).resolve().parents[2] / "build" / ".paddleocr.json"
_DEFAULT_TOKEN = os.environ.get(
    "PADDLEOCR_ACCESS_TOKEN", "672812e42832d666a6471af119c2ac14d93ec8ef"
)


def token() -> str:
    """环境变量优先，其次 build/.paddleocr.json，最后内置默认值。"""
    env = os.environ.get("PADDLEOCR_ACCESS_TOKEN")
    if env:
        return env.strip()
    try:
        data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        if data.get("token"):
            return str(data["token"]).strip()
    except Exception:
        pass
    return _DEFAULT_TOKEN


def save_token(value: str) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps({"token": value.strip()}, ensure_ascii=False),
                           encoding="utf-8")


def _headers() -> dict:
    return {"Authorization": "Bearer " + token()}


def _unwrap(payload):
    """官方接口统一包了一层 {"code":0,"msg":"...","data":{...}}（状态接口还多一个 traceId），
    这里只按「有没有 dict 类型的 data」判断，不要看键的个数。"""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def _extract(txt: str) -> dict:
    """把返回的 JSONL 拆成 ocrResults（带坐标）与 layoutParsingResults（VL 版式）。"""
    lines: list[dict] = []
    blocks: list[dict] = []
    try:
        obj = json.loads(txt.strip().split("\n")[0])
    except Exception:
        return {"lines": lines, "blocks": blocks}
    res = obj.get("result", {}) if isinstance(obj, dict) else {}
    for item in res.get("ocrResults", []) or []:
        pr = item.get("prunedResult", {})
        for t, s, p in zip(pr.get("rec_texts", []), pr.get("rec_scores", []),
                           pr.get("rec_polys", [])):
            xs = [q[0] for q in p] if p else [0]
            ys = [q[1] for q in p] if p else [0]
            lines.append({"text": t, "score": round(float(s), 3),
                          "x": int(min(xs)), "y": int(min(ys)),
                          "x2": int(max(xs)), "y2": int(max(ys))})
    for item in res.get("layoutParsingResults", []) or []:
        pr = item.get("prunedResult", {})
        for b in pr.get("parsing_res_list", []) or []:
            c = (b.get("block_content") or "").strip()
            if c:
                blocks.append({"label": b.get("block_label") or "", "text": c,
                               "bbox": b.get("block_bbox") or []})
    return {"lines": lines, "blocks": blocks}


def run(image_bytes: bytes, filename: str = "image.png", model: str = DEFAULT_MODEL,
        options: dict | None = None, poll_timeout: float = 150.0) -> dict:
    """提交一张图片到 PaddleOCR 官方 API，返回结构化结果。"""
    t0 = time.time()
    data = {"model": model, "optionalPayload": json.dumps(options or {})}
    try:
        r = requests.post(JOBS, data=data,
                          files={"file": (filename, image_bytes)},
                          headers=_headers(), timeout=120)
    except Exception as exc:  # 网络错误
        return {"ok": False, "error": f"提交失败：{exc}", "model": model}
    if r.status_code >= 300:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}", "model": model}
    job = _unwrap(r.json())
    job_id = job.get("jobId") or job.get("job_id") or job.get("id")
    if not job_id:
        return {"ok": False, "error": f"未返回 jobId：{str(job)[:200]}", "model": model}

    state = "pending"
    while time.time() - t0 < poll_timeout:
        time.sleep(1.5)
        try:
            s = requests.get(f"{JOBS}/{job_id}", headers=_headers(), timeout=60)
            d = _unwrap(s.json())
        except Exception as exc:
            return {"ok": False, "error": f"轮询失败：{exc}", "model": model, "job_id": job_id}
        state = d.get("state")
        if state == "done":
            url = (d.get("resultUrl") or {}).get("jsonUrl")
            raw = ""
            if url:
                try:
                    raw = requests.get(url, timeout=90).text
                except Exception as exc:
                    return {"ok": False, "error": f"下载结果失败：{exc}", "model": model}
            parsed = _extract(raw)
            return {"ok": True, "model": model, "job_id": job_id,
                    "elapsed": round(time.time() - t0, 1),
                    "lines": parsed["lines"], "blocks": parsed["blocks"],
                    "raw": raw[:20000]}
        if state == "failed":
            return {"ok": False, "error": str(d.get("errorMsg") or "任务失败"),
                    "model": model, "job_id": job_id}
    return {"ok": False, "error": f"等待超时（{poll_timeout:.0f}s，最后状态 {state}）",
            "model": model, "job_id": job_id}
