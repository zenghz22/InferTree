# Visual classification guide

Use caption and layout evidence conservatively. Candidate metadata includes `category`, `importance_score`, `confidence`, `evidence`, `category_rank`, and `recommended`; these fields prioritize inspection but do not replace judgment.

- **Main mechanism / architecture (`main_method`)**: overview, framework, architecture, pipeline, workflow, model structure, system diagram, or a caption describing how named components interact. Prefer an early figure that covers the complete method over a component detail. A low-numbered figure alone is insufficient.
- **Main experimental result (`main_result`)**: comparison against baselines, principal benchmark, performance curve, quantitative result, or the table/figure supporting the paper's central claim. Prefer overall test-set comparisons and multiple strong baselines over appendix detail.
- **Supporting experiment**: ablation, sensitivity, robustness, efficiency, qualitative example, or error analysis.
- **Other**: dataset examples, motivation, background illustration, UI screenshot, or a candidate that cannot be classified confidently.

When several candidates share a category, compare caption scope and experimental role rather than selecting strictly by score. Do not equate every Figure 1 with the architecture or every Table 1 with the main result. Return uncertainty when captions are ambiguous, and never claim pixel-level inspection based only on these fields.
