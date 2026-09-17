import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  workers: 2,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: process.env.PW_REPORT_DIR || 'playwright-report' }],
    ['json', { outputFile: process.env.PW_RESULTS_FILE || 'test-results/results.json' }]],
  use: {
    baseURL: process.env.PW_BASE_URL || 'http://127.0.0.1:5180',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chrome',
      use: { ...devices['Desktop Chrome'], channel: process.env.PW_BROWSER_CHANNEL || 'chrome' },
    },
  ],
  // Live deployment acceptance must exercise the actual 5173 entry, not a
  // second dev server with potentially different proxy configuration.
  webServer: process.env.PW_BASE_URL ? undefined : {
    command: 'npm run dev -- --port 5180',
    url: 'http://127.0.0.1:5180',
    reuseExistingServer: !process.env.CI,
    timeout: 30000,
  },
})
