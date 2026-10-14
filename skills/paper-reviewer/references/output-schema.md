# Structured review output

Return valid JSON when structured output is requested.

```json
{
  "summary": "string",
  "contributions": ["string"],
  "strengths": ["string"],
  "major_concerns": [
    {
      "issue": "string",
      "evidence": [{"page": 1, "section": "string", "quote": "short excerpt"}],
      "impact": "string",
      "suggested_action": "string"
    }
  ],
  "minor_concerns": ["string"],
  "questions": ["string"],
  "external_sources": [{"title": "string", "doi": "string or null", "url": "string"}],
  "reproducibility": {"assessment": "string", "missing_items": ["string"]},
  "confidence": 1,
  "recommendation": "accept | minor_revision | major_revision | reject | insufficient_evidence"
}
```

Confidence is an integer from 1 (low) to 5 (high). Quotes must remain short and must come from the supplied manuscript.

