const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const queries = fs
  .readFileSync(path.join(__dirname, 'queries.csv'), 'utf-8')
  .trim()
  .split('\n')
  .slice(1)
  .map((line) => {
    const [query_id, category, expected_behavior, ...rest] = line.split(',');
    return {
      query_id,
      category,
      expected_behavior,
      query: rest.join(','),
    };
  });

const results = [];

for (const item of queries) {
  test(`LLM query ${item.query_id}: ${item.query}`, async ({ page }) => {
    await page.goto('http://127.0.0.1:8000/ui');

    await page.selectOption('#tenantSelect', 'COMP-SHOPIFY');
    await page.fill('#userInput', item.query);

    const started = Date.now();
    await page.click('button[type="submit"]');

    const responseLocator = page.locator('.agent-msg').last();

    await expect(responseLocator).not.toContainText('Luintix is checking...', {
      timeout: 110000,
    });

    const responseText = await responseLocator.innerText();
    const durationMs = Date.now() - started;

    const result = {
      query_id: item.query_id,
      category: item.category,
      query: item.query,
      expected_behavior: item.expected_behavior,
      response: responseText,
      duration_ms: durationMs,
      status: responseText.includes('Error') ? 'FAIL' : 'PASS',
      possible_hallucination:
        /definitely|guaranteed|I have escalated|refund has been issued/i.test(responseText),
      captured_at: new Date().toISOString(),
    };

    results.push(result);
    fs.writeFileSync(
      path.join(__dirname, 'results', 'test-results.json'),
      JSON.stringify(results, null, 2)
    );

    expect(result.status).toBe('PASS');
  });
}