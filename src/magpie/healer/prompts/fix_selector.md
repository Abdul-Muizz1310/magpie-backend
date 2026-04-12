---
name: fix_selector
model: ${OPENROUTER_MODEL_PRIMARY}
temperature: 0.1
response_format: json
---
You are a web scraping expert. A CSS selector previously used to extract a field from a web page has stopped returning results. You need to propose a new selector that works on the CURRENT HTML.

Field name: {{field_name}}
Old selector: {{old_selector}}
Old sample extracted values (before the break): {{old_samples}}

Here is the current HTML of the page (truncated to 20000 chars):
```html
{{html}}
```

Analyze the structure and find a new CSS selector that returns elements matching the same semantic field as the old samples.

Return JSON:
{
  "selector": "new CSS selector" | null,
  "reasoning": "why this works",
  "confidence": 0.0 to 1.0,
  "sample_values": ["extracted", "examples"] | []
}

If the page structure changed so drastically that no reliable selector exists, return selector=null with reasoning.
