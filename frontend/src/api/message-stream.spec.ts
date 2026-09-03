import { afterEach, describe, expect, it, vi } from 'vitest'

import { sendStreamedMessage } from './message-stream'

const request = {
  userId: '00000000-0000-4000-8000-000000000001',
  tripId: '22222222-2222-4222-8222-222222222222',
  idempotencyId: '33333333-3333-3333-3333-333333333333',
  message: '帮我规划广州三日游',
}

describe('sendStreamedMessage', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('posts a message and emits complete SSE events split across response chunks', async () => {
    const encoded = new TextEncoder()
    const response = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(
            encoded.encode(
              'event: task.started\ndata: {"sequence":1,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:00+00:00","task_id":"task_1","module":"planning","message":"正在规划行程"}\n\nevent: token.delta\ndata: {"sequence":2,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:00+00:00","text":"广州未来三天"}\n\n',
            ),
          )
          controller.enqueue(
            encoded.encode(
              'event: task.result\ndata: {"sequence":3,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:01+00:00","task_id":"task_1","module":"planning","status":"success","result":"已完成广州行程规划。"}\n\nevent: task.completed\ndata: {"sequence":4,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:01+00:00","task_id":"task_1","module":"planning","message":"正在规划行程"}\n\nevent: result\ndata: {"sequence":5,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:01+00:00","route":"planning","message":"广州未来三天适合出行。","candidate_itinerary":null,"current_itinerary":null}\n\nevent: run.completed\ndata: {"sequence":6,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:01+00:00","status":"completed"}\n\n',
            ),
          )
          controller.close()
        },
      }),
      { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } },
    )
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
    const received: string[] = []

    const outcome = await sendStreamedMessage(request, {
      onEvent(event) {
        received.push(event.type)
      },
    })

    expect(fetch).toHaveBeenCalledWith('/api/messages/stream', {
      method: 'POST',
      headers: { Accept: 'text/event-stream, application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: request.userId,
        trip_id: request.tripId,
        idempotency_id: request.idempotencyId,
        message: request.message,
      }),
      signal: undefined,
    })
    expect(received).toEqual([
      'task.started',
      'token.delta',
      'task.result',
      'task.completed',
      'result',
      'run.completed',
    ])
    expect(outcome).toEqual({ kind: 'stream', status: 'completed' })
  })

  it('returns a processing outcome when an identical request is still running', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            idempotency_id: request.idempotencyId,
            status: 'processing',
          }),
          { status: 202, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )

    await expect(sendStreamedMessage(request)).resolves.toEqual({ kind: 'processing' })
  })

  it('rejects a stream closed before its terminal event', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          'event: token.delta\ndata: {"sequence":1,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:00+00:00","text":"未完成"}\n\n',
          { headers: { 'Content-Type': 'text/event-stream' } },
        ),
      ),
    )

    await expect(sendStreamedMessage(request)).rejects.toThrow('流式响应在结束前断开')
  })
})
