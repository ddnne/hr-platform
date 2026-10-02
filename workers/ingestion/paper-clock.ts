import {DurableObject} from "cloudflare:workers";

type PaperService = {
  paper_next_alarm(): Promise<number | null>;
  paper_tick(): Promise<void>;
};

/** Keep the persistent timer outside the Python scientific runtime. */
export class PaperClock extends DurableObject<Env> {
  private running = false;
  private syncing: Promise<void> = Promise.resolve();

  async sync(): Promise<void> {
    const next = this.syncing.then(async () => {
      if (this.running) return;
      const at = await (this.env.RESEARCH as unknown as PaperService).paper_next_alarm();
      if (this.running) return;
      if (at === null) await this.ctx.storage.deleteAlarm();
      else {
        if (!Number.isSafeInteger(at) || at <= 0) throw new Error("PAPER_ALARM_TIME");
        if (await this.ctx.storage.getAlarm() !== at) await this.ctx.storage.setAlarm(at);
      }
    });
    this.syncing = next.catch(() => {});
    await next;
  }

  async alarm(): Promise<void> {
    await this.syncing;
    this.running = true;
    try {
      await (this.env.RESEARCH as unknown as PaperService).paper_tick();
    } finally {
      this.running = false;
    }
    // Propagate errors for platform retry; the shared D1 lease prevents repeats.
    await this.sync();
  }
}
