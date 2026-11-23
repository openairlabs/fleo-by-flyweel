"""SERP Insight Formatting - Structure without over-engineering

Formats Perplexity SERP analysis into categorized, verifiable insights with
anti-hallucination guards and citation management.
"""
import re
from typing import Dict, Any, List, Optional


class SERPInsightFormatter:
    """Formats Perplexity SERP analysis for safe, structured LLM injection"""

    # Token limits by content style (conservative estimates)
    TOKEN_LIMITS = {
        'research': 2000,    # Research needs maximum detail
        'standard': 1200,
        'comparison': 1200,
        'guide': 1000,       # Guides focus on steps, less SERP detail
        'news': 1000,
        'category': 1200
    }

    def format_for_prompt(
        self,
        serp_data: Dict[str, Any],
        style: str
    ) -> str:
        """
        Format SERP analysis with anti-hallucination structure

        Args:
            serp_data: Full SERP data dict with 'serp_analysis' key
            style: Content style for token budget allocation

        Returns:
            Formatted prompt section ready for injection, or empty string if no analysis
        """
        # Graceful handling of missing data
        if not serp_data or not isinstance(serp_data, dict):
            return ""

        if not serp_data.get('serp_analysis'):
            return ""

        analysis = serp_data['serp_analysis'].get('analysis', '')
        citations = serp_data['serp_analysis'].get('citations', [])

        if not analysis or not isinstance(analysis, str):
            return ""

        # Get token limit for this style
        token_limit = self.TOKEN_LIMITS.get(style, 1200)

        # Categorize analysis into structured sections
        sections = self._categorize_analysis(analysis, token_limit)

        # Build formatted prompt section with anti-hallucination wrapper
        return self._build_prompt_section(sections, citations, style)

    def _categorize_analysis(
        self,
        analysis: str,
        token_limit: int
    ) -> Dict[str, List[str]]:
        """
        Categorize analysis sentences by type for structured presentation

        Uses simple keyword matching - Gemini does the intelligent extraction.
        This just organizes for readability and anti-hallucination.

        Args:
            analysis: Raw Perplexity analysis text
            token_limit: Max tokens to allocate

        Returns:
            Dict of categorized sentences
        """
        # Token estimation: ~4 characters per token (rough approximation)
        char_limit = token_limit * 4

        # Truncate if exceeds limit (preserve complete sentences)
        if len(analysis) > char_limit:
            truncated = analysis[:char_limit]
            # Find last complete sentence
            last_period = truncated.rfind('.')
            if last_period > 0:
                analysis = truncated[:last_period + 1]
            else:
                analysis = truncated + "..."

        # Split into sentences (handles ., !, ?)
        sentences = re.split(r'(?<=[.!?])\s+', analysis)

        # Initialize categories
        categories = {
            'statistics': [],
            'trends': [],
            'competitive': [],
            'gaps': [],
            'general': []
        }

        # Simple keyword-based categorization
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence or len(sentence) < 10:
                continue

            lower = sentence.lower()

            # Statistics & data points (numbers, percentages, surveys)
            if any(indicator in lower for indicator in [
                '%', 'percent', 'percentage', 'of users', 'of companies',
                'survey', 'study shows', 'research shows', 'data shows',
                'statistics', 'metric', 'measured', 'reported'
            ]):
                categories['statistics'].append(sentence)

            # Market trends (growth, changes, shifts)
            elif any(indicator in lower for indicator in [
                'increasing', 'decreasing', 'growing', 'declining',
                'rising', 'falling', 'trend', 'shift', 'evolution',
                'adoption', 'emerging', 'accelerating'
            ]):
                categories['trends'].append(sentence)

            # Competitive intelligence (rankings, competitors, top results)
            elif any(indicator in lower for indicator in [
                'competitor', 'competition', 'ranking', 'top result',
                'featured snippet', 'serp', 'search result', 'domain',
                'content approach', 'common format'
            ]):
                categories['competitive'].append(sentence)

            # Content gaps (missing, lacking, opportunities)
            elif any(indicator in lower for indicator in [
                'missing', 'lacking', 'absent', 'gap', 'overlook',
                'no content', "doesn't cover", 'opportunity to',
                'could benefit from', 'needs more'
            ]):
                categories['gaps'].append(sentence)

            # General insights (everything else)
            else:
                categories['general'].append(sentence)

        return categories

    def _build_prompt_section(
        self,
        sections: Dict[str, List[str]],
        citations: List[str],
        style: str
    ) -> str:
        """
        Build formatted prompt section with anti-hallucination guards

        Args:
            sections: Categorized insights
            citations: Source URLs from Perplexity
            style: Content style (affects citation format)

        Returns:
            Ready-to-inject prompt section
        """
        parts = []

        # Header with explicit anti-hallucination rules
        parts.append("""
═══════════════════════════════════════════════════════════════
🔍 VERIFIED SERP INTELLIGENCE (from Perplexity Analysis)
═══════════════════════════════════════════════════════════════

⚠️ CRITICAL ANTI-HALLUCINATION RULES:
• ONLY use insights listed below - DO NOT fabricate additional claims
• When referencing insights, cite with [Source Name](exact_url) from the directory below — NEVER emit bare [N] bracket numbers
• If uncertain about a claim, omit it rather than invent details
• Paraphrase for readability but maintain factual accuracy
• Never claim features/capabilities not verified in these sources
""")

        section_caps = (
            ('statistics', "\n📊 KEY STATISTICS & DATA POINTS:", 8),
            ('trends', "\n📈 MARKET TRENDS & PATTERNS:", 6),
            ('competitive', "\n🎯 COMPETITIVE LANDSCAPE INSIGHTS:", 6),
            ('gaps', "\n💡 IDENTIFIED CONTENT GAPS:", 5),
            ('general', "\n📝 ADDITIONAL INSIGHTS:", 8),
        )
        retained_by_sec = [
            (key, header, list(sections.get(key) or [])[:cap])
            for key, header, cap in section_caps
            if sections.get(key)
        ]

        indexed_valid: Dict[int, str] = (
            {i: u.strip() for i, u in enumerate(citations, 1) if isinstance(u, str) and u.strip()}
            if isinstance(citations, list)
            else {}
        )
        cite_ref_re = re.compile(r"\[(\d+)\](?!\()")
        referenced_indices: List[int] = []
        for _k, _h, items in retained_by_sec:
            for item in items:
                for num_str in cite_ref_re.findall(item):
                    idx = int(num_str)
                    if idx in indexed_valid and idx not in referenced_indices:
                        referenced_indices.append(idx)

        selected_old_indices = list(referenced_indices[:20])
        for idx in indexed_valid:
            if len(selected_old_indices) >= 20:
                break
            if idx not in selected_old_indices:
                selected_old_indices.append(idx)

        selected_urls = {i: indexed_valid[i] for i in selected_old_indices}

        def _link_or_drop(item: str) -> Optional[str]:
            refs = [int(n) for n in cite_ref_re.findall(item)]
            if not refs:
                return item
            if any(r not in selected_urls for r in refs):
                return None
            return cite_ref_re.sub(
                lambda m: f"[{self._extract_domain(u := selected_urls[int(m.group(1))].replace(')', '%29'))}]({u})",
                item,
            )

        from .url_safety import fence_untrusted_text

        data_parts: List[str] = []
        for _key, header, items in retained_by_sec:
            remapped_items = [r for r in (_link_or_drop(it) for it in items) if r is not None]
            if remapped_items:
                data_parts.append(header)
                for bullet in remapped_items:
                    data_parts.append(f"  • {bullet}")

        # Add citation directory matching retained references (URL-backed links; formatter emits numbered footnotes)
        if selected_old_indices:
            data_parts.append("\n📚 SOURCE DIRECTORY (cite inline as [Source Name](exact_url); do NOT emit bare numbered markers):")
            for old_i in selected_old_indices:
                url = indexed_valid[old_i]
                domain = self._extract_domain(url)
                data_parts.append(f"  • {domain}: {url}")

        if data_parts:
            parts.append(fence_untrusted_text("\n".join(data_parts), source="serp_intelligence", max_chars=24000))

        # Usage instructions for LLM
        parts.append("""
💡 INTEGRATION GUIDELINES:
• Weave insights naturally into relevant content sections
• Attribute referenced claims inline with [Source Name](exact_url) — numbered footnotes are generated automatically by the formatter
• Prioritize statistics and trends for establishing credibility
• Address identified content gaps to differentiate from competitors
• Combine these SERP insights with Reddit/Quora community insights
• Focus on insights most relevant to the specific topic being covered
═══════════════════════════════════════════════════════════════
""")

        return '\n'.join(parts)

    def _extract_domain(self, url: str) -> str:
        """
        Extract readable domain name from URL

        Args:
            url: Full URL string

        Returns:
            Cleaned domain name (e.g., 'Gartner' from 'https://gartner.com/report')
        """
        try:
            # Match domain from URL
            match = re.search(r'https?://(?:www\.)?([^/]+)', url)
            if match:
                domain = match.group(1)
                # Remove common TLDs for brevity
                domain = re.sub(r'\.(com|org|net|edu|gov|io|co)$', '', domain)
                # Capitalize first letter of each word
                return domain.replace('.', ' ').title()
            return "Source"
        except Exception:
            return "Source"

    def get_stats(self, serp_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get statistics about SERP insights for logging

        Args:
            serp_data: Full SERP data dict

        Returns:
            Stats dict with enabled status, counts, token estimates
        """
        if not serp_data or not serp_data.get('serp_analysis'):
            return {'enabled': False}

        analysis = serp_data['serp_analysis'].get('analysis', '')
        citations = serp_data['serp_analysis'].get('citations', [])

        return {
            'enabled': True,
            'analysis_chars': len(analysis),
            'citation_count': len(citations),
            'estimated_tokens': len(analysis) // 4  # Rough token estimate
        }
