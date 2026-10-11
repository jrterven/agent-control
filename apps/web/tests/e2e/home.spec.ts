import { test, expect } from './fixtures';
import type { HomeData, PanelDefinition } from '@hermes-control/shared-types';

test.use({ serviceWorkers: 'block' });

function data(): HomeData {
  const sources = { sessionIds: [], workspaceIds: [], automationIds: [], mailAccountIds: [], urls: [] };
  return {
    features: { customPanels: false, proactive: false },
    layout: { revision: 0, background: 'mist', reduceTransparency: false,
      panels: ['pending', 'prepared', 'background', 'automations'].map((kind) => ({ id: kind, kind, title: '', description: '', size: 'compact', pinned: false, sources, profileId: null, refreshMinutes: 1440, display: 'summary' } as PanelDefinition)),
      proactive: { enabled: false, profileId: null, sources, timezone: 'America/Mexico_City', language: 'es', quietStart: null, quietEnd: null } },
    snapshots: { background: { status: 'ready', updatedAt: null, items: [{ id: 'task', title: 'Investigación semanal', status: 'running', sessionId: 'session-e2e' }] } }, suggestions: [],
    sources: { sessions: [], workspaces: [], profiles: [], automations: [], mailAccounts: [] },
    review: { model: 'gpt-6-luna', intervalMinutes: 60, lastCheckedAt: null, lastAnalysisAt: null, nextReviewAt: null, errorCode: null, history: [] },
  };
}

test('Home personaliza, persiste y fija paneles sin desbordar la pantalla', async ({ page }) => {
  let home = data();
  await page.route('**/api/v1/home**', async (route) => {
    if (route.request().method() === 'PUT') home = { ...home, layout: { ...route.request().postDataJSON(), revision: home.layout.revision + 1 } };
    await route.fulfill({ json: home });
  });
  await page.goto('/');
  await expect(page).toHaveURL(/\/home$/);
  await expect(page.getByRole('heading', { name: 'Más espacio para lo que importa.' })).toBeVisible();
  await expect(page.getByText('Investigación semanal')).toBeVisible();
  await page.getByRole('button', { name: 'Personalizar', exact: true }).click();
  const panel = page.getByRole('article', { name: 'Trabajando en segundo plano' });
  await panel.getByRole('button', { name: 'Fijar junto al chat' }).click();
  await panel.getByRole('combobox').selectOption('wide');
  await panel.getByRole('button', { name: 'Subir', exact: true }).click();
  await page.getByLabel('Fondo', { exact: true }).selectOption('dusk');
  await page.getByLabel('Reducir transparencias').check();
  await page.getByRole('button', { name: 'Listo', exact: true }).click();
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-home-background', 'dusk');
  await expect(page.locator('html')).toHaveAttribute('data-reduce-transparency', 'true');
  expect(home.layout.panels[1].id).toBe('background');
  expect(home.layout.panels[1].pinned).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: `test-results/home-${test.info().project.name}.png`, fullPage: true });
  await page.goto('/chats');
  if (page.viewportSize()!.width >= 1200) await expect(page.getByRole('complementary', { name: 'Tus paneles' })).toBeVisible();
  else await expect(page.getByRole('complementary', { name: 'Tus paneles' })).toBeHidden();
});
