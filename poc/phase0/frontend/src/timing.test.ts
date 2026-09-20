import { describe, it, expect } from 'vitest';
import { ReviewClock, type Segment } from './timing';

describe('active review and wait measurements', () => {
  it('preserves simultaneous reading and waiting, and does not require typing', () => {
    let now = 0; const samples: Segment[] = [];
    const clock = new ReviewClock('session', () => now, s => samples.push(s));
    clock.set({running: true}); now = 1000;
    clock.set({waiting: true}); now = 3000;
    clock.set({visible: false}); now = 4000;
    clock.checkpoint(true);
    const sum = (f: (s: Segment) => boolean) => samples.filter(f).reduce((n, s) => n + s.duration_ms, 0);
    expect(sum(s => s.active)).toBe(3000);
    expect(sum(s => s.waiting)).toBe(3000);
    expect(sum(s => s.waiting && s.active)).toBe(2000);
  });
  it('marks suspended browser intervals by a sequence gap instead of invented activity', () => {
    let now = 0; const samples: Segment[] = [];
    const clock = new ReviewClock('session', () => now, s => samples.push(s));
    clock.set({running: true}); now = 120000; clock.checkpoint(true);
    expect(samples.at(-1)?.duration_ms).toBe(0);
    expect(samples.map(s => s.sequence)).toEqual([0, 2]);
  });
});
