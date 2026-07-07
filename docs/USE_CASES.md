# openppx Capability Use Cases

This document provides realistic tasks that can be tried directly, along with copy-ready prompt templates.

## 1. Real Task Examples by Risk Level

### 1.1 Beginner: Low Risk

1. News briefing
   - Visit several news home pages and collect headlines.
   - Deduplicate items and return key summaries with source links.

2. Page screenshot and PDF archiving
   - Open a list of pages.
   - Export screenshots and PDFs to a target directory.

3. Site availability check
   - Visit a list of URLs and check reachability, unexpected redirects, and timeouts.
   - Return an availability report.

### 1.2 Intermediate: Medium Risk

1. E-commerce price comparison
   - Open several commerce sites and search for the same product.
   - Extract price, shipping cost, estimated delivery time, and a recommendation.

2. Flight or hotel filtering
   - Filter by budget, date, direct flight, free cancellation, and other constraints.
   - Return candidate options and a final recommendation.

3. Website console error audit
   - Open core pages and collect console errors and warnings.
   - Produce a reviewable error list.

4. Test form autofill
   - Fill fields on a test page and prepare for submission.
   - Pause for human confirmation before the final submit action.

### 1.3 High Risk: Confirmation Required

1. Semi-automated task after login
   - The user logs in manually.
   - The agent performs navigation, information gathering, and draft preparation.
   - High-risk actions stay behind explicit confirmation.

Recommended trial order: beginner, then intermediate, then high-risk workflows.

## 2. Copy-Ready Prompt Templates

Replace placeholders with real values before sending these prompts to openppx.

### 2.1 E-commerce Price Comparison

```text
Help me compare prices for this product: <product name>.
Requirements:
1) Visit at least 3 e-commerce sites.
2) Extract price, shipping cost, and estimated delivery time for each site.
3) Recommend one option and explain why.
4) Return the final result as a table.
```

### 2.2 Flight or Hotel Filtering

```text
Help me filter travel options.
Origin: <origin>, destination: <destination>, dates: <date range>, budget: <budget>.
Requirements:
1) Prefer direct flights for airfare, or free cancellation for hotels.
2) Return 3-5 candidates.
3) For each candidate, include price, key restrictions, and the reason for recommendation.
```

### 2.3 News Briefing

```text
Create today's news briefing.
Requirements:
1) Visit at least 3 news source home pages.
2) Collect and deduplicate headlines.
3) Return 5 key items, each with a one-sentence summary and source link.
```

### 2.4 Test Form Autofill

```text
Open this test form and fill it automatically: <form URL>.
Use these fields and values: <fields and values>.
Requirements:
1) Do not submit after filling the form.
2) Show me a preview of the filled content.
3) Submit only after I confirm "you may submit".
```

### 2.5 Website Console Error Audit

```text
Audit frontend errors for this website: <site URL>.
Requirements:
1) Visit these core pages: <page list>.
2) Collect console errors and warnings.
3) Return an error list grouped by page, including the error text.
```

### 2.6 Page Screenshot and PDF Archiving

```text
Archive these pages: <URL list>.
Requirements:
1) Save both a screenshot and a PDF for each page.
2) Save files to this directory: <directory path>.
3) Return the file list and save status.
```

### 2.7 Site Availability Check

```text
Check availability for these URLs: <URL list>.
Requirements:
1) Check whether each URL is reachable, redirects unexpectedly, or times out.
2) Return status and failure reason for each URL.
3) Provide an overall availability conclusion.
```

### 2.8 Semi-Automated Task After Login

```text
I want to run a task after login: <task description>.
Follow this flow:
1) Open the target site and ask me to log in manually.
2) Continue only after I confirm "login complete".
3) Ask for confirmation again before any high-risk action such as publishing, deleting, or transferring money.
```
