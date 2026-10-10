import {afterEach, describe, expect, it, vi} from 'vitest';
import {api, ApiError, collectionOption, collectionSelection, hasActiveCollection} from './model';
import {encodePath} from './Browser';
import type {Collection, CollectionState, Project} from './model';

afterEach(() => vi.unstubAllGlobals());

describe('application API failure and lifecycle contract', () => {
  it('propagates a failed registration instead of treating its JSON as a registered project', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({code: 'duplicate', detail: '이미 등록된 Repository입니다.'}), {status: 409},
    )));
    await expect(api('projects', 'POST', {name: 'project'})).rejects.toThrow('이미 등록된 Repository');
  });
  it('sends an empty cancellation as JSON accepted by the local request boundary', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({id: 'next', state: 'queued'}), {status: 202}));
    vi.stubGlobal('fetch', fetch);
    const result = await api<{id: string}>('collections/previous/cancel', 'POST');
    expect(result.id).toBe('next');
    const [url, request] = fetch.mock.calls[0];
    expect(url).toBe('/api/collections/previous/cancel');
    expect(request.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(request.body)).toEqual({});
  });
  it('sends manual collection to the Project endpoint without memo input', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({id: 'next', kind: 'manual'}), {status: 202}));
    vi.stubGlobal('fetch', fetch);
    await api('projects/project/collections', 'POST');
    const [url, request] = fetch.mock.calls[0];
    expect(url).toBe('/api/projects/project/collections');
    expect(JSON.parse(request.body)).toEqual({});
  });
  it.each<CollectionState>(['capturing', 'queued', 'running'])('shares the active slot for %s', state => {
    const project = {collections: [{state: 'completed'}, {state}]} as Project;
    expect(hasActiveCollection(project)).toBe(true);
  });
  it('allows a new observation when only terminal collections exist', () => {
    const project = {collections: ['completed'].map(state => ({state}) as Collection)} as Project;
    expect(hasActiveCollection(project)).toBe(false);
    expect(hasActiveCollection(null)).toBe(false);
  });
});


it('encodes non-ASCII directory locators as UTF-8 bytes without replacing the display path', () => {
  expect(encodePath('docs/설계/')).toBe(Buffer.from('docs/설계/', 'utf8').toString('base64'));
});

it('uses only a title in select and keeps technical fallback without description', () => {
  const c = {id: 'one', state: 'completed', created_at: '2026-10-07T00:00:00Z', snapshot: {branch: 'main'}, description: 'private memo'} as Collection;
  expect(collectionOption({...c, title: '작업 기록'})).toBe('작업 기록');
  expect(collectionOption(c)).toContain('main · 수집 완료');
  expect(collectionOption(c)).not.toContain('private memo');
});
it('retains an existing selection, otherwise chooses newest completed or empty', () => {
  const cs = [{id: 'active', state: 'running', created_at:'2026-10-09T00:00:00Z'}, {id: 'new', state: 'completed', created_at:'2026-10-08T00:00:00Z'}, {id: 'old', state: 'completed', created_at:'2026-10-07T00:00:00Z'}] as Collection[];
  expect(collectionSelection(cs, '')).toBe('new');
  expect(collectionSelection(cs, 'old')).toBe('old');
  expect(collectionSelection(cs, 'active')).toBe('active');
  expect(collectionSelection(cs, 'deleted')).toBe('new');
  expect(collectionSelection([], 'deleted')).toBe('');
  expect(hasActiveCollection({collections: [], collection_busy: true} as unknown as Project)).toBe(true);
});

it('uses the completed numbering order regardless of response order and excludes unfinished defaults', () => {
  const older={id:'older',state:'completed',created_at:'2026-10-07T00:00:00Z'} as Collection;
  const sameTime=[{...older,id:'00000000-0000-0000-0000-000000000002',created_at:'2026-10-08T00:00:00Z'},
    {...older,id:'00000000-0000-0000-0000-000000000001',created_at:'2026-10-08T00:00:00Z'}];
  expect(collectionSelection([older,...sameTime], '')).toBe(sameTime[0].id);
  expect(collectionSelection([sameTime[1],older], 'deleted')).toBe(sameTime[1].id);
  for(const state of ['capturing','queued','running','cancel_pending'] as const) {
    const active={...older,id:'active',state,created_at:'2026-10-09T00:00:00Z'};
    expect(collectionSelection([active,older], '')).toBe('older');
    expect(collectionSelection([active], '')).toBe('');
  }
});

it('preserves the HTTP status so a collection removed during polling can be refreshed', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({detail: '수집 기록이 없습니다.'}), {status: 404})));
  try {await api('collections/removed'); throw new Error('expected failure');}
  catch (error) {expect(error).toBeInstanceOf(ApiError); expect((error as ApiError).status).toBe(404);}
});
