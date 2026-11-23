"""
Content Validator - Post-Generation Quality Assurance

Validates research content against quality standards from research_config.json:
- Structure validation (heading hierarchy, section word counts, tables)
- FAQ optimization (helpful coverage, concise answers, direct format)
- AEO readiness scoring (0-100 based on useful, crawlable, source-backed content)
- Table quality (context-aware breakdowns, numerical ranges)
- Data density and citation coverage

Integrates with:
- research_config.json (quality thresholds)
- fact_checker.py (citation analysis)
- context_intelligence.py (table validation)
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class ValidationReport:
    """Structured validation report with scores and recommendations."""

    def __init__(self, content_type: str = "research"):
        self.content_type = content_type
        self.checks = []
        self.warnings = []
        self.errors = []
        self.scores = {}
        self.overall_score = 0
        self.passed = False

    def add_check(self, name: str, passed: bool, message: str, score: int = 0):
        """Add validation check result."""
        self.checks.append({
            'name': name,
            'passed': passed,
            'message': message,
            'score': score
        })

        if not passed:
            if score > 0:
                self.warnings.append(f"{name}: {message}")
            else:
                self.errors.append(f"{name}: {message}")

    def add_score(self, category: str, score: int, max_score: int):
        """Add category score."""
        self.scores[category] = {
            'score': score,
            'max': max_score,
            'percentage': (score / max_score * 100) if max_score > 0 else 0
        }

    def calculate_overall_score(self):
        """Calculate overall validation score (0-100)."""
        if not self.scores:
            self.overall_score = 0
            return

        total_score = sum(s['score'] for s in self.scores.values())
        total_max = sum(s['max'] for s in self.scores.values())

        self.overall_score = int((total_score / total_max * 100) if total_max > 0 else 0)
        self.passed = self.overall_score >= 70  # Min threshold

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for logging/output."""
        return {
            'content_type': self.content_type,
            'overall_score': self.overall_score,
            'passed': self.passed,
            'scores': self.scores,
            'checks_passed': len([c for c in self.checks if c['passed']]),
            'checks_total': len(self.checks),
            'warnings': self.warnings,
            'errors': self.errors
        }

    def __str__(self) -> str:
        """Human-readable summary."""
        status = "✅ PASSED" if self.passed else "❌ FAILED"
        return f"{status} | Score: {self.overall_score}/100 | Errors: {len(self.errors)} | Warnings: {len(self.warnings)}"


class ContentValidator:
    """Validate research content against quality standards."""

    def __init__(self, research_config_path: Optional[Path] = None, icp_config_path: Optional[Path] = None):
        """
        Initialize validator with research quality config.

        Args:
            research_config_path: Path to research_config.json (optional)
            icp_config_path: Path to icp_config.json (optional)
        """
        if research_config_path is None:
            research_config_path = Path(__file__).parent.parent / 'config' / 'research_config.json'

        if icp_config_path is None:
            icp_config_path = Path(__file__).parent.parent / 'config' / 'icp_config.json'

        self.config = self._load_config(research_config_path)
        self.icp_config = self._load_icp_config(icp_config_path)

        # SEO optimization rules (banned words, title/meta limits). Used for the
        # body-level banned-word check and keyword-stuffing thresholds. Loaded
        # leniently — an empty dict just disables those AI-SEO sub-checks.
        config_dir = Path(__file__).parent.parent / 'config'
        self.seo_config = self._load_json_lenient(config_dir / 'seo_optimization.json')
        self.quality_rubric = self._load_json_lenient(config_dir / 'quality_rubric.json')
        self.proper_nouns = self._load_json_lenient(config_dir / 'proper_nouns.json')

    def _load_config(self, path: Path) -> Dict[str, Any]:
        """Load research quality configuration."""
        try:
            with open(path, 'r') as f:
                config = json.load(f)
                logger.debug(f"✅ Loaded research quality config")
                return config
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"⚠️  Could not load research_config.json: {e}, using defaults")
            return self._get_default_config()

    def _load_icp_config(self, path: Path) -> Dict[str, Any]:
        """Load ICP configuration for industry validation."""
        try:
            with open(path, 'r') as f:
                config = json.load(f)
                logger.debug(f"✅ Loaded ICP config for industry validation")
                return config
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"⚠️  Could not load icp_config.json: {e}, industry validation disabled")
            return {}

    def _load_json_lenient(self, path: Path) -> Dict[str, Any]:
        """Load an optional JSON config; return {} (and warn) if unavailable."""
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"⚠️  Could not load {path.name}: {e}; related checks disabled")
            return {}

    def _get_default_config(self) -> Dict[str, Any]:
        """Fallback default configuration."""
        return {
            'word_count_requirements': {
                'min_total_words': 3500,
                'methodology': {'min': 300, 'max': 400, 'required': True},
                'faq_section': {'min': 300, 'max': 500, 'min_questions': 12}
            },
            'faq_optimization': {
                'min_questions': 12,
                'answer_word_count': {'min': 40, 'max': 60}
            },
            'aeo_readiness': {
                'target_score': 85,
                'scoring_factors': {
                    'faq_coverage': 10,
                    'direct_answers': 15,
                    'source_quality': 15,
                    'crawl_snippet_readiness': 20,
                    'freshness': 10,
                    'duplicate_avoidance': 10,
                    'non_commodity_signals': 15,
                    'schema_markup': 5
                }
            }
        }

    def validate_structure(
        self,
        content: str,
        frontmatter: Dict[str, Any]
    ) -> ValidationReport:
        """
        Validate content structure (headings, sections, tables).

        Args:
            content: Full MDX content
            frontmatter: Frontmatter dict

        Returns:
            ValidationReport with structure checks

        Validates:
        - Heading hierarchy (H1 → H2 → H3, no skips)
        - Mandatory sections present
        - Section word counts meet minimums
        - Table presence and formatting
        """
        report = ValidationReport("structure")

        # Extract headings
        headings = self._extract_headings(content)

        # 1. Heading hierarchy validation
        hierarchy_valid, hierarchy_issues = self._validate_heading_hierarchy(headings)
        report.add_check(
            "Heading Hierarchy",
            hierarchy_valid,
            f"Found {len(hierarchy_issues)} hierarchy issues" if hierarchy_issues else "Valid H1→H2→H3 hierarchy",
            score=10 if hierarchy_valid else 0
        )

        # 2. Mandatory sections validation
        mandatory = self.config.get('structure_requirements', {}).get('mandatory_sections', [])
        missing_sections = self._check_mandatory_sections(headings, mandatory)
        report.add_check(
            "Mandatory Sections",
            len(missing_sections) == 0,
            f"Missing: {', '.join(missing_sections)}" if missing_sections else f"All {len(mandatory)} required sections present",
            score=20 if len(missing_sections) == 0 else max(0, 20 - len(missing_sections) * 4)
        )

        # 3. Section word counts
        section_counts = self._count_section_words(content, headings)
        word_count_issues = self._validate_section_word_counts(section_counts)
        report.add_check(
            "Section Word Counts",
            len(word_count_issues) == 0,
            f"{len(word_count_issues)} sections below minimum" if word_count_issues else "All sections meet minimum word counts",
            score=15 if len(word_count_issues) == 0 else max(0, 15 - len(word_count_issues) * 3)
        )

        # 4. Table validation
        tables = self._extract_tables(content)
        min_tables = self.config.get('structure_requirements', {}).get('min_tables', 2)
        report.add_check(
            "Table Presence",
            len(tables) >= min_tables,
            f"Found {len(tables)} tables (min: {min_tables})",
            score=10 if len(tables) >= min_tables else max(0, int(len(tables) / min_tables * 10))
        )

        table_quality_score, table_issues = self._validate_table_quality(tables)
        report.add_check(
            "Table Quality",
            len(table_issues) == 0,
            f"{len(table_issues)} table quality issues" if table_issues else f"{len(tables)} tables formatted correctly",
            score=table_quality_score
        )

        # Calculate score
        report.add_score("structure", sum(c['score'] for c in report.checks), 65)
        report.calculate_overall_score()

        logger.info(f"📐 Structure validation: {report}")
        return report

    def _extract_headings(self, content: str) -> List[Dict[str, Any]]:
        """Extract all headings with levels and text."""
        headings = []
        pattern = r'^(#{1,6})\s+(.+)$'

        for match in re.finditer(pattern, content, re.MULTILINE):
            level = len(match.group(1))
            text = match.group(2).strip()
            headings.append({
                'level': level,
                'text': text,
                'position': match.start(),
                'end_position': match.end(),
            })

        return headings

    def _validate_heading_hierarchy(
        self,
        headings: List[Dict[str, Any]]
    ) -> Tuple[bool, List[str]]:
        """Check for heading hierarchy violations (skipped levels)."""
        issues = []
        prev_level = 0

        for i, heading in enumerate(headings):
            level = heading['level']

            # Check for skipped levels (e.g., H1 → H3)
            if level > prev_level + 1 and prev_level > 0:
                issues.append(f"Skipped level at '{heading['text'][:50]}' (H{prev_level} → H{level})")

            prev_level = level

        return len(issues) == 0, issues

    def _check_mandatory_sections(
        self,
        headings: List[Dict[str, Any]],
        mandatory: List[str]
    ) -> List[str]:
        """Check if mandatory sections are present."""
        heading_texts = [h['text'].lower() for h in headings]
        missing = []

        for section in mandatory:
            section_lower = section.lower()
            # Fuzzy match (section name anywhere in heading)
            if not any(section_lower in text for text in heading_texts):
                missing.append(section)

        return missing

    def _count_section_words(
        self,
        content: str,
        headings: List[Dict[str, Any]]
    ) -> Dict[str, int]:
        """Count words in each H2 section."""
        counts = {}

        h2_headings = [h for h in headings if h['level'] == 2]

        for i, heading in enumerate(h2_headings):
            # Extract text between this H2 and next H2 (or end)
            start = heading['position']
            end = h2_headings[i + 1]['position'] if i + 1 < len(h2_headings) else len(content)

            section_text = content[start:end]
            word_count = len(section_text.split())

            counts[heading['text']] = word_count

        return counts

    def _validate_section_word_counts(
        self,
        section_counts: Dict[str, int]
    ) -> List[str]:
        """Validate section word counts against config minimums."""
        issues = []
        word_reqs = self.config.get('word_count_requirements', {})

        # Check specific sections
        for section_name, count in section_counts.items():
            section_lower = section_name.lower()

            # Match to config requirements
            if 'methodology' in section_lower:
                min_words = word_reqs.get('methodology', {}).get('min', 300)
                if count < min_words:
                    issues.append(f"'{section_name}': {count} words (min: {min_words})")

            elif 'case' in section_lower and 'stud' in section_lower:
                min_words = word_reqs.get('case_studies_section', {}).get('min', 750)
                if count < min_words:
                    issues.append(f"'{section_name}': {count} words (min: {min_words})")

            elif 'benchmark' in section_lower or 'comparative' in section_lower:
                min_words = word_reqs.get('comparative_benchmarking', {}).get('min', 400)
                if count < min_words:
                    issues.append(f"'{section_name}': {count} words (min: {min_words})")

        return issues

    def _extract_tables(self, content: str) -> List[str]:
        """Extract markdown tables from content."""
        # Match markdown table pattern
        table_pattern = r'(\|.+\|\n\|[-:\s|]+\|\n(?:\|.+\|\n)+)'
        tables = re.findall(table_pattern, content)
        return tables

    def _validate_table_quality(
        self,
        tables: List[str]
    ) -> Tuple[int, List[str]]:
        """
        Validate table quality (rows, columns, formatting).

        Returns:
            Tuple of (score out of 10, list of issues)
        """
        if not tables:
            return 0, ["No tables found"]

        issues = []
        quality_reqs = self.config.get('table_quality_requirements', {})
        min_rows = quality_reqs.get('min_rows', 4)
        min_cols = quality_reqs.get('min_columns', 3)

        for i, table in enumerate(tables, 1):
            rows = table.strip().split('\n')

            # Check row count (excluding header and separator)
            data_rows = [r for r in rows if not re.match(r'\|[-:\s|]+\|', r)]
            if len(data_rows) < min_rows + 1:  # +1 for header
                issues.append(f"Table {i}: Only {len(data_rows) - 1} data rows (min: {min_rows})")

            # Check column count
            if rows:
                col_count = rows[0].count('|') - 1  # -1 for empty edges
                if col_count < min_cols:
                    issues.append(f"Table {i}: Only {col_count} columns (min: {min_cols})")

            # Check for consistent column counts
            col_counts = [row.count('|') for row in rows]
            if len(set(col_counts)) > 1:
                issues.append(f"Table {i}: Inconsistent column counts")

        # Score: 10 points minus 2 per issue (min 0)
        score = max(0, 10 - len(issues) * 2)
        return score, issues

    def validate_faq_answers(
        self,
        faq_list: List[Dict[str, str]]
    ) -> ValidationReport:
        """
        Validate FAQ answers for AEO optimization.

        Args:
            faq_list: List of {question, answer} dicts from frontmatter

        Returns:
            ValidationReport with FAQ checks

        Validates:
        - FAQ coverage (prioritize real questions; 12-15 is a soft upper target)
        - Answer length (concise answers are preferred, but do not pad)
        - Direct answer format (answer in first sentence)
        """
        report = ValidationReport("faq")
        faq_config = self.config.get('faq_optimization', {})

        min_questions = faq_config.get('min_questions', 12)
        max_questions = faq_config.get('max_questions', 15)
        word_config = faq_config.get('answer_word_count', {})
        min_words = word_config.get('min', 40)
        max_words = word_config.get('max', 60)

        # 1. FAQ count validation
        faq_count = len(faq_list)
        optimal_count = min_questions <= faq_count <= max_questions
        has_useful_coverage = faq_count > 0

        report.add_check(
            "FAQ Coverage",
            optimal_count,
            f"{faq_count} FAQs (target useful set: up to {max_questions}; do not pad weak questions)",
            score=25 if optimal_count else (10 if has_useful_coverage else 1)
        )

        # 2. Answer length validation
        length_issues = []
        for i, faq in enumerate(faq_list, 1):
            answer = faq.get('answer', '')
            word_count = len(answer.split())

            if word_count < min_words:
                length_issues.append(f"FAQ {i}: {word_count} words (min: {min_words})")
            elif word_count > max_words:
                length_issues.append(f"FAQ {i}: {word_count} words (max: {max_words})")

        optimal_length_ratio = 1 - (len(length_issues) / max(faq_count, 1))
        report.add_check(
            "Concise FAQ Answers",
            len(length_issues) == 0,
            f"{len(length_issues)} answers outside concise range" if length_issues else f"All {faq_count} answers are concise and direct",
            score=30 if len(length_issues) == 0 else max(1, int(optimal_length_ratio * 30))
        )

        # 3. Direct answer format validation
        direct_answer_count = 0
        for faq in faq_list:
            answer = faq.get('answer', '')
            # Check if first sentence is direct (contains key question words)
            first_sentence = answer.split('.')[0] if '.' in answer else answer
            if len(first_sentence.split()) >= 10:  # Substantial first sentence
                direct_answer_count += 1

        direct_answer_ratio = direct_answer_count / max(faq_count, 1)
        report.add_check(
            "Direct Answer Format",
            direct_answer_ratio >= 0.8,
            f"{direct_answer_count}/{faq_count} FAQs have direct first-sentence answers",
            score=max(1, int(direct_answer_ratio * 20))
        )

        # Calculate score
        report.add_score("faq", sum(c['score'] for c in report.checks), 75)
        report.calculate_overall_score()

        logger.info(f"❓ FAQ validation: {report}")
        return report

    def validate_aeo_gates(
        self,
        content: str,
        frontmatter: Dict[str, Any],
        has_schema: bool = False
    ) -> ValidationReport:
        """
        Validate AEO-specific gates beyond FAQ basics.

        These gates are warning-oriented. Google-aligned AEO is normal SEO:
        crawlable pages, snippet eligibility, useful content, evidence, and
        duplicate avoidance matter more than rigid markup or FAQ quotas.
        """
        report = ValidationReport("aeo_gates")
        aeo_config = self.config.get('aeo_optimization', {})

        answer_issues = self._validate_atomic_answer_blocks(content)
        self._add_warning_check(
            report,
            "Atomic Answer Blocks",
            len(answer_issues) == 0,
            f"{len(answer_issues)} H2/H3 sections could use clearer concise lead answers" if answer_issues else "Important H2/H3 sections start with concise lead answers",
            15
        )

        heading_ratio = self._query_heading_ratio(content)
        target_ratio = aeo_config.get('query_mirroring', {}).get('target_question_heading_ratio', 0.5)
        self._add_warning_check(
            report,
            "Query-Style Headings",
            heading_ratio >= target_ratio,
            f"{heading_ratio:.0%} H2/H3 headings are question-style (target: {target_ratio:.0%})",
            15
        )

        evidence_issues = self._validate_evidence_quota(content)
        self._add_warning_check(
            report,
            "Evidence Quota",
            len(evidence_issues) == 0,
            f"{len(evidence_issues)} H2 sections below evidence quota" if evidence_issues else "Every H2 section has enough evidence",
            20
        )

        micro_table_issues = self._validate_micro_tables(content)
        self._add_warning_check(
            report,
            "Micro Tables",
            len(micro_table_issues) == 0,
            f"{len(micro_table_issues)} micro-table issues" if micro_table_issues else "Micro-table coverage is acceptable",
            10
        )

        citation_density = self._citation_density(content)
        min_citations = self.config.get('citation_requirements', {}).get('min_inline_citations', 15)
        self._add_warning_check(
            report,
            "Citation Density",
            citation_density >= min_citations,
            f"{citation_density} inline citations found (target: {min_citations}+)",
            15
        )

        duplicate_issues = self._detect_duplicate_passages(content)
        self._add_warning_check(
            report,
            "Duplicate Passages",
            len(duplicate_issues) == 0,
            f"{len(duplicate_issues)} repeated passage patterns detected" if duplicate_issues else "No duplicate passages detected",
            10
        )

        frontmatter_issues = self._validate_frontmatter_readiness(frontmatter)
        self._add_warning_check(
            report,
            "Frontmatter Readiness",
            len(frontmatter_issues) == 0,
            f"Missing or unsafe fields: {', '.join(frontmatter_issues)}" if frontmatter_issues else "Canonical, robots, and FAQ fields ready",
            10
        )

        self._add_warning_check(
            report,
            "Schema Readiness",
            has_schema,
            "Structured data present as an SEO aid" if has_schema else "Structured data missing or not detected",
            5
        )

        report.add_score("aeo_gates", sum(c['score'] for c in report.checks), 100)
        report.calculate_overall_score()
        return report

    def _add_warning_check(self, report: ValidationReport, name: str, passed: bool, message: str, score: int):
        """Add a non-blocking warning check."""
        report.add_check(name, passed, message, score=score if passed else 1)

    def _validate_atomic_answer_blocks(self, content: str) -> List[str]:
        """Check whether H2/H3 sections start with a 40-60 word answer block."""
        headings = [h for h in self._extract_headings(content) if h['level'] in (2, 3)]
        limits = self.config.get('aeo_optimization', {}).get('atomic_answer_word_count', {})
        min_words = limits.get('min', 40)
        max_words = limits.get('max', 60)
        issues = []

        for i, heading in enumerate(headings):
            start = heading['position'] + len('#' * heading['level']) + 1 + len(heading['text'])
            end = headings[i + 1]['position'] if i + 1 < len(headings) else len(content)
            section = content[start:end].strip()
            first_block = self._first_text_block(section)
            if not first_block:
                issues.append(heading['text'])
                continue
            word_count = len(first_block.split())
            if word_count < min_words or word_count > max_words:
                issues.append(f"{heading['text']}: {word_count} words")

        return issues

    def _first_text_block(self, section: str) -> str:
        """Return first non-empty prose block after a heading."""
        for block in re.split(r'\n\s*\n', section):
            block = block.strip()
            if not block or block.startswith(('|', '-', '*', '<', '```')):
                continue
            return re.sub(r'\s+', ' ', re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', block))
        return ""

    def _query_heading_ratio(self, content: str) -> float:
        """Ratio of H2/H3 headings that look like natural-language queries."""
        headings = [h['text'].strip().lower() for h in self._extract_headings(content) if h['level'] in (2, 3)]
        if not headings:
            return 0.0
        starters = tuple(self.config.get('aeo_optimization', {}).get('query_mirroring', {}).get('question_starters', []))
        query_count = sum(1 for text in headings if text.endswith('?') or text.startswith(starters))
        return query_count / len(headings)

    def _validate_evidence_quota(self, content: str) -> List[str]:
        """Ensure each H2 section has enough concrete evidence markers."""
        headings = self._extract_headings(content)
        h2_headings = [h for h in headings if h['level'] == 2]
        min_items = self.config.get('aeo_optimization', {}).get('evidence_quota', {}).get('min_evidence_items_per_h2', 2)
        issues = []

        for i, heading in enumerate(h2_headings):
            start = heading['position']
            end = h2_headings[i + 1]['position'] if i + 1 < len(h2_headings) else len(content)
            section = content[start:end]
            evidence_count = self._count_evidence_items(section)
            if evidence_count < min_items:
                issues.append(f"{heading['text']}: {evidence_count}/{min_items}")

        return issues

    def _count_evidence_items(self, text: str) -> int:
        """Count citations, metrics, dates, currency, and named-source markers."""
        patterns = [
            r'\[\d+\]',
            r'\[\[source\]',
            r'\d+(?:\.\d+)?%',
            r'\$\d+(?:,\d{3})*(?:\.\d+)?[KMB]?',
            r'\b20\d{2}\b',
            r'\b(?:Gartner|Forrester|McKinsey|HubSpot|Salesforce|Google|Meta|LinkedIn)\b',
            r'\b\d+(?:\.\d+)?x\b',
        ]
        return sum(len(re.findall(pattern, text, re.IGNORECASE)) for pattern in patterns)

    def _validate_micro_tables(self, content: str) -> List[str]:
        """Validate compact markdown tables for AEO extraction."""
        tables = self._extract_tables(content)
        rules = self.config.get('aeo_optimization', {}).get('micro_tables', {})
        min_count = rules.get('min_count', 2)
        max_rows = rules.get('max_rows', 8)
        min_cols = rules.get('min_columns', 3)
        issues = []

        if len(tables) < min_count:
            issues.append(f"Only {len(tables)} tables found (min: {min_count})")

        for i, table in enumerate(tables, 1):
            rows = [row for row in table.strip().split('\n') if row.strip()]
            data_rows = [row for row in rows if not re.match(r'\|[-:\s|]+\|', row)]
            if len(data_rows) - 1 > max_rows:
                issues.append(f"Table {i}: {len(data_rows) - 1} data rows (max: {max_rows})")
            if rows and rows[0].count('|') - 1 < min_cols:
                issues.append(f"Table {i}: fewer than {min_cols} columns")

        return issues

    def _citation_density(self, content: str) -> int:
        """Count inline citation markers."""
        return len(re.findall(r'\[\d+\]|\[\[source\]', content))

    def _detect_duplicate_passages(self, content: str) -> List[str]:
        """Detect repeated normalized passages."""
        dedupe = self.config.get('aeo_optimization', {}).get('dedupe', {})
        passage_words = dedupe.get('passage_word_count', 18)
        clean = re.sub(r'```[\s\S]*?```', ' ', content)
        clean = re.sub(r'^#{1,6}\s+.*$', ' ', clean, flags=re.MULTILINE)
        words = re.findall(r'\b\w+\b', clean.lower())
        seen = {}
        duplicates = []

        for i in range(0, max(len(words) - passage_words + 1, 0), passage_words):
            passage = ' '.join(words[i:i + passage_words])
            if not passage:
                continue
            if passage in seen:
                duplicates.append(passage)
            else:
                seen[passage] = i

        return duplicates[:10]

    def _validate_frontmatter_readiness(self, frontmatter: Dict[str, Any]) -> List[str]:
        """Validate canonical, robots, FAQ, and optional video fields."""
        issues = []
        seo = frontmatter.get('seo') or {}

        if not frontmatter.get('canonical'):
            issues.append('canonical')
        if seo.get('noindex') is not False:
            issues.append('seo.noindex:false')
        if seo.get('nofollow') is not False:
            issues.append('seo.nofollow:false')
        if not isinstance(frontmatter.get('faq') or [], list):
            issues.append('faq:list')
        if not frontmatter.get('description') and not seo.get('description'):
            issues.append('description')
        if not frontmatter.get('updatedDate'):
            issues.append('updatedDate')
        if 'videoEmbed' in frontmatter and not isinstance(frontmatter.get('videoEmbed'), (str, dict)):
            issues.append('videoEmbed')

        return issues

    def score_aeo_readiness(
        self,
        content: str,
        frontmatter: Dict[str, Any],
        has_schema: bool = False,
        citation_coverage: float = 0.0
    ) -> ValidationReport:
        """
        Score content for Google-aligned AEO readiness (0-100).

        Args:
            content: Full MDX content
            frontmatter: Frontmatter dict
            has_schema: Whether JSON-LD schema is present
            citation_coverage: Ratio of claims with citations (0.0-1.0)

        Returns:
            ValidationReport with AEO score

        Scoring factors:
        - FAQ coverage (10 pts): helpful questions without padding
        - Direct answers (15 pts): important sections answer the query early
        - Source quality (15 pts): claims are supported by citations and named sources
        - Crawl/snippet readiness (20 pts): canonical, robots, title/description
        - Freshness (10 pts): publish and updated dates available
        - Duplicate avoidance (10 pts): repeated commodity passages are minimized
        - Non-commodity signals (15 pts): examples, analysis, methodology, data
        - Schema markup (5 pts): structured data present as an SEO/rich-result aid
        """
        report = ValidationReport("aeo")
        # 1. FAQ Coverage Score (10 pts)
        faq_list = frontmatter.get('faq', [])
        faq_count = len(faq_list)
        faq_target = self.config.get('faq_optimization', {}).get('min_questions', 12)
        faq_score = min(10, int(faq_count / faq_target * 10)) if faq_target else 0

        report.add_check(
            "Helpful FAQ Coverage",
            faq_count > 0,
            f"{faq_count} FAQs present (12-15 is a soft upper target, not a padding requirement)",
            score=faq_score
        )

        # 2. Direct Answers Score (15 pts)
        # Count sections with direct first-sentence answers
        direct_answer_sections = self._count_direct_answer_sections(content)
        direct_answer_score = min(15, direct_answer_sections * 2)

        report.add_check(
            "Direct Answer Sections",
            direct_answer_sections >= 8,
            f"{direct_answer_sections} sections lead with direct answers (target: 8+)",
            score=direct_answer_score
        )

        # 3. Source Quality Score (15 pts)
        citation_density = self._citation_density(content)
        source_score = min(15, int((citation_coverage * 10) + min(5, citation_density)))

        report.add_check(
            "Source-Backed Claims",
            citation_coverage >= 0.8 or citation_density >= self.config.get('citation_requirements', {}).get('min_inline_citations', 15),
            f"{int(citation_coverage * 100)}% claim coverage; {citation_density} inline citations",
            score=source_score
        )

        # 4. Crawl and Snippet Readiness Score (20 pts)
        crawl_issues = self._validate_crawl_snippet_readiness(frontmatter)
        crawl_score = max(0, 20 - (len(crawl_issues) * 5))

        report.add_check(
            "Crawl and Snippet Readiness",
            not crawl_issues,
            "Canonical, robots, title, and description are ready" if not crawl_issues else f"Missing or unsafe fields: {', '.join(crawl_issues)}",
            score=crawl_score
        )

        # 5. Freshness Score (10 pts)
        freshness_issues = self._validate_freshness(frontmatter)
        freshness_score = max(0, 10 - (len(freshness_issues) * 5))

        report.add_check(
            "Freshness Signals",
            not freshness_issues,
            "Publish and updated dates present" if not freshness_issues else f"Missing fields: {', '.join(freshness_issues)}",
            score=freshness_score
        )

        # 6. Duplicate Avoidance Score (10 pts)
        duplicate_issues = self._detect_duplicate_passages(content)
        duplicate_score = max(0, 10 - len(duplicate_issues) * 2)

        report.add_check(
            "Duplicate Avoidance",
            not duplicate_issues,
            "No repeated commodity passages detected" if not duplicate_issues else f"{len(duplicate_issues)} repeated passage patterns detected",
            score=duplicate_score
        )

        # 7. Non-Commodity Content Signals Score (15 pts)
        non_commodity_signals = self._count_non_commodity_signals(content)
        non_commodity_score = min(15, non_commodity_signals * 3)

        report.add_check(
            "Non-Commodity Usefulness",
            non_commodity_signals >= 4,
            f"{non_commodity_signals} useful-content signals found",
            score=non_commodity_score
        )

        # 8. Schema Markup Score (5 pts)
        schema_score = 5 if has_schema else 0

        report.add_check(
            "Structured Data Aid",
            has_schema,
            "Structured data present as an SEO/rich-result aid" if has_schema else "No structured data detected",
            score=schema_score
        )

        # Calculate overall AEO score
        report.add_score("aeo_readiness", sum(c['score'] for c in report.checks), 100)
        report.calculate_overall_score()

        logger.info(f"🎯 AEO readiness: {report}")
        return report

    def _validate_crawl_snippet_readiness(self, frontmatter: Dict[str, Any]) -> List[str]:
        """Validate fields that help pages remain crawlable and snippet-eligible."""
        issues = []
        seo = frontmatter.get('seo') or {}
        title = frontmatter.get('title') or seo.get('title')
        description = frontmatter.get('description') or seo.get('description')

        if not frontmatter.get('canonical'):
            issues.append('canonical')
        if seo.get('noindex') is not False:
            issues.append('seo.noindex:false')
        if seo.get('nofollow') is not False:
            issues.append('seo.nofollow:false')
        if not title:
            issues.append('title')
        if not description:
            issues.append('description')

        return issues

    def _validate_freshness(self, frontmatter: Dict[str, Any]) -> List[str]:
        """Validate publish/update dates for page freshness and transparency."""
        issues = []
        if not frontmatter.get('publishDate'):
            issues.append('publishDate')
        if not frontmatter.get('updatedDate'):
            issues.append('updatedDate')
        return issues

    def _count_non_commodity_signals(self, content: str) -> int:
        """Count signals that indicate original, useful, non-boilerplate content."""
        signal_patterns = [
            r'\bcase stud(?:y|ies)\b',
            r'\bmethodolog(?:y|ical)\b',
            r'\bbenchmark(?:s|ing)?\b',
            r'\bexample\b',
            r'\bscenario\b',
            r'\btrade-?off\b',
            r'\blimitation(?:s)?\b',
            r'\b\d+(?:\.\d+)?%',
            r'\$\d+(?:,\d{3})*(?:\.\d+)?[KMB]?',
        ]
        return sum(1 for pattern in signal_patterns if re.search(pattern, content, re.IGNORECASE))

    def _count_direct_answer_sections(self, content: str) -> int:
        """Count H2 sections that start with direct answer sentences."""
        headings = self._extract_headings(content)
        h2_headings = [h for h in headings if h['level'] == 2]

        direct_count = 0
        for i, heading in enumerate(h2_headings):
            # Extract first paragraph immediately after the heading line
            start = heading.get('end_position', heading['position'] + heading['level'] + 1 + len(heading['text']))
            end = h2_headings[i + 1]['position'] if i + 1 < len(h2_headings) else len(content)

            section_text = content[start:end].lstrip()
            first_para = section_text.split('\n\n')[0] if '\n\n' in section_text else section_text

            # Check if first sentence is substantial (15+ words)
            first_sentence = first_para.split('.')[0] if '.' in first_para else first_para
            if len(first_sentence.split()) >= 15:
                direct_count += 1

        return direct_count

    def _count_data_points(self, content: str) -> int:
        """Count statistical data points (percentages, metrics, dollar amounts)."""
        patterns = [
            r'\d+(?:\.\d+)?%',  # Percentages
            r'\$\d+(?:,\d{3})*(?:\.\d+)?[KMB]?',  # Dollar amounts
            r'\d+x',  # Multipliers
            r'\d+\s*(?:hours?|days?|weeks?|months?)',  # Time periods
        ]

        count = 0
        for pattern in patterns:
            matches = re.findall(pattern, content)
            count += len(matches)

        return count

    def validate_industry_table_coverage(
        self,
        content: str,
        keyword: str
    ) -> ValidationReport:
        """
        Validate that tables use real ICP industries from config, not hardcoded values.

        Args:
            content: Full MDX content
            keyword: Research keyword (used to detect expected industry)

        Returns:
            ValidationReport with industry coverage checks

        Validates:
        - Tables contain industry breakdowns (not generic data)
        - Industries used match icp_config.json (not hardcoded)
        - Detected industry from keyword appears in tables
        - No generic placeholder industries (e.g., "Industry A", "Various")
        """
        report = ValidationReport("industry_coverage")

        if not self.icp_config:
            logger.warning("⚠️  ICP config not loaded, skipping industry validation")
            report.add_check(
                "ICP Config Available",
                False,
                "ICP config not loaded - industry validation skipped",
                score=0
            )
            report.calculate_overall_score()
            return report

        # Extract valid industries from ICP config
        valid_industries = self._extract_valid_industries()
        logger.debug(f"📋 Valid ICP industries: {list(valid_industries.keys())}")

        # Detect expected industry from keyword
        detected_industry = self._detect_keyword_industry(keyword, valid_industries)

        # Extract tables from content
        tables = self._extract_tables(content)

        if not tables:
            report.add_check(
                "Tables Present",
                False,
                "No tables found in content - cannot validate industry coverage",
                score=0
            )
            report.calculate_overall_score()
            return report

        # Check if tables contain industry-specific data
        tables_with_industries = 0
        hardcoded_industry_issues = []
        detected_industry_present = False

        for i, table in enumerate(tables, 1):
            table_lower = table.lower()

            # Check for industry mentions (count each table at most once)
            industries_in_table = []
            for industry_key, industry_subs in valid_industries.items():
                # Check for industry category name
                industry_display = industry_key.replace('_', ' ').title()
                if industry_display.lower() in table_lower:
                    industries_in_table.append(industry_display)
                    if industry_key == detected_industry:
                        detected_industry_present = True

                # Check for sub-industries
                for sub in industry_subs:
                    if sub.lower() in table_lower:
                        industries_in_table.append(sub)
                        if industry_key == detected_industry:
                            detected_industry_present = True

            if industries_in_table:
                tables_with_industries += 1

            # Check for hardcoded/generic industry names
            generic_patterns = [
                r'industry\s+[a-d]',  # "Industry A", "Industry B"
                r'generic\s+industry',
                r'example\s+industry',
                r'various\s+industri',
                r'sample\s+data',
                r'\[industry\s+name\]'
            ]

            for pattern in generic_patterns:
                if re.search(pattern, table_lower):
                    hardcoded_industry_issues.append(f"Table {i}: Contains generic placeholder '{pattern}'")

        # 1. Industry presence validation (30 pts)
        industry_coverage_ratio = tables_with_industries / max(len(tables), 1)
        industry_score = int(industry_coverage_ratio * 30)

        report.add_check(
            "Industry-Specific Tables",
            tables_with_industries > 0,
            f"{tables_with_industries}/{len(tables)} tables contain ICP industry data",
            score=industry_score
        )

        # 2. No hardcoded/generic industries (30 pts)
        no_hardcoded = len(hardcoded_industry_issues) == 0
        hardcoded_score = 30 if no_hardcoded else max(0, 30 - len(hardcoded_industry_issues) * 10)

        report.add_check(
            "No Generic Placeholders",
            no_hardcoded,
            f"{len(hardcoded_industry_issues)} generic/hardcoded industry issues" if hardcoded_industry_issues else "All industries from ICP config",
            score=hardcoded_score
        )

        # 3. Detected industry coverage (40 pts)
        if detected_industry:
            detected_industry_display = detected_industry.replace('_', ' ').title()
            report.add_check(
                f"Keyword Industry Coverage ({detected_industry_display})",
                detected_industry_present,
                f"Tables cover detected industry '{detected_industry_display}' for keyword '{keyword}'" if detected_industry_present else f"Missing expected industry '{detected_industry_display}' for '{keyword}'",
                score=40 if detected_industry_present else 0
            )
        else:
            report.add_check(
                "Keyword Industry Detection",
                False,
                f"Could not detect specific industry from keyword '{keyword}' - validation limited",
                score=20  # Partial credit if we can't detect industry
            )

        # Calculate score
        report.add_score("industry_coverage", sum(c['score'] for c in report.checks), 100)
        report.calculate_overall_score()

        logger.info(f"🏭 Industry coverage validation: {report}")
        return report

    def _extract_valid_industries(self) -> Dict[str, List[str]]:
        """
        Extract valid industry categories and sub-industries from ICP config.

        Returns:
            Dict mapping industry category (e.g., 'home_services') to list of sub-industries
        """
        industries = {}

        # Navigate ICP config structure
        sales_led = self.icp_config.get('primary_icps', {}).get('sales_led_services', {})
        industry_dict = sales_led.get('industries', {})

        for category, subs in industry_dict.items():
            if isinstance(subs, list):
                industries[category] = subs

        return industries

    def _detect_keyword_industry(self, keyword: str, valid_industries: Dict[str, List[str]]) -> Optional[str]:
        """
        Detect which ICP industry category the keyword relates to.

        Args:
            keyword: Research keyword
            valid_industries: Dict of industry categories and sub-industries

        Returns:
            Industry category key (e.g., 'home_services') or None
        """
        keyword_lower = keyword.lower()
        keyword_words = set(keyword_lower.split())

        # Check each industry category and sub-industries
        for category, subs in valid_industries.items():
            # Check category name
            category_display = category.replace('_', ' ')
            if category_display in keyword_lower:
                logger.info(f"🎯 Detected industry '{category}' from keyword '{keyword}' (category match)")
                return category

            # Check sub-industries with flexible word matching
            for sub in subs:
                sub_lower = sub.lower()

                # Exact substring match (e.g., "HVAC" in "HVAC lead costs")
                if sub_lower in keyword_lower:
                    logger.info(f"🎯 Detected industry '{category}' from keyword '{keyword}' (sub-industry: {sub})")
                    return category

                # Word-level match (e.g., "solar" from "solar installers" matches "solar lead generation")
                sub_words = set(sub_lower.split())
                # Filter out common words (installers, brokers, clinics, etc.)
                sub_significant_words = {w for w in sub_words if w not in ['installers', 'brokers', 'clinics', 'practices', 'firms', 'companies', 'services', 'providers']}

                # If any significant word from sub-industry appears in keyword, it's a match
                if sub_significant_words & keyword_words:
                    logger.info(f"🎯 Detected industry '{category}' from keyword '{keyword}' (word match: {sub})")
                    return category

        logger.debug(f"❓ Could not detect specific industry from keyword '{keyword}'")
        return None

    # ------------------------------------------------------------------ #
    # AI-SEO / Princeton GEO citability dimensions (deterministic).
    # These fill the three citability factors the validator was missing:
    # keyword-stuffing penalty, process structure, and author attribution,
    # plus a body-level banned-word check (titles were checked, bodies were
    # not). Each mirrors the existing validate_* → ValidationReport contract.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Return the MDX body with any leading YAML frontmatter removed."""
        m = re.match(r"^---\n.*?\n---\n(.*)$", content, re.DOTALL)
        return m.group(1) if m else content

    @staticmethod
    def _word_tokens(text: str) -> List[str]:
        return re.findall(r"[a-zA-Z][a-zA-Z'-]*", text.lower())

    def validate_keyword_stuffing(self, content: str, keyword: str) -> ValidationReport:
        """Flag keyword over-optimization (Princeton GEO: stuffing is -10%).

        Computes exact-phrase keyword density over the body. Healthy density is
        roughly 0.5%-2.5%; above the configured ceiling we treat it as stuffing
        and dock the score. Natural, non-stuffed content earns full marks.
        """
        report = ValidationReport("keyword_stuffing")
        body = self._strip_frontmatter(content)
        tokens = self._word_tokens(body)
        total = len(tokens)

        rules = (self.seo_config.get('keyword_density_rules')
                 if isinstance(self.seo_config, dict) else None) or {}
        max_density = rules.get('max_density', 0.025)   # 2.5% ceiling
        ideal_max = rules.get('ideal_max_density', 0.02)

        if not keyword or total == 0:
            report.add_check("Keyword Density", True,
                             "No keyword or empty body - density check skipped", score=10)
            report.add_score("keyword_stuffing", 10, 10)
            report.calculate_overall_score()
            return report

        kw_tokens = self._word_tokens(keyword)
        # Count exact consecutive keyword-phrase occurrences.
        occurrences = 0
        if kw_tokens:
            n = len(kw_tokens)
            for i in range(total - n + 1):
                if tokens[i:i + n] == kw_tokens:
                    occurrences += 1
        density = (occurrences * len(kw_tokens)) / total if total else 0.0

        if density > max_density:
            score = max(0, int(10 - (density - max_density) / max_density * 10))
            passed = False
            msg = (f"Keyword density {density*100:.1f}% exceeds {max_density*100:.0f}% ceiling "
                   f"({occurrences}x) - GEO penalizes stuffing (-10%)")
        elif density > ideal_max:
            score = 8
            passed = True
            msg = f"Keyword density {density*100:.1f}% slightly high but acceptable ({occurrences}x)"
        else:
            score = 10
            passed = True
            msg = f"Keyword density {density*100:.1f}% is natural ({occurrences}x)"

        report.add_check("Keyword Density", passed, msg, score=score)
        report.add_score("keyword_stuffing", score, 10)
        report.calculate_overall_score()
        return report

    def validate_process_structure(self, content: str, style: Optional[str] = None) -> ValidationReport:
        """Reward numbered-list/process structure for how-to/guide content.

        AI answer engines extract HowTo steps from ordered lists. For guide-style
        content, an absence of numbered steps hurts extractability; for other
        styles this is informational only and never fails.
        """
        report = ValidationReport("process_structure")
        body = self._strip_frontmatter(content)

        ordered_items = re.findall(r"(?m)^\s*\d+\.\s+\S", body)
        has_ol_html = bool(re.search(r"<ol[\s>]", body, re.IGNORECASE))
        step_count = len(ordered_items) + (3 if has_ol_html else 0)

        process_styles = {"guide", "how-to", "howto"}
        is_process = (style or "").lower() in process_styles

        if is_process:
            if step_count >= 3:
                score, passed = 10, True
                msg = f"{step_count} numbered steps present - good HowTo extractability"
            elif step_count > 0:
                score, passed = 6, False
                msg = f"Only {step_count} numbered step(s) for a {style} - add ordered steps"
            else:
                score, passed = 0, False
                msg = f"No numbered steps in a {style} - AI cannot extract a HowTo sequence"
        else:
            # Non-process styles: numbered lists are a bonus, never required.
            score = 10 if step_count == 0 else 10
            passed = True
            msg = (f"{step_count} numbered list item(s); ordered steps optional for style "
                   f"'{style or 'unknown'}'")

        report.add_check("Process / Numbered-List Structure", passed, msg, score=score)
        report.add_score("process_structure", score, 10)
        report.calculate_overall_score()
        return report

    def validate_author_attribution(self, content: str, frontmatter: Dict[str, Any]) -> ValidationReport:
        """Score author/expert attribution (Princeton GEO: +25-30% citability).

        Looks for a byline in frontmatter and "according to [expert]" style
        attribution density in the body. Reuses FactChecker's expert-claim
        extraction when available, falling back to a local regex.
        """
        report = ValidationReport("author_attribution")
        body = self._strip_frontmatter(content)

        author = (frontmatter or {}).get('author') or (frontmatter or {}).get('authorName')
        has_author = bool(author and str(author).strip())

        expert_count = 0
        try:
            from .fact_checker import FactChecker
            claims = FactChecker().extract_claims(body)
            expert_count = sum(1 for c in claims if getattr(c, 'claim_type', '') == 'expert')
        except Exception:
            # Local fallback mirroring FactChecker's expert patterns.
            expert_count = len(re.findall(
                r"(?:according\s+to\s+[A-Z]|experts?\s+(?:recommend|suggest|indicate|note|observe))",
                body))

        byline_score = 5 if has_author else 0
        expert_score = min(5, expert_count)
        score = byline_score + expert_score
        passed = has_author and expert_count >= 1

        msg = (f"author={'set' if has_author else 'missing'}; "
               f"{expert_count} expert attribution(s) in body")
        report.add_check("Author & Expert Attribution", passed, msg, score=score)
        report.add_score("author_attribution", score, 10)
        report.calculate_overall_score()
        return report

    def validate_banned_words_body(self, content: str) -> ValidationReport:
        """Flag SEO banned words appearing in the article body, not just titles.

        Audit gap: banned words were enforced on titles only. Buzzwords in the
        body ("leverage", "robust", "cutting-edge") read as AI filler and erode
        authoritative tone. Each distinct banned term docks the score.
        """
        report = ValidationReport("banned_words_body")
        body = self._strip_frontmatter(content)

        banned = []
        if isinstance(self.seo_config, dict):
            banned = (self.seo_config.get('title_rules', {}) or {}).get('banned_words', []) or []

        if not banned:
            report.add_check("Body Banned Words", True,
                             "No banned-word list configured - check skipped", score=10)
            report.add_score("banned_words_body", 10, 10)
            report.calculate_overall_score()
            return report

        body_lower = body.lower()
        hits = {}
        for term in banned:
            t = str(term).lower().strip()
            if not t:
                continue
            count = len(re.findall(r"\b" + re.escape(t) + r"\b", body_lower))
            if count:
                hits[t] = count

        distinct = len(hits)
        score = max(0, 10 - distinct * 2)
        passed = distinct == 0
        if passed:
            msg = "No banned buzzwords in body"
        else:
            top = ", ".join(f"{k} (x{v})" for k, v in sorted(hits.items(), key=lambda kv: -kv[1])[:5])
            msg = f"{distinct} banned word(s) in body: {top}"

        report.add_check("Body Banned Words", passed, msg, score=score)
        report.add_score("banned_words_body", score, 10)
        report.calculate_overall_score()
        return report


    @staticmethod
    def _clean_heading_text(text: str) -> str:
        """Normalize a heading for editorial quality checks."""
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")
        text = re.sub(r"^[#\s]+", "", text).strip()
        text = re.sub(r"^\d+[\.)]\s+", "", text)
        return re.sub(r"\s+", " ", text).strip(" \t-–—")

    @staticmethod
    def _meaningful_heading_tokens(text: str) -> List[str]:
        stop = {
            "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
            "how", "in", "into", "is", "it", "of", "on", "or", "the", "this",
            "to", "what", "when", "where", "which", "why", "with", "you", "your",
            "does", "do", "can", "should", "will", "work", "works", "cost",
            "features", "implement", "implementation",
        }
        return [t for t in ContentValidator._word_tokens(text) if t not in stop]

    def heading_text_issues(
        self,
        heading: str,
        keyword: str = "",
        section_context: str = "",
        all_headings: Optional[List[str]] = None,
    ) -> List[str]:
        """Return editorial issues for one heading without forcing exact keywords.

        This is intentionally a guardrail, not a keyword-density rule. Smart
        variants are allowed; mechanical raw-keyword wrappers and broken grammar
        are flagged.
        """
        clean = self._clean_heading_text(heading)
        lower = clean.lower().strip()
        issues: List[str] = []
        if not clean:
            return ["empty heading"]

        awkward_patterns = [
            (r"\bwhat\s+are\s+.+\s+features\??$", "mechanical FAQ/template heading"),
            (r"\bhow\s+to\s+implement\s+.+\??$", "mechanical FAQ/template heading"),
            (r"\bhow\s+much\s+does\s+.+\s+cost\??$", "mechanical FAQ/template heading"),
        ]
        for pattern, message in awkward_patterns:
            if re.search(pattern, lower):
                issues.append(message)

        keyword_tokens = self._meaningful_heading_tokens(keyword)
        heading_tokens = self._meaningful_heading_tokens(clean)
        if keyword_tokens and heading_tokens:
            overlap = sum(1 for token in heading_tokens if token in keyword_tokens)
            overlap_ratio = overlap / max(len(heading_tokens), 1)
            template_starters = (
                "what is ", "what are ", "how does ", "how do ",
                "how to ", "why does ", "when does ",
            )
            interpretive_tokens = [t for t in heading_tokens if t not in keyword_tokens]
            if lower.startswith(template_starters) and overlap_ratio >= 0.7 and len(interpretive_tokens) <= 1:
                issues.append("mechanical raw-keyword wrapper")

            # Preserve obvious semantic anchors when the heading otherwise copies
            # the keyword. This catches dropped glue like "when"/"30 days" only
            # when the model is already trying to use the raw phrase, not when it
            # chooses a smarter variant such as "long sales cycles".
            copies_keyword_phrase = overlap >= max(3, int(len(keyword_tokens) * 0.6))
            keyword_lower = (keyword or "").lower()
            if copies_keyword_phrase and "30" in keyword_lower and "30" not in lower and "long" not in lower:
                issues.append("raw-keyword variant dropped the 30-day/long-cycle meaning")
            if copies_keyword_phrase and "when" in keyword_lower and "when" not in lower and "after" not in lower and "long" not in lower:
                issues.append("raw-keyword variant dropped the timing relationship")

        if all_headings:
            normalized = [self._clean_heading_text(h).lower().rstrip("?") for h in all_headings]
            if normalized.count(lower.rstrip("?")) > 1:
                issues.append("duplicate heading")

        return issues

    def validate_heading_quality(self, content: str, keyword: str = "") -> ValidationReport:
        """Score headings for editorial quality without exact-match stuffing."""
        report = ValidationReport("heading_quality")
        body = self._strip_frontmatter(content)
        headings = [h for h in self._extract_headings(body) if h['level'] in (2, 3)]
        heading_texts = [h['text'] for h in headings]

        if not headings:
            report.add_check("Heading Quality", False, "No H2/H3 headings found", score=0)
            report.add_score("heading_quality", 0, 100)
            report.calculate_overall_score()
            return report

        issues: List[str] = []
        for i, heading in enumerate(headings):
            start = heading['position'] + len('#' * heading['level']) + 1 + len(heading['text'])
            end = headings[i + 1]['position'] if i + 1 < len(headings) else len(body)
            section_context = body[start:end]
            for issue in self.heading_text_issues(
                heading['text'], keyword, section_context=section_context, all_headings=heading_texts
            ):
                issues.append(f"{heading['text']}: {issue}")

        passed = len(issues) == 0
        score = max(0, 100 - len(issues) * 15)
        detail = "Headings use natural keyword variants" if passed else f"{len(issues)} heading quality issue(s): " + "; ".join(issues[:4])
        report.add_check("Natural Keyword Variants", passed, detail, score=score)
        report.add_score("heading_quality", score, 100)
        report.calculate_overall_score()
        return report

    @staticmethod
    def _clean_title_text(text: str) -> str:
        """Normalize a title for editorial quality checks."""
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")
        text = re.sub(r"^[#\s]+", "", text).strip()
        return re.sub(r"\s+", " ", text).strip(" \t-–—")

    def _proper_noun_casing_issues(self, title: str) -> List[str]:
        """Detect title-cased acronyms/proper nouns using config/proper_nouns.json."""
        overrides = {}
        if isinstance(self.proper_nouns, dict):
            raw = self.proper_nouns.get('overrides', {}) or {}
            if isinstance(raw, dict):
                overrides = {str(k).lower(): str(v) for k, v in raw.items()}
        if not overrides:
            return []

        issues: List[str] = []
        for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9.]*\b", title or ""):
            token = match.group(0)
            canonical = overrides.get(token.lower().replace('.', '')) or overrides.get(token.lower())
            if canonical and token != canonical:
                issues.append(f"improper casing for {canonical}")
        return issues[:3]

    def title_text_issues(
        self,
        title: str,
        keyword: str = "",
        *,
        style: str = "",
        description: str = "",
        body_preview: str = "",
    ) -> List[str]:
        """Return editorial title issues without forcing exact keyword matches."""
        clean = self._clean_title_text(title)
        lower = clean.lower().strip()
        issues: List[str] = []
        if not clean:
            return ["empty title"]

        if len(clean) < 25:
            issues.append("title too short to carry search intent")
        if len(clean) > 70:
            issues.append("title too long for SERP display")

        banned = []
        if isinstance(self.seo_config, dict):
            banned = (self.seo_config.get('title_rules', {}) or {}).get('banned_words', []) or []
        for term in banned:
            t = str(term).lower().strip()
            if t and re.search(r"\b" + re.escape(t) + r"\b", lower):
                issues.append(f"contains banned word: {t}")

        if re.search(r"\b(everything you need to know|complete guide|expert insights and proven strategies)\b", lower):
            issues.append("generic SEO filler formula")

        if re.search(r"[;:|\-–—]\s*(a|an|and|are|for|is|of|or|the|to|with)$", lower):
            issues.append("dangling truncated title fragment")
        elif re.search(r"\b(a|an|and|are|for|is|of|or|the|to|with)$", lower):
            issues.append("dangling title ending")

        if clean.count(';') > 0 and len(self._meaningful_heading_tokens(clean.split(';')[-1])) < 3:
            issues.append("semicolon fragment lacks a complete idea")

        keyword_tokens = self._meaningful_heading_tokens(keyword)
        title_tokens = self._meaningful_heading_tokens(clean)
        if keyword_tokens and title_tokens:
            overlap = sum(1 for token in title_tokens if token in keyword_tokens)
            overlap_ratio = overlap / max(len(title_tokens), 1)
            template_starters = ("what is ", "what are ", "understanding ", "guide to ", "how to ")
            interpretive_tokens = [t for t in title_tokens if t not in keyword_tokens]
            if lower.startswith(template_starters) and overlap_ratio >= 0.7 and len(interpretive_tokens) <= 1:
                issues.append("mechanical raw-keyword title wrapper")
            if overlap >= max(4, int(len(keyword_tokens) * 0.75)) and len(interpretive_tokens) <= 1 and lower != (keyword or "").lower().strip():
                issues.append("excessive raw-keyword copying without editorial angle")

        # A title should share at least one meaningful concept with the article
        # when context is available, while still allowing abstract thought-leadership framing.
        context = f"{description} {body_preview}".strip()
        if context and title_tokens:
            context_tokens = set(self._meaningful_heading_tokens(context[:700]))
            if context_tokens and sum(1 for token in title_tokens if token in context_tokens) == 0:
                issues.append("title does not match article context")

        issues.extend(self._proper_noun_casing_issues(clean))
        return list(dict.fromkeys(issues))

    def validate_title_quality(
        self,
        content: str,
        frontmatter: Dict[str, Any],
        keyword: str = "",
        style: str = "",
    ) -> ValidationReport:
        """Score title/frontmatter quality for smart SEO/AEO title behavior."""
        report = ValidationReport("title_quality")
        title = str(frontmatter.get('title') or '')
        seo = frontmatter.get('seo') if isinstance(frontmatter.get('seo'), dict) else {}
        seo_title = str((seo or {}).get('title') or '')
        description = str(frontmatter.get('description') or (seo or {}).get('description') or '')
        body_preview = self._strip_frontmatter(content)[:700]

        issues = self.title_text_issues(
            title,
            keyword,
            style=style,
            description=description,
            body_preview=body_preview,
        )
        if seo_title:
            seo_issues = self.title_text_issues(
                seo_title,
                keyword,
                style=style,
                description=description,
                body_preview=body_preview,
            )
            issues.extend(f"seo.title: {issue}" for issue in seo_issues)

        passed = len(issues) == 0
        score = max(0, 100 - len(issues) * 15)
        detail = "Title uses a natural SEO/AEO variant" if passed else f"{len(issues)} title quality issue(s): " + "; ".join(issues[:5])
        report.add_check("Smart Title Quality", passed, detail, score=score)
        report.add_score("title_quality", score, 100)
        report.calculate_overall_score()
        return report

    def _plain_link_anchor(self, anchor: str) -> str:
        """Normalize link anchor text without markdown/HTML decoration."""
        anchor = re.sub(r"<[^>]+>", " ", anchor or "")
        anchor = re.sub(r"[`*_{}\[\]]", "", anchor)
        return re.sub(r"\s+", " ", anchor).strip()

    def _is_internal_url(self, url: str) -> bool:
        raw = (url or "").strip()
        if raw.startswith("/") and not raw.startswith("//"):
            return True
        from .brand_profile import load_brand_profile
        domain = load_brand_profile().domain_host()
        if not domain:
            return False
        parsed = urlparse(raw)
        host = (parsed.netloc or "").lower()
        return host == domain or host.endswith(f".{domain}")

    @staticmethod
    def _normalize_internal_path(url: str) -> str:
        raw = (url or "").strip()
        from .brand_profile import load_brand_profile
        domain = load_brand_profile().domain_host()
        if domain:
            m = re.match(rf"^https?://(?:www\.)?{re.escape(domain)}(/.*)?$", raw, re.IGNORECASE)
            if m:
                return ((m.group(1) or "/").rstrip("/") or "/").lower()
        if raw.startswith("/") and not raw.startswith("//"):
            return (raw.rstrip("/") or "/").lower()
        return raw.rstrip("/").lower()

    def _url_topic_tokens(self, url: str) -> List[str]:
        parsed = urlparse(url or "")
        path_text = re.sub(r"[-_/]+", " ", parsed.path or "")
        return self._meaningful_heading_tokens(path_text)

    def _destination_metadata_tokens(self, url: str, metadata: Optional[Dict[str, Any]] = None) -> List[str]:
        metadata = metadata or {}
        pieces = [url or ""]
        for key in ("title", "anchor_text", "description", "category", "source_type", "type"):
            value = metadata.get(key)
            if value:
                pieces.append(str(value))
        tags = metadata.get("tags") or metadata.get("categories")
        if isinstance(tags, list):
            pieces.extend(str(t) for t in tags)
        return self._meaningful_heading_tokens(" ".join(pieces))

    def _is_generic_link_anchor(self, anchor: str) -> bool:
        clean = self._plain_link_anchor(anchor).lower()
        tokens = self._meaningful_heading_tokens(clean)
        generic = {
            "standard", "comparison", "investment", "managing", "tracking",
            "platform", "reporting", "optimize", "industry", "tools", "blog",
            "features", "documentation", "learn", "guide", "better", "example",
            "examples", "solution", "solutions", "strategy", "strategies",
            "performance", "data", "system", "systems", "process", "approach",
            "results", "insights", "business", "marketing", "sales",
        }
        if not clean or len(clean) < 4:
            return True
        if len(tokens) == 1 and tokens[0] in generic:
            return True
        if len(tokens) == 1 and len(tokens[0]) < 5:
            return True
        if clean in {"click here", "read more", "learn more", "this guide", "this post"}:
            return True
        return False

    def _line_for_position(self, text: str, position: int) -> str:
        start = text.rfind("\n", 0, max(position, 0)) + 1
        end = text.find("\n", max(position, 0))
        if end == -1:
            end = len(text)
        return text[start:end]

    def _sentence_window(self, text: str, start: int, end: int, radius: int = 180) -> str:
        left = max(0, start - radius)
        right = min(len(text), end + radius)
        window = text[left:right]
        return re.sub(r"\s+", " ", window).strip()

    def _extract_internal_link_records(self, content: str) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Parse internal markdown/HTML links and collect malformed-link issues."""
        body = self._strip_frontmatter(content or "")
        records: List[Dict[str, Any]] = []
        issues: List[str] = []

        md_pattern = re.compile(r"(?<!!)\[([^\]]+)\]\(((?:https?://[^\s\)]+(?:\)[^\s\)]*)?|/[a-zA-Z0-9_/-]*))\)")
        for match in md_pattern.finditer(body):
            anchor = match.group(1)
            url = match.group(2)
            if "[" in url or "](" in url or url.count("http") > 1:
                issues.append("malformed or nested markdown link")
            if "[" in anchor or "](" in anchor:
                issues.append("nested markdown link anchor")
            if self._is_internal_url(url):
                records.append({
                    "anchor": anchor,
                    "url": url,
                    "start": match.start(),
                    "end": match.end(),
                    "format": "markdown",
                    "line": self._line_for_position(body, match.start()),
                    "context": self._sentence_window(body, match.start(), match.end()),
                })

        html_pattern = re.compile(r'<a\s+[^>]*href=["\']((?:https?://[^"\']+|/[a-zA-Z0-9_/-]*))["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
        for match in html_pattern.finditer(body):
            url = match.group(1)
            anchor = re.sub(r"<[^>]+>", " ", match.group(2))
            if self._is_internal_url(url):
                records.append({
                    "anchor": anchor,
                    "url": url,
                    "start": match.start(),
                    "end": match.end(),
                    "format": "html",
                    "line": self._line_for_position(body, match.start()),
                    "context": self._sentence_window(body, match.start(), match.end()),
                })

        return records, list(dict.fromkeys(issues))

    def internal_link_text_issues(
        self,
        content: str,
        keyword: str = "",
        *,
        current_url: str = "",
        destination_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[str]:
        """Return deterministic issues for internal-link validity, sparsity, and relevance."""
        records, issues = self._extract_internal_link_records(content)
        destination_metadata = destination_metadata or {}

        if not records:
            return issues

        word_count = max(1, len(self._strip_frontmatter(content or "").split()))
        max_links = max(4, min(18, word_count // 250 + 3))
        if len(records) > max_links:
            issues.append(f"too many internal links for article length ({len(records)} > {max_links})")

        by_url: Dict[str, int] = {}
        canonical_current = self._normalize_internal_path(current_url or "")
        keyword_tokens = set(self._meaningful_heading_tokens(keyword or ""))

        for record in records:
            anchor = self._plain_link_anchor(record.get("anchor", ""))
            anchor_tokens = set(self._meaningful_heading_tokens(anchor))
            url = str(record.get("url") or "")
            clean_url = self._normalize_internal_path(url)
            by_url[clean_url] = by_url.get(clean_url, 0) + 1

            if canonical_current and clean_url == canonical_current:
                issues.append("self-link points to the current article")
            if record.get("line", "").lstrip().startswith("#"):
                issues.append("internal link appears in a heading")
            if self._is_generic_link_anchor(anchor):
                issues.append(f"generic internal-link anchor: {anchor[:40]}")

            meta = destination_metadata.get(url) or destination_metadata.get(clean_url) or {}
            destination_tokens = set(self._destination_metadata_tokens(url, meta))
            context_tokens = set(self._meaningful_heading_tokens(record.get("context", "")))
            overlap = (anchor_tokens | context_tokens | keyword_tokens) & destination_tokens
            high_priority = any(token in destination_tokens for token in {"pricing", "integration", "integrations", "product", "features", "platform", "docs"})
            if destination_tokens and not overlap and not high_priority:
                issues.append(f"internal link context does not match destination: {anchor[:40]}")

        repeated = [url for url, count in by_url.items() if count > 2]
        if repeated:
            issues.append("same internal destination linked more than twice")

        return list(dict.fromkeys(issues))

    def validate_internal_link_quality(
        self,
        content: str,
        frontmatter: Optional[Dict[str, Any]] = None,
        keyword: str = "",
        destination_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> ValidationReport:
        """Score internal links for editorial relevance, sparsity, and safety."""
        report = ValidationReport("internal_link_quality")
        frontmatter = frontmatter or {}
        current_url = str(frontmatter.get("canonical") or frontmatter.get("url") or "")
        issues = self.internal_link_text_issues(
            content,
            keyword,
            current_url=current_url,
            destination_metadata=destination_metadata,
        )
        records, _ = self._extract_internal_link_records(content)
        passed = len(issues) == 0
        score = max(0, 100 - len(issues) * 12)
        detail = (
            f"{len(records)} useful internal link(s) with safe anchors"
            if passed else
            f"{len(issues)} internal-link issue(s): " + "; ".join(issues[:6])
        )
        report.add_check("Internal Link Quality", passed, detail, score=score)
        report.add_score("internal_link_quality", score, 100)
        report.calculate_overall_score()
        return report

    def validate_complete_research_content(
        self,
        content: str,
        frontmatter: Dict[str, Any],
        has_schema: bool = False,
        verified_claims: Optional[List[Dict[str, Any]]] = None,
        keyword: Optional[str] = None,
        style: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run complete validation suite for research content.

        Args:
            content: Full MDX content
            frontmatter: Frontmatter dict
            has_schema: Whether JSON-LD schema is present
            verified_claims: List of verified claims from FactChecker

        Returns:
            Complete validation report dict with all scores

        This is the main entry point for end-to-end validation.
        """
        logger.info("🔍 Running complete research content validation...")

        # Calculate citation coverage
        citation_coverage = 0.0
        if verified_claims:
            verified_count = sum(1 for c in verified_claims if c.get('verified', False))
            citation_coverage = verified_count / max(len(verified_claims), 1)

        # Resolve keyword/style from frontmatter when not passed explicitly.
        keyword = keyword or frontmatter.get('primaryKeyword') or frontmatter.get('keyword') or ''
        style = style or frontmatter.get('style') or frontmatter.get('category') or ''

        # Run all validations
        structure_report = self.validate_structure(content, frontmatter)
        faq_report = self.validate_faq_answers(frontmatter.get('faq', []))
        aeo_report = self.score_aeo_readiness(content, frontmatter, has_schema, citation_coverage)
        aeo_gates_report = self.validate_aeo_gates(content, frontmatter, has_schema)
        # New AI-SEO / GEO citability dimensions (deterministic).
        keyword_stuffing_report = self.validate_keyword_stuffing(content, keyword)
        process_report = self.validate_process_structure(content, style)
        attribution_report = self.validate_author_attribution(content, frontmatter)
        banned_body_report = self.validate_banned_words_body(content)
        heading_quality_report = self.validate_heading_quality(content, keyword)
        title_quality_report = self.validate_title_quality(content, frontmatter, keyword, style)
        internal_link_report = self.validate_internal_link_quality(content, frontmatter, keyword)

        # Compile complete report
        reports = [structure_report, faq_report, aeo_report, aeo_gates_report,
                   keyword_stuffing_report, process_report, attribution_report,
                   banned_body_report, heading_quality_report, title_quality_report,
                   internal_link_report]
        blocking_reports = [r for r in reports if r is not faq_report]
        blocking_errors = sum(
            len(
                [err for err in r.errors if not err.startswith("Helpful FAQ Coverage:")]
                if r is aeo_report
                else r.errors
            )
            for r in blocking_reports
        )
        overall_score = int(sum(r.overall_score for r in reports) / len(reports))
        passed = (
            blocking_errors == 0
            and structure_report.overall_score >= 50
            and overall_score >= 50
        )
        complete_report = {
            'overall_score': overall_score,
            'passed': passed,
            'structure': structure_report.to_dict(),
            'faq': faq_report.to_dict(),
            'aeo_readiness': aeo_report.to_dict(),
            'aeo_gates': aeo_gates_report.to_dict(),
            'keyword_stuffing': keyword_stuffing_report.to_dict(),
            'process_structure': process_report.to_dict(),
            'author_attribution': attribution_report.to_dict(),
            'banned_words_body': banned_body_report.to_dict(),
            'heading_quality': heading_quality_report.to_dict(),
            'title_quality': title_quality_report.to_dict(),
            'internal_link_quality': internal_link_report.to_dict(),
            'summary': {
                'total_checks': sum(len(r.checks) for r in reports),
                'passed_checks': sum(len([c for c in r.checks if c['passed']]) for r in reports),
                'total_errors': blocking_errors,
                'total_warnings': sum(
                    len(r.errors) + len(r.warnings) for r in reports
                ) - blocking_errors
            }
        }

        # Log summary
        status = "✅ VALIDATION PASSED" if complete_report['passed'] else "⚠️  VALIDATION ISSUES FOUND"
        logger.info(f"{status} | Overall Score: {complete_report['overall_score']}/100")
        logger.info(f"  Structure: {structure_report.overall_score}/100 | FAQ: {faq_report.overall_score}/100 | AEO: {aeo_report.overall_score}/100 | Gates: {aeo_gates_report.overall_score}/100")
        logger.info(f"  Keyword: {keyword_stuffing_report.overall_score}/100 | Process: {process_report.overall_score}/100 | Attribution: {attribution_report.overall_score}/100 | BannedBody: {banned_body_report.overall_score}/100 | Headings: {heading_quality_report.overall_score}/100 | Title: {title_quality_report.overall_score}/100 | InternalLinks: {internal_link_report.overall_score}/100")

        if complete_report['summary']['total_errors'] > 0:
            logger.warning(f"  {complete_report['summary']['total_errors']} errors, {complete_report['summary']['total_warnings']} warnings")

        return complete_report

    # Default rubric weights (used when config/quality_rubric.json is absent).
    # AEO/authority weighted highest per Princeton GEO (citations +40%,
    # statistics +37%). Judge dimensions are only blended when supplied.
    _DEFAULT_RUBRIC_WEIGHTS = {
        "structure": 1.0,
        "faq": 0.5,
        "aeo_readiness": 1.5,
        "aeo_gates": 1.0,
        "keyword_stuffing": 1.0,
        "process_structure": 0.5,
        "author_attribution": 1.0,
        "banned_words_body": 0.5,
        "heading_quality": 1.0,
        "title_quality": 0.75,
        "internal_link_quality": 0.75,
        "clarity": 1.0,
        "fluency": 1.0,
        "authoritative_tone": 1.0,
    }

    def build_scorecard(
        self,
        content: str,
        frontmatter: Dict[str, Any],
        *,
        keyword: Optional[str] = None,
        style: Optional[str] = None,
        has_schema: bool = False,
        verified_claims: Optional[List[Dict[str, Any]]] = None,
        judge: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a single weighted 0-100 quality composite + per-dimension table.

        Blends the deterministic AI-SEO dimensions (always) with optional
        LLM-judge subjective dimensions (clarity/fluency/authoritative tone)
        from ``core.quality_judge``. Weights come from config/quality_rubric.json
        with a built-in fallback. Returns the report shape consumed by the eval
        harness: ``{keyword, style, composite, dimensions:{name:{score,max,...}}}``.

        ``judge`` schema (when provided): ``{"dimensions": {"clarity": {"score":
        0-100, "rationale": str}, ...}}`` — a dimension with ``score is None`` is
        recorded but excluded from the composite (no faked passes).
        """
        keyword = keyword or frontmatter.get('primaryKeyword') or frontmatter.get('keyword') or ''
        style = style or frontmatter.get('style') or frontmatter.get('category') or ''

        det = self.validate_complete_research_content(
            content, frontmatter, has_schema=has_schema,
            verified_claims=verified_claims, keyword=keyword, style=style,
        )

        # Each deterministic sub-report exposes overall_score on a 0-100 scale.
        det_keys = ["structure", "faq", "aeo_readiness", "aeo_gates",
                    "keyword_stuffing", "process_structure",
                    "author_attribution", "banned_words_body",
                    "heading_quality", "title_quality",
                    "internal_link_quality"]
        dimensions: Dict[str, Any] = {}
        for k in det_keys:
            sub = det.get(k, {})
            dimensions[k] = {
                "score": sub.get("overall_score", 0),
                "max": 100,
                "kind": "deterministic",
            }

        # Merge judge dimensions (subjective) when supplied.
        if judge and isinstance(judge.get("dimensions"), dict):
            for name, payload in judge["dimensions"].items():
                if not isinstance(payload, dict):
                    continue
                dimensions[name] = {
                    "score": payload.get("score"),
                    "max": 100,
                    "kind": "judge",
                    "rationale": payload.get("rationale"),
                    "error": payload.get("error"),
                }

        weights = {}
        if isinstance(self.quality_rubric, dict):
            weights = self.quality_rubric.get("weights", {}) or {}
        if not weights:
            weights = self._DEFAULT_RUBRIC_WEIGHTS

        # Weighted composite over dimensions that have a usable numeric score.
        num = 0.0
        den = 0.0
        for name, dim in dimensions.items():
            score = dim.get("score")
            if score is None:
                continue
            w = float(weights.get(name, 1.0))
            num += w * float(score)
            den += w
        composite = int(round(num / den)) if den else 0

        return {
            "keyword": keyword,
            "style": style,
            "composite": composite,
            "passed": det.get("passed", False),
            "dimensions": dimensions,
            "deterministic_overall": det.get("overall_score", 0),
            "summary": det.get("summary", {}),
        }

    # ------------------------------------------------------------------ #
    # Generic Blog Config-to-Output Contract Guardrails
    # ------------------------------------------------------------------ #

    def _collect_live_astro_routes(self, astro_src: Optional[Path]) -> Tuple[set, set]:
        """Return (valid_paths_set, live_blog_slugs_set) from a connected site src tree."""
        valid_paths = {"/", "/pricing", "/blog", "/faq", "/about", "/features", "/integrations", "/docs"}
        live_blog_slugs: set = set()
        if not astro_src or not astro_src.exists():
            return set(), set()

        import yaml

        # Static/dynamic pages under pages/
        pages_dir = astro_src / "pages"
        if pages_dir.exists():
            for p in pages_dir.rglob("*.astro"):
                rel = "/" + p.relative_to(pages_dir).with_suffix("").as_posix()
                if rel.endswith("/index"):
                    rel = rel[:-6] or "/"
                if "[" not in rel:
                    valid_paths.add(rel.rstrip("/") or "/")

        content_dir = astro_src / "content"
        for collection, route_prefix in (
            ("blog", "/blog"),
            ("integrations", "/integrations"),
            ("tools", "/tools"),
            ("announcements", "/announcements"),
            ("changelogs", "/changelogs"),
        ):
            cdir = content_dir / collection
            if not cdir.exists():
                continue
            for mdx_file in cdir.glob("*.mdx"):
                if mdx_file.name.startswith("_"):
                    continue
                try:
                    raw = mdx_file.read_text(encoding="utf-8")
                    parts = raw.split("---", 2)
                    fm = yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
                except Exception:
                    fm = {}
                if not isinstance(fm, dict):
                    fm = {}
                if collection == "blog" and fm.get("draft", False):
                    continue
                slug = str(fm.get("slug") or mdx_file.stem)
                valid_paths.add(f"{route_prefix}/{slug}")
                valid_paths.add(f"{route_prefix}/{mdx_file.stem}")
                if collection == "blog":
                    live_blog_slugs.add(slug)
                    live_blog_slugs.add(mdx_file.stem)
                if collection == "integrations" and fm.get("pageUrl"):
                    valid_paths.add(str(fm["pageUrl"]).rstrip("/") or "/")

        return valid_paths, live_blog_slugs

    @classmethod
    def _validate_json_schema_node(cls, value: Any, schema_node: Dict[str, Any], path: str = "frontmatter") -> List[str]:
        """Validate a parsed YAML frontmatter value against a JSON Schema node."""
        if not isinstance(schema_node, dict):
            return []
        errs: List[str] = []

        if "oneOf" in schema_node and isinstance(schema_node["oneOf"], list):
            if not any(len(cls._validate_json_schema_node(value, sub, path)) == 0 for sub in schema_node["oneOf"]):
                errs.append(f"{path} does not match any allowed schema variant")
            return errs

        expected_type = schema_node.get("type")
        if expected_type == "object":
            if not isinstance(value, dict):
                return [f"{path} must be an object, got {type(value).__name__}"]
            props = schema_node.get("properties") or {}
            for req in schema_node.get("required") or []:
                if req not in value or value.get(req) is None:
                    errs.append(f"Missing required field: {path}.{req}" if path != "frontmatter" else f"Missing required frontmatter field: {req}")
            if schema_node.get("additionalProperties") is False and props:
                unknown = [k for k in value.keys() if k not in props]
                if unknown:
                    label = "Unknown frontmatter key(s) (Zod would strip)" if path == "frontmatter" else f"Unknown key(s) in {path}"
                    errs.append(f"{label}: {', '.join(str(k) for k in unknown)}")
            for k, v in value.items():
                if k in props and v is not None:
                    sub_path = f"{path}.{k}" if path != "frontmatter" else str(k)
                    errs.extend(cls._validate_json_schema_node(v, props[k], sub_path))
        elif expected_type == "array":
            if not isinstance(value, list):
                return [f"{path} must be an array, got {type(value).__name__}"]
            min_items = schema_node.get("minItems")
            max_items = schema_node.get("maxItems")
            if min_items is not None and len(value) < min_items:
                errs.append(f"{path} has {len(value)} item(s) (minimum {min_items})")
            if max_items is not None and len(value) > max_items:
                errs.append(f"{path} has {len(value)} item(s) (maximum {max_items})")
            item_schema = schema_node.get("items")
            if isinstance(item_schema, dict):
                for idx, item in enumerate(value):
                    errs.extend(cls._validate_json_schema_node(item, item_schema, f"{path}[{idx}]"))
        elif expected_type == "string":
            if not isinstance(value, str):
                return [f"{path} must be a string, got {type(value).__name__}"]
            min_len = schema_node.get("minLength")
            max_len = schema_node.get("maxLength")
            if min_len is not None and len(value) < min_len:
                errs.append(f"{path} length is {len(value)} chars (minimum {min_len})")
            if max_len is not None and len(value) > max_len:
                errs.append(f"{path} length is {len(value)} chars (maximum {max_len})")
            pat = schema_node.get("pattern")
            if pat and not re.search(pat, value):
                errs.append(f"{path} does not match pattern {pat}: {value!r}")
        elif expected_type == "boolean":
            if not isinstance(value, bool):
                return [f"{path} must be a boolean, got {type(value).__name__}"]
        elif expected_type == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return [f"{path} must be a number, got {type(value).__name__}"]

        if "enum" in schema_node and value not in schema_node["enum"]:
            errs.append(f"Invalid {path}={value!r}; must be one of {schema_node['enum']}")

        return errs

    def validate_blog_contract(
        self,
        mdx_content: str,
        *,
        slug: str = "",
        astro_src_path: Optional[Path] = None,
        require_hero_asset: bool = False,
        require_citations: bool = True,
        flyweel_mode: str = "full",
    ) -> Dict[str, Any]:
        """Validate an MDX blog post against canonical SEO/AEO structure & frontmatter rules.

        Returns ``{"valid": bool, "errors": List[str], "warnings": List[str]}``.
        """
        from datetime import date, datetime
        import yaml
        from .faq_builder import find_faq_section

        errors: List[str] = []
        warnings: List[str] = []

        if not mdx_content or not mdx_content.startswith("---\n"):
            return {"valid": False, "errors": ["Missing leading YAML frontmatter fence (---)"], "warnings": []}

        parts = mdx_content.split("---", 2)
        if len(parts) < 3:
            return {"valid": False, "errors": ["Unterminated YAML frontmatter block"], "warnings": []}

        try:
            fm = yaml.safe_load(parts[1])
        except yaml.YAMLError as exc:
            return {"valid": False, "errors": [f"YAML parse error in frontmatter: {exc}"], "warnings": []}

        if not isinstance(fm, dict):
            return {"valid": False, "errors": ["Frontmatter is not a YAML mapping"], "warnings": []}

        for date_key in ("publishDate", "updatedDate", "date"):
            if isinstance(fm.get(date_key), (datetime, date)):
                fm[date_key] = fm[date_key].isoformat()

        body = parts[2]
        schema_path = Path(__file__).parent.parent / "config" / "blog-content-schema.json"
        schema = self._load_json_lenient(schema_path)

        # 1. Schema & unknown keys (Guardrail #1)
        if isinstance(schema, dict):
            for serr in self._validate_json_schema_node(fm, schema, "frontmatter"):
                if serr not in errors:
                    errors.append(serr)

        for req in ("title", "description", "author", "product", "funnel_stage", "topic", "excerpt", "canonical", "relatedPosts", "tldr", "seo", "image", "faq"):
            if req not in fm or fm.get(req) in (None, ""):
                msg = f"Missing required frontmatter field: {req}"
                if msg not in errors:
                    errors.append(msg)

        if not slug:
            canon = str(fm.get("canonical") or "")
            slug = canon.rsplit("/", 1)[-1] if "/blog/" in canon else ""

        from .brand_profile import load_brand_profile
        profile = load_brand_profile()
        expected_canon = profile.canonical_blog_url(slug) if slug else ""
        if slug and profile.site_url and fm.get("canonical") != expected_canon:
            errors.append(f"canonical must be {expected_canon!r}, got {fm.get('canonical')!r}")

        # 2. Hero asset & alt text
        astro_src = astro_src_path
        image = fm.get("image")
        if not isinstance(image, dict):
            errors.append("frontmatter image must be an object with src and alt")
        else:
            img_src = str(image.get("src") or "")
            img_alt = str(image.get("alt") or "").strip()
            expected_src = f"/src/assets/blog/{slug}-hero.webp" if slug else ""
            if not re.match(r"^/src/assets/blog/[a-z0-9-]+-hero\.webp$", img_src):
                errors.append(f"image.src must match /src/assets/blog/<slug>-hero.webp, got {img_src!r}")
            elif expected_src and img_src != expected_src:
                warnings.append(f"image.src ({img_src}) differs from slug hero path ({expected_src})")
            if len(img_alt) < 10:
                errors.append("image.alt is missing or too short")
            elif img_alt.lower().startswith("hero image"):
                errors.append(f"image.alt must describe what the image shows, never 'Hero image for ...': {img_alt!r}")
            if require_hero_asset and astro_src and img_src.startswith("/src/assets/"):
                hero_file = astro_src / "assets" / img_src[len("/src/assets/"):]
                if not hero_file.exists():
                    errors.append(f"Hero asset file does not exist on disk: {hero_file}")

        # 3. Character counts & SEO block (Guardrail #3)
        title = str(fm.get("title") or "").strip()
        desc = str(fm.get("description") or "").strip()
        if not (150 <= len(desc) <= 160):
            errors.append(f"description length is {len(desc)} chars (must be 150-160)")
        if "—" in desc or "–" in desc or re.search(r"\*\*|\[[^\]]+\]\(", desc):
            errors.append("description contains markdown or em/en dashes")

        tldr = fm.get("tldr")
        if not isinstance(tldr, dict):
            errors.append("frontmatter tldr must be an object with answer and takeaways")
        else:
            ans = str(tldr.get("answer") or "")
            tks = tldr.get("takeaways")
            if not (120 <= len(ans) <= 500):
                errors.append(f"tldr.answer length is {len(ans)} chars (must be 120-500)")
            if not isinstance(tks, list) or not (3 <= len(tks) <= 6):
                errors.append(f"tldr.takeaways count is {len(tks) if isinstance(tks, list) else 0} (must be 3-6)")

        seo = fm.get("seo")
        if not isinstance(seo, dict):
            errors.append("frontmatter seo must be an object")
        else:
            for sreq in ("title", "description", "ogTitle", "ogDescription", "ogImage", "twitterCard", "focusKeyword", "schemaType"):
                if not seo.get(sreq):
                    errors.append(f"Missing required seo.{sreq}")
            seo_title = str(seo.get("title") or "").strip()
            suffix = profile.effective_title_suffix()
            rendered_seo_title = f"{seo_title}{suffix}" if suffix and suffix.strip() not in seo_title else seo_title
            if not (35 <= len(rendered_seo_title) <= 68):
                errors.append(f"seo.title rendered length is {len(rendered_seo_title)} chars (target 35-68): {rendered_seo_title!r}")
            if seo_title and title and seo_title.lower() == title.lower():
                errors.append("seo.title must not be identical to H1 title")
            focus_kw = str(seo.get("focusKeyword") or "").strip().lower()
            if seo_title and focus_kw:
                kw_sig = [w for w in re.findall(r"\w+", focus_kw) if len(w) > 3]
                first_half = seo_title[: max(30, len(seo_title) // 2 + 10)].lower()
                if kw_sig and not any(w in first_half for w in kw_sig):
                    errors.append(f"seo.title should lead with primary keyword ({focus_kw!r}): {seo_title!r}")
            seo_desc = str(seo.get("description") or "").strip()
            if not (150 <= len(seo_desc) <= 160):
                errors.append(f"seo.description length is {len(seo_desc)} chars (must be 150-160)")
            if seo.get("schemaType") not in ("Article", "BlogPosting"):
                errors.append(f"seo.schemaType must be 'Article' or 'BlogPosting' (never 'HowTo'), got {seo.get('schemaType')!r}")
            if seo.get("twitterCard") != "summary_large_image":
                errors.append(f"seo.twitterCard must be 'summary_large_image', got {seo.get('twitterCard')!r}")
            if seo.get("noindex") is not False or seo.get("nofollow") is not False:
                errors.append("seo.noindex and seo.nofollow must both be false")

        # Plain-text fields & headings must have no footnote markers
        plain_blobs = [title, desc, str(fm.get("excerpt") or "")]
        if isinstance(tldr, dict):
            plain_blobs.append(str(tldr.get("answer") or ""))
            if isinstance(tldr.get("takeaways"), list):
                plain_blobs.extend(str(x) for x in tldr["takeaways"])
        for item in (fm.get("faq") or []) if isinstance(fm.get("faq"), list) else []:
            if isinstance(item, dict):
                plain_blobs.extend([str(item.get("question") or ""), str(item.get("answer") or "")])
        plain_blobs.extend(h["text"] for h in self._extract_headings(body))
        for blob in plain_blobs:
            if re.search(r"(#home-fn-|<sup\b|[¹²³])", blob, re.IGNORECASE):
                errors.append(f"Footnote marker found in plain-text field or heading: {blob[:60]!r}")
                break

        # 4. Lint rules
        no_code = re.sub(r"```[\s\S]*?```", "", mdx_content)
        if "—" in no_code:
            errors.append("Em dash (—) found in article")
        domain = profile.domain_host()
        if domain and re.search(rf'(?<!!)\[[^\]]+\]\(https?://(?:www\.)?{re.escape(domain)}[^\)]*\)|<a\s+[^>]*href=["\']https?://(?:www\.)?{re.escape(domain)}', body, re.IGNORECASE):
            errors.append(f"Absolute {domain} link found in body (prefer relative paths)")
        if re.search(
            r"\b(?:stated product facts|company facts|provided research|verification notes)\s+(?:do not|does not|did not|cannot)\s+(?:confirm|state|verify|mention)",
            body,
            re.IGNORECASE,
        ) or re.search(r"INCLUDE THIS\s*-\s*CRITICAL|GENERATION SCOPE FOR THIS PART|^\s*SECTION\s+\d+\s+of\s+\d+", body, re.I | re.M):
            errors.append("Leaked research-note or prompt-scaffolding phrase found in body")

        # 5. Internal links and relatedPosts resolution (Guardrail #5)
        valid_paths, live_blog_slugs = self._collect_live_astro_routes(astro_src)
        rel_posts = fm.get("relatedPosts")
        if rel_posts is not None and not isinstance(rel_posts, list):
            errors.append("relatedPosts must be a list of slugs")
        elif isinstance(rel_posts, list):
            slug_re = re.compile(r"^[A-Za-z0-9_-]+$")
            for rslug in rel_posts:
                if not isinstance(rslug, str) or not slug_re.match(rslug):
                    errors.append(f"relatedPosts entry is not a valid slug: {rslug!r}")
                elif rslug == slug:
                    errors.append(f"relatedPosts contains self slug: {rslug}")
                elif live_blog_slugs and rslug not in live_blog_slugs:
                    errors.append(f"relatedPosts slug does not resolve to a live blog post: {rslug!r}")

        if valid_paths:
            for m_link in re.finditer(r'(?<!!)\[[^\]]+\]\((/[a-zA-Z0-9_/-]*)\)|href=["\'](/[a-zA-Z0-9_/-]*)["\']', body):
                lpath = (m_link.group(1) or m_link.group(2) or "/").rstrip("/") or "/"
                if lpath == f"/blog/{slug}":
                    errors.append(f"Body contains self-link to {lpath}")
                elif lpath not in valid_paths:
                    errors.append(f"Internal link does not resolve to a live page: {lpath!r}")

        # 6. Citations target parity
        if require_citations:
            inline_refs = set(re.findall(r'href=["\']#cite-(\d+)["\']', body))
            span_targets = set(re.findall(r'id=["\']cite-(\d+)["\']', body))
            missing_targets = sorted(inline_refs - span_targets, key=int)
            if missing_targets:
                errors.append(f"Inline #cite-N ref(s) missing matching id='cite-N' target: {missing_targets}")

        # Formatting: one blank line after frontmatter closing --- and trailing newline
        if not re.match(r"^---\n.*?\n---\n\n\S", mdx_content, re.DOTALL):
            warnings.append("Expected exactly one blank line after closing --- of frontmatter")
        if not mdx_content.endswith("\n"):
            warnings.append("Missing trailing newline at end of MDX file")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }


def validate_blog_contract(mdx_content: str, **kwargs: Any) -> Dict[str, Any]:
    """Convenience wrapper for ContentValidator().validate_blog_contract(...)."""
    return ContentValidator().validate_blog_contract(mdx_content, **kwargs)
