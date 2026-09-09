#!/usr/bin/env python3
"""Probe Music Flamingo for structured, DJ-relevant semantic annotations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from transformers import AudioFlamingo3ForConditionalGeneration, AutoProcessor


PROMPT = """Analyze this entire song as a DJ transition-planning critic. Return only valid JSON.
All times must be seconds from the beginning of the supplied audio. Do not transcribe or quote lyrics.
Use this schema:
{
  "summary": {"genre": str, "mood": str, "estimated_bpm": number|null, "meter": str|null},
  "sections": [{"start": number, "end": number, "label": "intro|verse|pre_chorus|chorus|drop|breakdown|bridge|outro|other", "energy": number, "vocal_density": number}],
  "vocal_phrases": [{"start": number, "end": number, "role": "lead|hook|adlib|spoken", "hype_or_singalong": number, "ends_cleanly": bool}],
  "events": [{"time": number, "type": "drop|build_start|build_end|quiet_pocket|drum_change|bass_change|vocal_entry|vocal_exit", "confidence": number}],
  "safe_exits": [{"start": number, "end": number, "confidence": number, "reason": str}],
  "safe_entries": [{"start": number, "end": number, "confidence": number, "reason": str}],
  "never_fade": [{"start": number, "end": number, "confidence": number, "reason": str}]
}
Energy, vocal_density, hype_or_singalong, and confidence are 0 to 1. Prefer musically useful phrase and section boundaries. A safe exit must not cut or fade a recognizable lead phrase mid-line. Mark crowd-singalong hooks and climactic lines in never_fade. Be conservative: omit uncertain events rather than inventing precision."""

FOCUS_PROMPT = """Judge this short candidate region for a DJ transition. Return only compact valid JSON and do not quote lyrics.
Times are seconds from the start of this supplied clip. Use exactly this schema:
{
  "vocal_phrases": [{"start": number, "end": number, "hype": number, "complete": bool}],
  "never_fade": [{"start": number, "end": number, "confidence": number, "reason": str}],
  "safe_exits": [{"start": number, "end": number, "confidence": number, "reason": str}],
  "reusable_backing_loops": [{"start": number, "end": number, "confidence": number, "reason": str}],
  "drop_times": [number],
  "notes": str
}
Return at most 8 vocal phrases and at most 3 items in every other list. A safe exit must happen after a complete vocal thought or inside a true quiet/instrumental pocket. Never recommend fading during a recognizable hook, crowd-singalong line, or climactic phrase. A reusable loop must have stable rhythm and no lead vocal. Be conservative and do not invent sub-second precision."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="nvidia/music-flamingo-hf")
    parser.add_argument("--max-new-tokens", type=int, default=1600)
    parser.add_argument("--focus", action="store_true", help="judge short candidate windows")
    return parser.parse_args()


def extract_json(text: str) -> object | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def main() -> None:
    args = parse_args()
    processor = AutoProcessor.from_pretrained(args.model)
    model = AudioFlamingo3ForConditionalGeneration.from_pretrained(
        args.model,
        device_map="auto",
        torch_dtype=torch.bfloat16,
        low_cpu_mem_usage=True,
        attn_implementation="sdpa",
    )
    model.eval()

    results = []
    for audio in args.audio:
        conversation = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": FOCUS_PROMPT if args.focus else PROMPT},
                    {"type": "audio", "path": str(audio.resolve())},
                ],
            }
        ]
        inputs = processor.apply_chat_template(
            conversation,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
        ).to(model.device)
        # The processor emits float32 audio while the memory-efficient model is
        # bfloat16. Transformers does not currently cast this tensor for AF3.
        inputs["input_features"] = inputs["input_features"].to(torch.bfloat16)
        with torch.inference_mode():
            outputs = model.generate(
                **inputs,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
            )
        raw = processor.batch_decode(
            outputs[:, inputs.input_ids.shape[1] :], skip_special_tokens=True
        )[0].strip()
        results.append({"audio": str(audio), "annotation": extract_json(raw), "raw": raw})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
