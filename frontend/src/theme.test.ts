import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {describe, expect, it, vi} from 'vitest';

const source = readFileSync(new URL('../public/theme.js', import.meta.url), 'utf8');
function bootstrap({saved = null, dark = false, readFails = false, writeFails = false, removeFails = false}: {
  saved?: string | null; dark?: boolean; readFails?: boolean; writeFails?: boolean; removeFails?: boolean;
} = {}) {
  const listeners = new Set<() => void>();
  const media = {matches:dark, addEventListener:vi.fn((_event:string,fn:()=>void)=>listeners.add(fn)),
    removeEventListener:vi.fn((_event:string,fn:()=>void)=>listeners.delete(fn))};
  const storage = {
    getItem:vi.fn(() => {if(readFails) throw new Error('blocked'); return saved;}),
    setItem:vi.fn((_key:string,value:string) => {if(writeFails) throw new Error('blocked'); saved=value;}),
    removeItem:vi.fn(() => {if(removeFails) throw new Error('blocked'); saved=null;}),
  };
  const root = {dataset:{} as Record<string,string>,style:{} as Record<string,string>};
  const window = {matchMedia:()=>media,localStorage:storage} as unknown as Window;
  runInNewContext(source,{window,document:{documentElement:root}});
  return {theme:window.projectLogTheme,root,storage,listeners,media,
    changeSystem(value:boolean) {media.matches=value;listeners.forEach(fn=>fn());}};
}

describe('theme bootstrap and preference persistence',()=>{
  it.each([false,true])('applies the system theme before React and subscribes only on demand (%s)',dark=>{
    const app=bootstrap({dark});
    expect(app.theme.getPreference()).toBe('system');expect(app.root.dataset.theme).toBe(dark?'dark':'light');
    expect(app.root.style.colorScheme).toBe(app.root.dataset.theme);expect(app.listeners.size).toBe(0);
    const stop=app.theme.watchSystem();app.changeSystem(!dark);
    expect(app.root.dataset.theme).toBe(dark?'light':'dark');stop();expect(app.listeners.size).toBe(0);
  });
  it.each(['light','dark'] as const)('restores an explicit %s preference independently of the OS',saved=>{
    const app=bootstrap({saved,dark:saved==='light'});expect(app.root.dataset.theme).toBe(saved);
    const stop=app.theme.watchSystem();app.changeSystem(saved==='light');expect(app.root.dataset.theme).toBe(saved);stop();
  });
  it('stores the preference and returns to the current system setting',()=>{
    const app=bootstrap({dark:true});app.theme.setPreference('light');expect(app.root.dataset.theme).toBe('light');
    app.theme.setPreference('system');expect(app.storage.setItem).toHaveBeenLastCalledWith('project-log.theme','system');
    expect(app.theme.getPreference()).toBe('system');expect(app.root.dataset.theme).toBe('dark');
  });
  it.each(['invalid',null])('falls back to the system for a missing or invalid saved value (%s)',saved=>{
    const app=bootstrap({saved,dark:true});expect(app.theme.getPreference()).toBe('system');expect(app.root.dataset.theme).toBe('dark');
  });
  it('keeps rendering and switching when storage reads, writes and removal are blocked',()=>{
    const app=bootstrap({readFails:true,writeFails:true,removeFails:true});
    expect(app.theme.getPreference()).toBe('system');expect(()=>app.theme.setPreference('dark')).not.toThrow();
    expect(app.root.dataset.theme).toBe('dark');expect(app.theme.getPreference()).toBe('dark');
  });
  it('removes an older saved selection when a write fails but removal is available',()=>{
    const app=bootstrap({saved:'light',writeFails:true});app.theme.setPreference('dark');
    expect(app.storage.removeItem).toHaveBeenCalledWith('project-log.theme');expect(app.root.dataset.theme).toBe('dark');
  });
  it('resolves a system change that occurred before the listener was attached and cleans up resubscription',()=>{
    const app=bootstrap();app.changeSystem(true);const stop=app.theme.watchSystem();expect(app.root.dataset.theme).toBe('dark');
    stop();const next=app.theme.watchSystem();expect(app.listeners.size).toBe(1);next();expect(app.listeners.size).toBe(0);
  });
});
