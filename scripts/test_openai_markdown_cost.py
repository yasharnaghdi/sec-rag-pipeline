#!/usr/bin/env python3
"""Send Markdown filings to OpenAI and report token usage + estimated cost.

Usage:
    .venv311/bin/poetry run python scripts/test_openai_markdown_cost.py \
      --input-dir output/html_markdown \
      --output-dir output/openai_markdown_test \
      --limit 10 \
      --model gpt-4o-mini
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

DEFAULT_MODEL = "gpt-4o-mini"
_DUMMY_KEY_VALUES = {"dummy", "", "your-key-here", "sk-dummy"}
_DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
_CONTEXT_RESERVE_TOKENS = 8_000
_EST_CHARS_PER_TOKEN = 4

# Approximate model context windows used to size default --max-chars safely.
MODEL_CONTEXT_WINDOW_TOKENS: dict[str, int] = {
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
}

# Fallback assumptions used only when user does not pass explicit pricing flags.
# Units are USD per 1M tokens.
DEFAULT_MODEL_PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),  # input, output
    "gpt-4o": (2.50, 10.00),  # input, output
}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileRunResult:
    file_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None
    response_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a small OpenAI test over markdown files and estimate usage cost."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("output/html_markdown"),
        help="Directory containing .md files to send.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/openai_markdown_test"),
        help="Directory where API responses and run summary will be written.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of markdown files to send (sorted by name).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="OpenAI chat model name.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=0,
        help=(
            "Maximum characters sent per markdown file. "
            "Use 0 to auto-size close to the model context window."
        ),
    )
    parser.add_argument(
        "--input-cost-per-1m",
        type=float,
        default=None,
        help="Optional input token price in USD per 1M tokens.",
    )
    parser.add_argument(
        "--output-cost-per-1m",
        type=float,
        default=None,
        help="Optional output token price in USD per 1M tokens.",
    )
    return parser.parse_args()


def _resolve_api_key(project_root: Path) -> str:
    load_dotenv(project_root / ".env", override=False)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key.lower() in _DUMMY_KEY_VALUES:
        raise RuntimeError(
            "OPENAI_API_KEY is missing or set to a dummy value. "
            "Set a real key in your environment or .env before running this test."
        )
    return api_key


def _resolve_pricing(args: argparse.Namespace) -> tuple[float, float] | None:
    explicit_input = args.input_cost_per_1m
    explicit_output = args.output_cost_per_1m
    if (explicit_input is None) != (explicit_output is None):
        raise ValueError("Provide both --input-cost-per-1m and --output-cost-per-1m, or neither.")
    if explicit_input is not None and explicit_output is not None:
        return float(explicit_input), float(explicit_output)

    return DEFAULT_MODEL_PRICING_USD_PER_1M.get(str(args.model))


def _estimate_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    pricing: tuple[float, float] | None,
) -> float | None:
    if pricing is None:
        return None
    input_price_per_1m, output_price_per_1m = pricing
    return (prompt_tokens / 1_000_000.0) * input_price_per_1m + (
        completion_tokens / 1_000_000.0
    ) * output_price_per_1m


def _resolve_max_chars(model: str, requested_max_chars: int) -> int:
    if requested_max_chars > 0:
        return requested_max_chars

    context_tokens = MODEL_CONTEXT_WINDOW_TOKENS.get(model, _DEFAULT_CONTEXT_WINDOW_TOKENS)
    usable_tokens = max(1_000, context_tokens - _CONTEXT_RESERVE_TOKENS)
    return usable_tokens * _EST_CHARS_PER_TOKEN


def _build_messages(file_name: str, markdown_text: str) -> list[dict[str, str]]:
    system_prompt = (
        "You extract metadata from SEC filing markdown. "
        "Return valid JSON with keys: company_name, form_type, filing_year, "
        "sections_detected (array of short strings), and one_sentence_summary."
    )
    user_prompt = (
        f"File name: {file_name}\n"
        "Analyze the markdown below and return only JSON.\n\n"
        f"{markdown_text}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _write_summary_csv(path: Path, rows: list[FileRunResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "file_name",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "estimated_cost_usd",
                "response_path",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.file_name,
                    row.prompt_tokens,
                    row.completion_tokens,
                    row.total_tokens,
                    "" if row.estimated_cost_usd is None else f"{row.estimated_cost_usd:.8f}",
                    str(row.response_path),
                ]
            )


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    if args.limit <= 0:
        log.error("--limit must be > 0")
        return 1
    if args.max_chars < 0:
        log.error("--max-chars must be >= 0")
        return 1
    if not args.input_dir.exists():
        log.error("Input directory does not exist: %s", args.input_dir)
        return 1
    max_chars = _resolve_max_chars(str(args.model), int(args.max_chars))
    log.info("Using max_chars=%d for model=%s", max_chars, args.model)

    markdown_files = sorted(args.input_dir.glob("*.md"))
    if not markdown_files:
        log.error("No markdown files found in %s", args.input_dir)
        return 1
    selected_files = markdown_files[: args.limit]

    project_root = Path(__file__).resolve().parents[1]
    try:
        api_key = _resolve_api_key(project_root)
    except RuntimeError as exc:
        log.error(str(exc))
        return 1

    try:
        pricing = _resolve_pricing(args)
    except ValueError as exc:
        log.error(str(exc))
        return 1
    if pricing is None:
        log.warning(
            "No default pricing found for model=%s. Cost fields will be left blank. "
            "Pass --input-cost-per-1m and --output-cost-per-1m for explicit pricing.",
            args.model,
        )
    else:
        log.info(
            "Using pricing for model=%s: input=$%.4f/M output=$%.4f/M tokens",
            args.model,
            pricing[0],
            pricing[1],
        )

    output_dir: Path = args.output_dir
    responses_dir = output_dir / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)

    client = OpenAI(api_key=api_key)
    results: list[FileRunResult] = []
    failure_count = 0

    for file_path in selected_files:
        markdown_text = file_path.read_text(encoding="utf-8", errors="replace")
        truncated_text = markdown_text[:max_chars]
        messages = _build_messages(file_path.name, truncated_text)

        try:
            response = client.chat.completions.create(  # type: ignore[call-overload]
                model=args.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            response_text = response.choices[0].message.content or ""
            usage = response.usage
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
            estimated_cost_usd = _estimate_cost_usd(prompt_tokens, completion_tokens, pricing)

            parsed_response: Any
            try:
                parsed_response = json.loads(response_text)
            except json.JSONDecodeError:
                parsed_response = None

            response_path = responses_dir / f"{file_path.stem}.response.json"
            payload = {
                "file_name": file_path.name,
                "model": args.model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": estimated_cost_usd,
                "truncated_char_count": len(truncated_text),
                "response_text": response_text,
                "parsed_response": parsed_response,
            }
            response_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            results.append(
                FileRunResult(
                    file_name=file_path.name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    response_path=response_path,
                )
            )
            log.info(
                "Processed %s | prompt=%d completion=%d total=%d cost=%s",
                file_path.name,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                "n/a" if estimated_cost_usd is None else f"${estimated_cost_usd:.6f}",
            )
        except Exception as exc:  # noqa: BLE001
            failure_count += 1
            log.exception("OpenAI request failed for %s: %s", file_path.name, exc)

    summary_csv_path = output_dir / "summary.csv"
    _write_summary_csv(summary_csv_path, results)

    total_prompt_tokens = sum(result.prompt_tokens for result in results)
    total_completion_tokens = sum(result.completion_tokens for result in results)
    total_tokens = sum(result.total_tokens for result in results)
    total_estimated_cost = (
        None
        if any(result.estimated_cost_usd is None for result in results)
        else sum((result.estimated_cost_usd or 0.0) for result in results)
    )

    run_summary = {
        "model": args.model,
        "input_dir": str(args.input_dir),
        "output_dir": str(output_dir),
        "files_requested": len(selected_files),
        "files_succeeded": len(results),
        "files_failed": failure_count,
        "prompt_tokens_total": total_prompt_tokens,
        "completion_tokens_total": total_completion_tokens,
        "tokens_total": total_tokens,
        "pricing_usd_per_1m": (
            None if pricing is None else {"input": pricing[0], "output": pricing[1]}
        ),
        "estimated_total_cost_usd": total_estimated_cost,
        "summary_csv_path": str(summary_csv_path),
    }
    summary_path = output_dir / "run_summary.json"
    summary_path.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")

    log.info("Wrote %s and %s", summary_csv_path, summary_path)
    if len(results) == 0:
        log.error("No successful OpenAI responses.")
        return 1
    if failure_count > 0:
        log.warning("Completed with failures: %d failed files.", failure_count)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
