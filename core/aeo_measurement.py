"""AEO measurement ingestion and opportunity summarization."""

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class AEOMeasurement:
    """Manual or JSON-ingested AEO visibility signal."""

    prompt: str
    source: str = "manual"
    ai_referrals: int = 0
    share_of_mentions: float = 0.0
    cited_competitors: List[str] = field(default_factory=list)
    missed: bool = False
    notes: str = ""
    captured_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AEOMeasurement":
        """Create a measurement from a loose JSON dict."""
        raw_referrals = data.get('ai_referrals', 0) or 0
        raw_som = data.get('share_of_mentions', data.get('som', 0.0))
        som = float(raw_som) if raw_som not in (None, '') else 0.0
        ref_float = float(raw_referrals)
        if not math.isfinite(som) or not math.isfinite(ref_float):
            raise ValueError("AEOMeasurement numeric fields must be finite")
        return cls(
            prompt=data.get('prompt', ''),
            source=data.get('source', 'manual'),
            ai_referrals=int(ref_float),
            share_of_mentions=som,
            cited_competitors=list(data.get('cited_competitors', [])),
            missed=bool(data.get('missed', False)),
            notes=data.get('notes', ''),
            captured_at=data.get('captured_at', datetime.now().isoformat())
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        if (
            isinstance(self.share_of_mentions, bool)
            or not isinstance(self.share_of_mentions, (int, float))
            or not math.isfinite(float(self.share_of_mentions))
            or isinstance(self.ai_referrals, bool)
            or not isinstance(self.ai_referrals, (int, float))
            or not math.isfinite(float(self.ai_referrals))
        ):
            raise ValueError("AEOMeasurement numeric fields must be finite")
        return {
            'prompt': self.prompt,
            'source': self.source,
            'ai_referrals': int(self.ai_referrals),
            'share_of_mentions': float(self.share_of_mentions),
            'cited_competitors': self.cited_competitors,
            'missed': self.missed,
            'notes': self.notes,
            'captured_at': self.captured_at
        }


class AEOMeasurementStore:
    """Append-only local JSONL store for AEO observations."""

    def __init__(self, path: Optional[Path] = None):
        from .paths import output_root

        self.path = path or (output_root() / 'aeo_measurements.jsonl')

    def append(self, measurement: AEOMeasurement):
        """Append one measurement."""
        payload = json.dumps(measurement.to_dict(), allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, 'a') as f:
            f.write(payload + '\n')

    def ingest_json(self, data: Any) -> List[AEOMeasurement]:
        """Ingest a dict or list of dicts."""
        rows = data if isinstance(data, list) else [data]
        measurements = [AEOMeasurement.from_dict(row) for row in rows]
        for measurement in measurements:
            self.append(measurement)
        return measurements

    def load(self) -> List[AEOMeasurement]:
        """Load all stored measurements."""
        if not self.path.exists():
            return []
        measurements = []
        with open(self.path, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    measurements.append(AEOMeasurement.from_dict(json.loads(line)))
        return measurements


def summarize_opportunities(measurements: List[AEOMeasurement], limit: int = 10) -> List[Dict[str, Any]]:
    """Return planner-ready opportunities from missed or weak prompts."""
    candidates = [
        measurement for measurement in measurements
        if measurement.missed or measurement.share_of_mentions < 0.25 or measurement.ai_referrals == 0
    ]
    candidates.sort(key=lambda item: (item.missed, -len(item.cited_competitors), -item.share_of_mentions), reverse=True)

    return [
        {
            'prompt': item.prompt,
            'reason': 'missed_prompt' if item.missed else 'low_visibility',
            'share_of_mentions': item.share_of_mentions,
            'ai_referrals': item.ai_referrals,
            'cited_competitors': item.cited_competitors,
            'brief': (
                f"Create or refresh content answering '{item.prompt}' with direct FAQ coverage, "
                f"competitor contrast against {', '.join(item.cited_competitors[:3]) or 'current cited competitors'}, "
                "and stronger evidence density."
            )
        }
        for item in candidates[:limit]
    ]
