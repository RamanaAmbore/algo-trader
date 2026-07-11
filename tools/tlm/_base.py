#!/usr/bin/env python3
"""
_base.py — RamboQ-TLM shared base class, data types, and CLI boilerplate.

Every TLM tool subclasses TlmTool and calls self._run_or_dry(args).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# Repository root — two levels up from tools/tlm/
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = REPO_ROOT / ".log"


@dataclass
class TlmFinding:
    """One discrete issue found by a tool."""
    item: str        # what was checked (file/function/package/etc.)
    detail: str      # human-readable description
    severity: str    # "P1" | "P2" | "P3"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TlmResult:
    """Structured result from one TLM tool run."""
    tool: str
    status: str              # "ok" | "warn" | "fail" | "skip"
    severity: str            # highest severity present: "P1" | "P2" | "P3" | ""
    summary: str             # one-line human summary
    findings: list[TlmFinding] = field(default_factory=list)
    exit_code: int = 0       # 0=ok/warn-P3, 1=P1/P2 found, 2=tool error/skip

    def to_dict(self) -> dict:
        d = asdict(self)
        d["findings"] = [f.to_dict() for f in self.findings]
        return d

    @classmethod
    def skip(cls, tool: str, reason: str) -> "TlmResult":
        return cls(
            tool=tool,
            status="skip",
            severity="",
            summary=reason,
            findings=[],
            exit_code=2,
        )

    @classmethod
    def tool_error(cls, tool: str, reason: str) -> "TlmResult":
        return cls(
            tool=tool,
            status="fail",
            severity="P1",
            summary=f"Tool error: {reason}",
            findings=[TlmFinding(item="tool", detail=reason, severity="P1")],
            exit_code=2,
        )


def _highest_severity(findings: list[TlmFinding]) -> str:
    """Return the highest severity level from a list of findings."""
    order = {"P1": 0, "P2": 1, "P3": 2}
    if not findings:
        return ""
    return min((f.severity for f in findings), key=lambda s: order.get(s, 99))


def _compute_exit_code(findings: list[TlmFinding]) -> int:
    """
    0 — no findings or P3 only
    1 — any P1 or P2
    2 — tool error (set externally)
    """
    for f in findings:
        if f.severity in ("P1", "P2"):
            return 1
    return 0


def _compute_status(findings: list[TlmFinding]) -> str:
    if not findings:
        return "ok"
    sev = _highest_severity(findings)
    if sev == "P1":
        return "fail"
    return "warn"  # P2 or P3


class TlmTool:
    """
    Base class for all TLM tools.

    Subclasses must implement:
        name: str
        description: str
        run(self, args: argparse.Namespace) -> TlmResult
    """

    name: str = "TOOL"
    description: str = "No description."

    # ------------------------------------------------------------------ #
    # Subclasses override this                                             #
    # ------------------------------------------------------------------ #
    def run(self, args: argparse.Namespace) -> TlmResult:
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Shared parser — every tool gets these flags for free                #
    # ------------------------------------------------------------------ #
    def build_parser(self) -> argparse.ArgumentParser:
        p = argparse.ArgumentParser(
            prog=self.name.lower(),
            description=f"{self.name}: {self.description}",
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be checked; no side effects (no snapshot writes, no installs).",
        )
        p.add_argument(
            "--json",
            action="store_true",
            help="Emit result as JSON to stdout.",
        )
        p.add_argument(
            "--since",
            type=int,
            default=1,
            metavar="DAYS",
            help="Look-back window in days for git-based checks (default: 1).",
        )
        return p

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        """Subclasses may override to add tool-specific arguments."""
        pass

    # ------------------------------------------------------------------ #
    # Entry point                                                          #
    # ------------------------------------------------------------------ #
    def main(self, argv: Optional[list[str]] = None) -> int:
        parser = self.build_parser()
        self.add_args(parser)
        args = parser.parse_args(argv)

        if args.dry_run:
            print(f"[{self.name}] --dry-run: would run checks, no side effects.")
            self._print_dry_run_plan(args)
            return 0

        try:
            result = self.run(args)
        except Exception as exc:  # noqa: BLE001
            result = TlmResult.tool_error(self.name, str(exc))

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            self._print_human(result)

        return result.exit_code

    def _print_dry_run_plan(self, args: argparse.Namespace) -> None:
        """Override to describe what the tool would do in dry-run mode."""
        pass

    def _print_human(self, result: TlmResult) -> None:
        icon = {"ok": "[ok]", "warn": "[warn]", "fail": "[FAIL]", "skip": "[skip]"}
        prefix = icon.get(result.status, "[?]")
        print(f"{prefix} {result.tool}: {result.summary}")
        for f in result.findings:
            print(f"  {f.severity}  {f.item}: {f.detail}")

    # ------------------------------------------------------------------ #
    # Helpers available to subclasses                                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def ensure_log_dir() -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_DIR

    @staticmethod
    def build_result(
        tool: str,
        findings: list[TlmFinding],
        ok_summary: str,
    ) -> TlmResult:
        """
        Convenience constructor: derives status, severity, exit_code from findings.
        ok_summary is used when findings is empty.
        """
        sev = _highest_severity(findings)
        status = _compute_status(findings)
        exit_code = _compute_exit_code(findings)

        if findings:
            p1 = sum(1 for f in findings if f.severity == "P1")
            p2 = sum(1 for f in findings if f.severity == "P2")
            p3 = sum(1 for f in findings if f.severity == "P3")
            parts = []
            if p1:
                parts.append(f"{p1} P1")
            if p2:
                parts.append(f"{p2} P2")
            if p3:
                parts.append(f"{p3} P3")
            summary = f"{', '.join(parts)} finding(s)"
        else:
            summary = ok_summary

        return TlmResult(
            tool=tool,
            status=status,
            severity=sev,
            summary=summary,
            findings=findings,
            exit_code=exit_code,
        )
