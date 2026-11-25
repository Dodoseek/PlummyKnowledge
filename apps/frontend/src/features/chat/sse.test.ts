import { describe, expect, it } from 'vitest';

import { readSse, SseParser } from './sse';

function streamOf(...pieces: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const piece of pieces) controller.enqueue(encoder.encode(piece));
      controller.close();
    },
  });
}

async function collect(body: ReadableStream<Uint8Array>) {
  const events = [];
  for await (const event of readSse(body)) events.push(event);
  return events;
}

describe('SseParser', () => {
  it('parses named events with JSON data', () => {
    const parser = new SseParser();
    expect(parser.feed('event: token\ndata: {"delta":"Hi"}\n\n')).toEqual([
      { event: 'token', data: '{"delta":"Hi"}' },
    ]);
  });

  it('joins pieces split anywhere, including inside a CRLF', () => {
    const parser = new SseParser();
    const text = 'event: token\r\ndata: {"delta":"a"}\r\n\r\nevent: token\r\ndata: {"delta":"b"}\r\n\r\n';
    const events = [];
    for (const char of text) events.push(...parser.feed(char));
    expect(events.map((event) => event.data)).toEqual(['{"delta":"a"}', '{"delta":"b"}']);
  });

  it('accepts lone CR line breaks and multi-line data', () => {
    const parser = new SseParser();
    // A CR at the very end may be half of a CRLF: the event completes with the next piece.
    expect(parser.feed('data: one\rdata: two\r\r')).toEqual([]);
    expect(parser.feed(': next')).toEqual([{ event: 'message', data: 'one\ntwo' }]);
  });

  it('skips comments (heartbeats), ids and events without data', () => {
    const parser = new SseParser();
    expect(parser.feed(': heartbeat\n\nid: 7\nretry: 10\n\nevent: ping\n\n')).toEqual([]);
    // The name of the empty event does not leak into the next one.
    expect(parser.feed('data: x\n\n')).toEqual([{ event: 'message', data: 'x' }]);
  });

  it('keeps the value after the first colon only once trimmed of one space', () => {
    const parser = new SseParser();
    expect(parser.feed('data:  a: b\n\n')).toEqual([{ event: 'message', data: ' a: b' }]);
  });
});

describe('readSse', () => {
  it('yields events across chunks and multi-byte characters', async () => {
    const bytes = new TextEncoder().encode('data: привет\n\n');
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        // Split inside a two-byte character.
        controller.enqueue(bytes.slice(0, 8));
        controller.enqueue(bytes.slice(8));
        controller.close();
      },
    });
    expect(await collect(body)).toEqual([{ event: 'message', data: 'привет' }]);
  });

  it('drops an event cut off by the end of the stream', async () => {
    expect(await collect(streamOf('data: whole\n\n', 'event: token\ndata: half'))).toEqual([
      { event: 'message', data: 'whole' },
    ]);
  });

  it('cancels the stream when the reader stops early', async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: 1\n\ndata: 2\n\n'));
      },
      cancel() {
        cancelled = true;
      },
    });
    for await (const event of readSse(body)) {
      expect(event.data).toBe('1');
      break;
    }
    expect(cancelled).toBe(true);
  });
});
