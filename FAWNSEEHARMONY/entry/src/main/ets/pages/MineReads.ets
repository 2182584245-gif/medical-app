import { actorId } from '../services/Cloud';

// Each request belongs to one visible section of one member's page.
export class MineReads {
  private owner: number = 0;
  private sequence: number = 0;
  private tickets: Map<string, number> = new Map();

  begin(): void {
    this.stop();
    this.owner = actorId();
  }

  stop(): void {
    this.owner = 0;
    this.tickets.clear();
  }

  active(): boolean {
    return this.owner > 0 && this.owner === actorId();
  }

  async read<T>(key: string, request: () => Promise<T>, apply: (value: T) => void,
    feedback: (message: string) => void): Promise<void> {
    if (!this.active()) return;
    const ticket = ++this.sequence;
    this.tickets.set(key, ticket);
    feedback('');
    try {
      const value = await request();
      if (this.active() && this.tickets.get(key) === ticket) apply(value);
    } catch (e) {
      if (this.active() && this.tickets.get(key) === ticket) feedback((e as Error).message || '请稍后重试。');
    }
  }
}
