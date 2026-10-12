import { readFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
export const name = 'bridge-budget';
export const inject = ['commands'];

export async function openSessionSafari(data, baseURL, run = promisify(execFile)) {
  const base = new URL(baseURL);
  // The exported session-transfer userscript carries the same bridge PORT.
  const port = base.port || '80';
  if (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' || !/^[1-9][0-9]{0,4}$/.test(port) || Number(port) > 65535
      || !/^[A-Za-z0-9_-]{43}$/.test(data.nonce)) throw new Error('Unsupported Safari transfer endpoint');
  const fragment = new URLSearchParams({bridge_session: data.nonce, expires: String(Date.now() + data.expires_in * 1000)});
  await run('/usr/bin/open', ['-a', 'Safari', 'https://chat.deepseek.com/#' + fragment.toString()], {timeout:5000});
}

export function formatBudget(data) {
  const names = {generation:'Генерации', chat:'Новые чаты', http:'HTTP-запросы'};
  const rows = data.budgets.map(b => `${names[b.category]} / ${b.window_seconds === 3600 ? 'час' : 'сутки'}: ${b.used} / ${b.limit}; осталось ${b.remaining}` +
    (b.remaining === 0 ? `; освободится ${new Date(b.next_available_at * 1000).toLocaleString('ru-RU')}` : ''));
  return ['Локальный бюджет DeepSeek (не квота провайдера)', ...rows,
    `Отдельный лимит одного диалога: ${data.conversation?.max_replies ?? 'не сообщён мостом'} подтверждённых ответов.`,
    'Расход текущего диалога здесь не показан; /bridge-budget-reset этот лимит не сбрасывает.',
    `Интервал: ${data.min_interval_seconds} сек.`,
    `Защитная пауза: ${data.paused || 'нет'}. Активный запрос: ${data.busy ? 'да' : 'нет'}.`,
    'Для ручного сброса: /bridge-budget-reset'].join('\n');
}

export function apply(ctx, config, {openSafari = openSessionSafari} = {}) {
  const url = new URL(config.baseURL || 'http://127.0.0.1:8000');
  if (url.protocol !== 'http:' || !['127.0.0.1','[::1]'].includes(url.hostname) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('Budget endpoint must be literal loopback HTTP');
  const pending = new Map();
  async function request(path, body) {
    const token = (await readFile(config.tokenFile, 'utf8')).trim();
    if (!/^[a-f0-9]{64}$/.test(token)) throw new Error('Budget credentials unavailable');
    const response = await fetch(new URL(path, url), {method: body ? 'POST' : 'GET', redirect:'error',
      signal: AbortSignal.timeout(5000), headers:{Authorization:`Bearer ${token}`, 'Content-Type':'application/json'},
      ...(body ? {body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Budget service unavailable');
    return data;
  }
  function handler(action) {
    return async invocation => {
      try {return {kind:'success', text: await action(invocation)};}
      catch {return {kind:'error', text:'Операция не выполнена. Мост недоступен, занят, счётчики изменились или нет доступа. Выполни /bridge-budget и повтори подтверждение.'};}
    };
  }
  ctx.commands.register({definitionId:'local/bridge-budget', name:'bridge-budget',
    description:'Локальные счётчики и лимиты DeepSeek', recordInput:false,
    handler:handler(async () => formatBudget(await request('/admin/budget')))});
  ctx.commands.register({definitionId:'local/deepseek-login', name:'deepseek-login',
    description:'Обновить сессию DeepSeek через Safari, сохранив историю и счётчики', recordInput:false,
    handler:handler(async () => {
      const data = await request('/admin/session/deepseek/start', {});
      try { await openSafari(data, url.href); }
      catch (error) { await request('/admin/session/deepseek/cancel', {}); throw error; }
      return 'Safari открыт. Расширение DeepSeek session transfer должно быть установлено и разрешено для DeepSeek и 127.0.0.1. Войди в тот же аккаунт, если потребуется. При первом переносе подтверди аккаунт. Запрос действует 10 минут. История и счётчики сохраняются; цель сама не возобновится. Результат: /deepseek-login-status.';
    })});
  ctx.commands.register({definitionId:'local/deepseek-login-status', name:'deepseek-login-status',
    description:'Результат переноса сессии Safari', recordInput:false,
    handler:handler(async () => {
      const data = await request('/admin/session/deepseek/status');
      const labels = {idle:'Перенос ещё не запускался.', waiting:'Ожидается сессия Safari.', saved:'Сессия сохранена. История и счётчики сохранены. Цель не возобновлялась.', expired:'Время переноса истекло. Повтори /deepseek-login.', cancelled:'Перенос отменён.', state_changed:'Состояние изменилось. Повтори /deepseek-login.', failed:'Перенос не завершён. Проверь состояние моста.'};
      return labels[data.status] || 'Неизвестный результат переноса.';
    })});
  ctx.commands.register({definitionId:'local/bridge-budget-reset', name:'bridge-budget-reset',
    description:'Ручной сброс локальных счётчиков (с подтверждением)', input:{hint:'[CONFIRM]'}, recordInput:false,
    handler:handler(async invocation => {
      const id = invocation.agent.session.id;
      if (invocation.rawInput.trim() === '') {
        const data = await request('/admin/budget');
        if (data.busy) return 'Есть активный или неопределённый запрос. Сброс запрещён.';
        if (pending.size >= 100) pending.clear();
        pending.set(id, {revision:data.revision, until:Date.now()+60000});
        return formatBudget(data)+'\n\nСброс удалит локальный учёт расхода за час и сутки. Квоты сайта, защитные паузы и задержки сохранятся. Для подтверждения в течение 60 секунд: /bridge-budget-reset CONFIRM';
      }
      const preview = pending.get(id);
      pending.delete(id);
      if (invocation.rawInput.trim() !== 'CONFIRM' || !preview || preview.until < Date.now()) return 'Сначала выполни /bridge-budget-reset без аргументов и просмотри счётчики.';
      const data = await request('/admin/budget/reset', {confirmation:'RESET LOCAL BUDGET', revision:preview.revision});
      return 'Локальные счётчики сброшены вручную.\n'+formatBudget(data);
    })});
}
