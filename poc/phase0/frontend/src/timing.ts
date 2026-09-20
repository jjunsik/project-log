export interface Segment {
  session_id: string; sequence: number; revision_id: string | null;
  duration_ms: number; active: boolean; waiting: boolean; closed: boolean; observed_at: string;
}

// A timer is local to a page session. No subtraction of model wait from reading time.
export class ReviewClock {
  private last: number;
  private sequence = 0;
  private running = false;
  private visible = true;
  private waiting = false;
  private revision: string | null = null;
  constructor(private id: string, private now: () => number, private emit: (s: Segment) => void) {
    this.last = now();
  }
  checkpoint(closed = false) {
    const current = this.now();
    const elapsed = Math.max(0, current - this.last);
    // A throttled/suspended browser cannot establish attention in the unobserved interval.
    if (elapsed <= 15000) {
      this.emit({session_id: this.id, sequence: this.sequence++, revision_id: this.revision,
        duration_ms: elapsed, active: this.running && this.visible,
        waiting: this.waiting, closed, observed_at: new Date().toISOString()});
    } else {
      this.sequence++; // Persisted sequence gap signals an unknown interval.
      this.emit({session_id: this.id, sequence: this.sequence++, revision_id: this.revision,
        duration_ms: 0, active: false, waiting: this.waiting, closed,
        observed_at: new Date().toISOString()});
    }
    this.last = current;
  }
  set(values: {running?: boolean; visible?: boolean; waiting?: boolean; revision?: string | null}) {
    this.checkpoint();
    if (values.running !== undefined) this.running = values.running;
    if (values.visible !== undefined) this.visible = values.visible;
    if (values.waiting !== undefined) this.waiting = values.waiting;
    if (values.revision !== undefined) this.revision = values.revision;
  }
}
