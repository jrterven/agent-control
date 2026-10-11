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


test('preguntar sobre un panel prepara texto sin enviar ni crear una conversación', async ({ page }) => {
  const home = data();
  let writes = 0;
  await page.route('**/api/v1/home', (route) => route.fulfill({ json: home }));
  page.on('request', (request) => { if (request.method() === 'POST' && /\/sessions(?:$|\/[^/]+\/prompts$)/.test(new URL(request.url()).pathname)) writes++; });
  await page.goto('/home');
  await page.getByRole('article', { name: 'Trabajando en segundo plano' }).getByRole('button', { name: 'Preguntar sobre este panel' }).click();
  await expect(page).toHaveURL(/\/chats$/);
  await expect(page.getByRole('textbox', { name: 'Mensaje a Newton…' })).toHaveValue(/Investigación semanal/);
  expect(writes).toBe(0);
});

test('describe un panel, revisa la vista previa y guarda componentes seguros', async ({ page }) => {
  let home = data();
  home.features.customPanels = true;
  home.sources.profiles = [{ id: 'profile-newton-e2e', label: 'Newton', available: true }];
  await page.route('**/api/v1/home**', async (route) => {
    if (route.request().url().endsWith('/panels/preview')) {
      const panel = { ...route.request().postDataJSON().panel, title: 'Investigación' };
      const snapshot = { status: 'ready', updatedAt: '2026-10-11T12:00:00Z', blocks: [{ type: 'list', items: ['Resumen listo para revisar', '<script>window.panelExecuted=true</script>'] }] };
      home.snapshots[panel.id] = snapshot as HomeData['snapshots'][string];
      return route.fulfill({ json: { definition: panel, snapshot } });
    }
    if (route.request().method() === 'PUT') home = { ...home, layout: { ...route.request().postDataJSON(), revision: 1 } };
    return route.fulfill({ json: home });
  });
  await page.goto('/home');
  await page.getByRole('button', { name: 'Agregar panel', exact: true }).first().click();
  await page.getByRole('button', { name: /Describe un panel/ }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('¿Qué te gustaría seguir?').fill('Resume mis hallazgos de investigación');
  await dialog.getByRole('combobox', { name: /^Agente/ }).selectOption('profile-newton-e2e');
  await dialog.getByRole('button', { name: 'Vista previa con Luna' }).click();
  await expect(dialog.getByText('Resumen listo para revisar')).toBeVisible();
  await dialog.getByRole('button', { name: 'Agregar a Home' }).click();
  await page.reload();
  await expect(page.getByRole('article', { name: 'Investigación' })).toBeVisible();
  expect(await page.evaluate(() => 'panelExecuted' in window)).toBe(false);
});
