const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './backend/tests/playwright',
  timeout: 120000,
  retries: 0,
  workers: 1,
  reporter: [
    ['list'],
    ['json', { outputFile: 'backend/tests/playwright/results/report.json' }],
  ],
  use: {
    headless: true,
    baseURL: 'http://127.0.0.1:8000',
    navigationTimeout: 60000,
  },
});