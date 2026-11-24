# Fleo Brand Context Builder Prompt (Works in ChatGPT, Claude, Cursor, or OpenCode)

Copy everything inside the prompt block below into your favorite AI assistant. Save the markdown output it produces to **`.agents/product-marketing.md`** in the folder where you run `npx @flyweel-labs/fleo`; Fleo automatically imports it on launch!

---

```markdown
You are helping me create a `.agents/product-marketing.md` brand context file for **Fleo by Flyweel** (and compatible open-source marketing skills).

Ask me **one brief group of questions at a time** (or read my website URL / codebase if you have web or repo tools):
1. **Company & Website**: What is my company name, website URL, product category, and a 1–2 sentence plain-English description of what we build?
2. **Products & Pricing**: What are our main products/tiers, key features, and exact public pricing (so articles never guess or invent features)?
3. **Target Buyer (ICP) & Pain Points**: Who is the real buyer/reader (job titles, company type/stage), and what are the top 2–3 concrete frustrations they experience before finding us? What verbatim words/phrases do customers use?
4. **Competitors & Differentiation**: Who are our top 3 direct alternatives/competitors, and where do we genuinely win?
5. **Brand Voice & Accuracy Rules**: How should our articles sound (e.g. direct, technical, conversational), what buzzwords should we ban, and are there any legal/compliance claims we must NEVER make?

Once you have those answers, output ONLY a clean Markdown file in this exact structure so I can save it to `.agents/product-marketing.md`:

# Product Marketing Context: [Company Name]

*Last updated: YYYY-MM-DD*

## Product Overview
- **Company:** [Company Name]
- **Website:** [https://...]
- **One-liner:** [1–2 factual sentences]
- **Category:** [Primary category]
- **Primary CTA:** [CTA Label] ([Signup/Demo URL])

## Products & Capabilities
- **[Product 1]** ([URL]): [Factual description] | Pricing: [Exact pricing]

## Target Audience (ICP)
- **Primary Reader:** [Specific buyer/reader roles and company profile]
- **Business Models:** [1–3 workflow bullets]
- **Priority Topics:** [6–10 core search topics]

## Problems & Pain Points
- **[pain_key]**: [Concrete description]

## Customer Language
- **Terms audience uses:** [comma-separated terms]
- **Words to avoid:** comprehensive, ultimate, leverage, delve, tapestry

## Competitive Landscape
- **Direct competitors:** [Competitor 1, Competitor 2, Competitor 3]

## Brand Voice & Guardrails
- **Tone:** [Brand voice description]
### Accuracy & Compliance Rules
- [Rule 1: e.g., State only verified features and public pricing]
```
