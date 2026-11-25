/**
 * Server-sent events over `fetch` (API_CONTRACTS.md §3). `EventSource` cannot send a POST body or the CSRF
 * header, so the stream is read from `Response.body` and parsed here.
 */

export interface SseEvent {
  event: string;
  data: string;
}

/**
 * Incremental parser of the `text/event-stream` format: feed it decoded text as it arrives, in pieces of any
 * size; it returns the events completed by each piece. Comments, `id:` and `retry:` lines are ignored.
 */
export class SseParser {
  private buffer = '';
  private event = '';
  private data: string[] = [];

  feed(text: string): SseEvent[] {
    this.buffer += text;
    const events: SseEvent[] = [];
    for (;;) {
      const match = /\r\n|\r|\n/.exec(this.buffer);
      if (match === null) break;
      // A lone "\r" at the very end may be the first half of "\r\n": wait for the next piece.
      if (match[0] === '\r' && match.index === this.buffer.length - 1) break;
      const line = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const event = this.line(line);
      if (event !== null) events.push(event);
    }
    return events;
  }

  private line(line: string): SseEvent | null {
    if (line === '') {
      if (this.data.length === 0) {
        this.event = '';
        return null;
      }
      const event = { event: this.event || 'message', data: this.data.join('\n') };
      this.event = '';
      this.data = [];
      return event;
    }
    if (line.startsWith(':')) return null;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.event = value;
    else if (field === 'data') this.data.push(value);
    return null;
  }
}

/**
 * Reads a byte stream and yields its events. An event cut off by the end of the stream is dropped (as the SSE
 * standard says). Leaving the loop early cancels the stream, which closes the connection.
 */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      yield* parser.feed(decoder.decode(value, { stream: true }));
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}
